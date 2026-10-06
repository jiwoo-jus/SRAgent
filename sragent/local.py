"""Reloadable vLLM endpoint pool with optional, owned SSH tunnels.

Only this process's tunnels are closed. Removed endpoints drain active requests;
new requests read the endpoint file again. Limits are per SRAgent process.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yaml

LOG = logging.getLogger(__name__)


class LocalUnavailable(RuntimeError):
    """Temporary endpoint failure; another endpoint or configuration may recover it."""


class LocalRequestError(RuntimeError):
    """A request needs correction, rather than another connection attempt."""


def endpoint_path(cfg: dict) -> Path:
    p = Path(cfg.get('local', {}).get('endpoints_file', 'local_endpoints.yaml')).expanduser()
    return p if p.is_absolute() else Path(cfg.get('_config_dir', 'configs')) / p


def validate_endpoints(data: dict) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get('endpoints'), list):
        raise ValueError('Expected a YAML mapping containing an endpoints list')
    out, seen = [], set()
    for value in data['endpoints']:
        if not isinstance(value, dict):
            raise ValueError('Each endpoint must be a mapping')
        e = dict(value)
        if bool(e.get('url')) == bool(e.get('ssh_host')):
            raise ValueError('Each endpoint needs either url or ssh_host')
        if e.get('url'):
            u = urlsplit(e['url'])
            if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError('Use an HTTP(S) URL without embedded credentials, query, or fragment')
            e['url'] = e['url'].rstrip('/')
            if not e['url'].endswith('/v1'):
                e['url'] += '/v1'
            key = e['url']
        else:
            for field, default in [('ssh_host', ''), ('remote_host', '127.0.0.4')]:
                e[field] = str(e.get(field, default))
                if not re.fullmatch(r'[A-Za-z0-9_@][A-Za-z0-9_.@-]*', e[field]):
                    raise ValueError(f'Invalid {field}')
            e['remote_port'] = int(e.get('remote_port', 8000))
            if not 1 <= e['remote_port'] <= 65535:
                raise ValueError('remote_port must be between 1 and 65535')
            key = f"ssh://{e['ssh_host']}/{e['remote_host']}:{e['remote_port']}"
        if key in seen:
            raise ValueError(f'Duplicate endpoint: {key}')
        seen.add(key)
        e['_key'] = key
        e['max_concurrency'] = int(e.get('max_concurrency', 1))
        if e['max_concurrency'] < 1:
            raise ValueError('max_concurrency must be positive')
        if not isinstance(e.get('enabled', True), bool):
            raise ValueError('enabled must be true or false')
        if not isinstance(e.get('revision', ''), (str, int, float)):
            raise ValueError('revision must be a string or number')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', e.get('api_key_env', 'LOCAL_API_KEY')):
            raise ValueError('api_key_env must name an environment variable')
        e['model'] = str(e.get('model', 'auto'))
        if not e['model']:
            raise ValueError('model must be auto or a served model ID')
        if 'roles' in e and (not isinstance(e['roles'], list) or not all(isinstance(r, str) for r in e['roles'])):
            raise ValueError('roles must be a list of role names')
        out.append(e)
    return out


def write_endpoints(path: Path, entries: list[dict]):
    """Validate then replace atomically so readers never see a partial update."""
    validate_endpoints({'endpoints': entries})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump({'endpoints': entries}, f, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class TunnelManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.processes = {}
        atexit.register(self.close)

    def url(self, entry: dict, timeout: float) -> str:
        if entry.get('url'):
            return entry['url']
        key = entry['_key']
        with self.lock:
            old = self.processes.get(key)
            if old and old[0].poll() is None:
                return old[1]
            if old:
                old[0].wait()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            url = f'http://127.0.0.1:{port}/v1'
            cmd = ['ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                   '-o', f'ConnectTimeout={max(1, int(timeout))}', '-o', 'ServerAliveInterval=15',
                   '-o', 'ServerAliveCountMax=2', '-L',
                   f"127.0.0.1:{port}:{entry['remote_host']}:{entry['remote_port']}", entry['ssh_host']]
            if sys.platform.startswith('linux'):
                parent = os.getpid()
                wrapper = ("import ctypes,os,signal,sys; "
                           "libc=ctypes.CDLL(None); "
                           "libc.prctl(1,signal.SIGTERM); "
                           "expected=int(sys.argv[1]); "
                           "sys.exit(1) if os.getppid()!=expected else None; "
                           "os.execvp(sys.argv[2],sys.argv[2:])")
                cmd = [sys.executable, '-c', wrapper, str(parent), *cmd]
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            except OSError as exc:
                raise LocalUnavailable('Could not start ssh; check that OpenSSH is installed') from exc
            self.processes[key] = (proc, url)
            # The first probe may race tunnel startup; normal retry handles that.
            return url

    def retain(self, keys):
        with self.lock:
            for key in list(self.processes):
                if key not in keys:
                    proc, _ = self.processes.pop(key)
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()

    def close(self):
        self.retain(set())
        atexit.unregister(self.close)


def _headers(entry, default_key):
    name = entry.get('api_key_env', 'LOCAL_API_KEY')
    key = os.environ.get(name) or default_key or 'EMPTY'
    return {'Authorization': 'Bearer ' + key}


def discover(url: str, headers: dict, timeout: float) -> list[str]:
    try:
        r = requests.get(url + '/models', headers=headers, timeout=timeout)
        if r.status_code != 200:
            raise LocalUnavailable(f'Model discovery returned HTTP {r.status_code}')
        ids = [m['id'] for m in r.json().get('data', []) if isinstance(m, dict) and isinstance(m.get('id'), str)]
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise LocalUnavailable('Model discovery failed') from exc
    if not ids:
        raise LocalUnavailable('Server has no loaded models')
    return ids


def completion(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    try:
        r = requests.post(url + '/chat/completions', headers=headers, json=payload, timeout=timeout)
        if r.status_code in (401, 403, 404, 408, 409, 429) or r.status_code >= 500:
            raise LocalUnavailable(f'Completion returned HTTP {r.status_code}')
        if r.status_code >= 400:
            # Do not echo response bodies, which can contain full study prompts.
            raise LocalRequestError(f'Local model rejected the request (HTTP {r.status_code}); '
                                    'check context/output limits, json_mode and extra_body settings')
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        raise LocalUnavailable('Completion connection failed or returned an invalid response') from exc


class LocalPool:
    def __init__(self, cfg: dict):
        c = cfg.get('local', {})
        self.path = endpoint_path(cfg).resolve()
        self.interval = max(0.01, float(c.get('poll_interval', 2)))
        self.cooldown = max(0.01, float(c.get('retry_interval', 5)))
        self.probe_timeout = max(0.01, float(c.get('probe_timeout', 5)))
        self.wait_timeout = c.get('wait_timeout')  # null: wait until nodes recover or the user cancels
        if self.wait_timeout is not None:
            self.wait_timeout = max(0, float(self.wait_timeout))
        self.condition = threading.Condition(threading.RLock())
        self.entries, self.states = [], {}
        self.raw = None
        self.warning_times = {}
        self.sequence = 0
        self.closed = False
        self.tunnels = TunnelManager()

    def _warn(self, message):
        now = time.monotonic()
        if now - self.warning_times.get(message, float('-inf')) >= 30:
            LOG.warning('[local] %s', message)
            self.warning_times[message] = now

    def _reload(self):
        try:
            raw = self.path.read_text()
            if raw == self.raw:
                return
            entries = validate_endpoints(yaml.safe_load(raw))
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            self._warn(f'Cannot read a valid endpoint list at {self.path}; retaining last valid list ({type(exc).__name__})')
            return
        self.raw, self.entries = raw, entries
        for e in entries:
            state = self.states.setdefault(e['_key'], {'active': 0, 'retry_at': 0, 'used': 0})
            state['retry_at'] = 0  # a deliberate configuration update can recover immediately
        self._cleanup()
        LOG.info('[local] Loaded %d endpoint(s) from %s', len(entries), self.path)

    def _cleanup(self):
        keep = {e['_key'] for e in self.entries if e.get('enabled', True)}
        keep |= {k for k, v in self.states.items() if v['active']}
        self.tunnels.retain(keep)
        self.states = {k: v for k, v in self.states.items() if k in keep}

    def snapshot(self):
        with self.condition:
            self._reload()
            return [dict(e) for e in self.entries]

    def run(self, role, configured_model, default_key, operation):
        """Lease one endpoint; retry only connectivity/availability failures."""
        started = time.monotonic()
        while True:
            with self.condition:
                if self.closed:
                    raise LocalUnavailable('Local pool closed')
                self._reload()
                now = time.monotonic()
                if self.wait_timeout is not None and now - started >= self.wait_timeout:
                    raise LocalUnavailable(f'Timed out waiting for local endpoints in {self.path}')
                ready = [e for e in self.entries if e.get('enabled', True)
                         and (not e.get('roles') or role in e['roles'])
                         and self.states[e['_key']]['active'] < e['max_concurrency']
                         and self.states[e['_key']]['retry_at'] <= now]
                if not ready:
                    self._warn(f'Waiting for an available endpoint for {role}; update {self.path} to change nodes')
                    self.condition.wait(self.interval)
                    continue
                e = min(ready, key=lambda x: (self.states[x['_key']]['active'] / x['max_concurrency'],
                                              self.states[x['_key']]['used']))
                state = self.states[e['_key']]
                state['active'] += 1
                self.sequence += 1
                state['used'] = self.sequence
            try:
                url = self.tunnels.url(e, self.probe_timeout)
                headers = _headers(e, default_key)
                models = discover(url, headers, self.probe_timeout)
                wanted = configured_model if configured_model not in (None, '', 'auto') else e['model']
                if wanted == 'auto':
                    if len(models) != 1:
                        raise LocalUnavailable('Multiple served models; set the endpoint model explicitly')
                    model = models[0]
                elif wanted in models:
                    model = wanted
                else:
                    raise LocalUnavailable(f'Requested model {wanted} is not served')
                return operation(e, url, headers, model)
            except LocalUnavailable as exc:
                with self.condition:
                    state['retry_at'] = time.monotonic() + self.cooldown
                    self._warn(f"{e['_key']}: {exc}; retrying available endpoints")
            finally:
                with self.condition:
                    state['active'] -= 1
                    self._cleanup()
                    self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.tunnels.close()


def cli_endpoints(args):
    from .config import load_config, load_secrets
    cfg = load_config(args.config)
    path = endpoint_path(cfg)
    if args.nodes is not None or args.urls is not None:
        entries = ([{'ssh_host': node, 'remote_host': args.remote_host, 'remote_port': args.remote_port,
                     'model': args.model, 'max_concurrency': args.slots} for node in args.nodes]
                   if args.nodes is not None else
                   [{'url': url, 'model': args.model, 'max_concurrency': args.slots} for url in args.urls])
        write_endpoints(path, entries)
        print(f'Updated {path} ({len(entries)} endpoints); running clients reload it automatically.')
    pool = LocalPool(cfg)
    try:
        entries = pool.snapshot()
        rows = []
        key = load_secrets(cfg['llm_defaults'].get('api_env'), cfg.get('_config_dir')).get('local_api_key')
        for e in entries:
            row = {k: v for k, v in e.items() if k != '_key'}
            if args.probe and e.get('enabled', True):
                try:
                    url = pool.tunnels.url(e, pool.probe_timeout)
                    # Allow an owned tunnel to finish its SSH handshake.
                    for attempt in range(3):
                        try:
                            row['served_models'] = discover(url, _headers(e, key), pool.probe_timeout)
                            break
                        except LocalUnavailable:
                            if attempt == 2:
                                raise
                            time.sleep(1)
                except LocalUnavailable as exc:
                    row['error'] = str(exc)
            rows.append(row)
        print(json.dumps({'file': str(path), 'endpoints': rows}, indent=2))
    finally:
        pool.close()
