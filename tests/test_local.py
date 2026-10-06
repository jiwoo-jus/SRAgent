"""Local serving behavior without GPUs, SSH sessions, or external requests."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import threading
import time

import pytest
import requests
import yaml

from sragent.config import load_config
from sragent.llm import LLMHub
from sragent import local


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


def reply(model, value='ok'):
    return Response({'model': model, 'choices': [{'message': {'content': json.dumps({'value': value})}}],
                     'usage': {'prompt_tokens': 12, 'completion_tokens': 4, 'total_tokens': 16}})


@pytest.fixture
def rig(tmp_path, monkeypatch):
    path = tmp_path / 'endpoints.yaml'
    local.write_endpoints(path, [{'url': 'http://a:8000/v1'}])
    cfg = load_config(None, {'cache_dir': str(tmp_path / 'cache'),
        'llm_defaults': {'provider': 'local', 'model': 'auto', 'api_env': None, 'max_retries': 1},
        'local': {'endpoints_file': str(path), 'poll_interval': .01, 'retry_interval': .03,
                  'probe_timeout': .1, 'wait_timeout': 2}})
    models, calls = {'a': 'model-A', 'b': 'model-B', 'c': 'model-C'}, []

    def get(url, **kwargs):
        host = url.split('//')[1].split(':')[0]
        return Response({'data': [{'id': models[host]}]})

    def post(url, json, **kwargs):
        calls.append((url, json.copy()))
        return reply(json['model'])

    monkeypatch.setattr(local.requests, 'get', get)
    monkeypatch.setattr(local.requests, 'post', post)
    hub = LLMHub(cfg, tmp_path / 'run')
    yield hub, path, models, calls
    hub.close()


MSG = [{'role': 'user', 'content': 'Return JSON'}]


def test_live_nodes_models_cache_and_usage(rig):
    hub, path, models, calls = rig
    agent = hub['agent']
    assert agent.chat(MSG) == {'value': 'ok'}
    agent.chat(MSG)
    assert len(calls) == 1
    models['a'] = 'replacement-model'
    agent.chat(MSG)
    assert calls[-1][1]['model'] == 'replacement-model' and len(calls) == 2
    local.write_endpoints(path, [{'url': 'http://b:8000/v1'}])
    agent.chat(MSG)
    assert calls[-1][0].startswith('http://b:')
    logs = [json.loads(x) for x in hub.tracker.log_path.read_text().splitlines()]
    assert [r['model'] for r in logs] == ['model-A', 'model-A', 'replacement-model', 'model-B']
    assert logs[-1]['endpoint'] == 'http://b:8000/v1'
    assert all(r['usd'] == 0 and r['provider'] == 'local' for r in logs)


def test_cache_revision_and_output_limit(rig):
    hub, path, _, calls = rig
    agent = hub['agent']
    agent.chat(MSG, max_tokens=100)
    agent.chat(MSG, max_tokens=200)
    local.write_endpoints(path, [{'url': 'http://a:8000/v1', 'revision': 'new-weights'}])
    agent.chat(MSG, max_tokens=200)
    assert len(calls) == 3


def test_failure_routes_to_healthy_endpoint(rig, monkeypatch):
    hub, path, _, calls = rig
    local.write_endpoints(path, [{'url': 'http://a:8000/v1'}, {'url': 'http://b:8000/v1'}])
    old_post = local.requests.post
    def post(url, **kwargs):
        if url.startswith('http://a:'):
            raise requests.ConnectionError('node allocation ended')
        return old_post(url, **kwargs)
    monkeypatch.setattr(local.requests, 'post', post)
    assert hub['agent'].chat(MSG) == {'value': 'ok'}
    assert calls[-1][0].startswith('http://b:')


def test_all_down_then_new_node_recovers_same_call(rig, monkeypatch):
    hub, path, _, calls = rig
    attempted = threading.Event()
    old_get = local.requests.get
    def get(url, **kwargs):
        if url.startswith('http://a:'):
            attempted.set()
            raise requests.ConnectionError('node ended')
        return old_get(url, **kwargs)
    monkeypatch.setattr(local.requests, 'get', get)
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(hub['agent'].chat, MSG)
        assert attempted.wait(1)
        local.write_endpoints(path, [{'url': 'http://c:8000/v1'}])
        assert f.result(timeout=2) == {'value': 'ok'}
    assert calls[-1][1]['model'] == 'model-C'


def test_invalid_update_retains_last_valid_list(rig):
    hub, path, _, calls = rig
    agent = hub['agent']
    agent.chat(MSG)
    path.write_text('endpoints: [')
    agent.chat(MSG, use_cache=False)
    assert len(calls) == 2
    local.write_endpoints(path, [{'url': 'http://b:8000/v1'}])
    agent.chat(MSG)
    assert calls[-1][1]['model'] == 'model-B'


def test_empty_list_waits_and_can_be_repopulated(rig):
    hub, path, _, _ = rig
    agent = hub['agent']
    local.write_endpoints(path, [])
    with ThreadPoolExecutor(max_workers=1) as ex:
        f = ex.submit(agent.chat, MSG)
        time.sleep(.03)
        assert not f.done()
        local.write_endpoints(path, [{'url': 'http://b:8000/v1'}])
        assert f.result(timeout=2) == {'value': 'ok'}


def test_shared_role_concurrency_and_distribution(rig, monkeypatch):
    hub, path, _, _ = rig
    local.write_endpoints(path, [{'url': 'http://a:8000/v1'}, {'url': 'http://b:8000/v1'}])
    active, peak, seen = {}, {}, set()
    lock = threading.Lock()
    def post(url, json, **kwargs):
        with lock:
            active[url] = active.get(url, 0) + 1
            peak[url] = max(peak.get(url, 0), active[url])
            seen.add(url)
        time.sleep(.02)
        with lock:
            active[url] -= 1
        return reply(json['model'])
    monkeypatch.setattr(local.requests, 'post', post)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(hub['agent' if i % 2 else 'judge'].chat, MSG, use_cache=False) for i in range(12)]
        assert all(f.result(timeout=2) == {'value': 'ok'} for f in futures)
    assert len(seen) == 2 and max(peak.values()) == 1
    assert hub['agent'].local_pool is hub['judge'].local_pool


def test_removed_node_drains_inflight_request(rig, monkeypatch):
    hub, path, _, calls = rig
    entered, release = threading.Event(), threading.Event()
    old_post = local.requests.post
    def post(url, **kwargs):
        if url.startswith('http://a:'):
            entered.set()
            assert release.wait(1)
        return old_post(url, **kwargs)
    monkeypatch.setattr(local.requests, 'post', post)
    with ThreadPoolExecutor(max_workers=2) as ex:
        first = ex.submit(hub['agent'].chat, MSG, use_cache=False)
        assert entered.wait(1)
        local.write_endpoints(path, [{'url': 'http://b:8000/v1'}])
        second = ex.submit(hub['agent'].chat, MSG, use_cache=False)
        assert second.result(timeout=1) == {'value': 'ok'}
        release.set()
        assert first.result(timeout=1) == {'value': 'ok'}
    assert {c[1]['model'] for c in calls} == {'model-A', 'model-B'}


def test_role_routing_and_model_pin(rig):
    hub, path, _, calls = rig
    local.write_endpoints(path, [{'url': 'http://a:8000/v1', 'roles': ['judge']},
                                 {'url': 'http://b:8000/v1', 'roles': ['agent']}])
    hub.cfg['roles']['agent'] = {'model': 'model-B'}
    hub['agent'].chat(MSG)
    hub['judge'].chat(MSG)
    assert [p['model'] for _, p in calls] == ['model-B', 'model-A']


def test_permanent_bad_request_is_not_retried_forever(rig, monkeypatch):
    hub, _, _, _ = rig
    monkeypatch.setattr(local.requests, 'post', lambda *a, **k: Response({}, 400))
    with pytest.raises(local.LocalRequestError, match='context/output limits'):
        hub['agent'].chat(MSG)


def test_bounded_wait(rig):
    hub, path, _, _ = rig
    local.write_endpoints(path, [])
    hub.cfg['local']['wait_timeout'] = .03
    with pytest.raises(local.LocalUnavailable, match='Timed out'):
        hub['agent'].chat(MSG)


def test_config_include_keeps_endpoint_path(tmp_path):
    configs = tmp_path / 'configs'
    configs.mkdir()
    backend = configs / 'backend.yaml'
    backend.write_text('local:\n  endpoints_file: endpoints.yaml\n')
    review = configs / 'review.yaml'
    review.write_text('include: [backend.yaml]\n')
    job = tmp_path / 'generated_job.yaml'
    job.write_text(yaml.safe_dump({'include': [str(review)]}))
    assert local.endpoint_path(load_config(job)) == configs / 'endpoints.yaml'


def test_atomic_writer_rejects_invalid_without_replacing_file(tmp_path):
    path = tmp_path / 'endpoints.yaml'
    local.write_endpoints(path, [{'url': 'http://a:8000/v1'}])
    original = path.read_bytes()
    with pytest.raises(ValueError):
        local.write_endpoints(path, [{'ssh_host': '-bad-host'}])
    assert path.read_bytes() == original
    with pytest.raises(ValueError):
        local.write_endpoints(path, [{'url': 'http://a:8000/v1', 'max_concurrency': 0}])


def test_managed_tunnel_is_reused_restarted_and_closed(monkeypatch):
    processes, commands = [], []
    class Proc:
        returncode = None
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9
    def popen(cmd, **kwargs):
        commands.append(cmd)
        proc = Proc()
        processes.append(proc)
        return proc
    monkeypatch.setattr(local.subprocess, 'Popen', popen)
    manager = local.TunnelManager()
    entry = local.validate_endpoints({'endpoints': [{'ssh_host': 'compute-node', 'remote_host': '127.0.0.4'}]})[0]
    url = manager.url(entry, 1)
    assert manager.url(entry, 1) == url and len(processes) == 1
    assert commands[0][-1] == 'compute-node'
    assert any('127.0.0.4:8000' in part for part in commands[0])
    assert 'BatchMode=yes' in commands[0] and 'StrictHostKeyChecking=yes' in commands[0]
    processes[0].returncode = 255
    manager.url(entry, 1)
    assert len(processes) == 2
    manager.retain(set())
    assert processes[-1].returncode == 0
    manager.close()


@pytest.mark.parametrize('count', [1, 12])
def test_one_or_twelve_endpoints(rig, count):
    hub, path, models, calls = rig
    models.update({f'node{i}': f'model-{i}' for i in range(count)})
    local.write_endpoints(path, [{'url': f'http://node{i}:8000/v1'} for i in range(count)])
    for _ in range(count * 2):
        hub['agent'].chat(MSG, use_cache=False)
    assert len({url for url, _ in calls}) == count


def test_empty_pool_cancellation_wakes_waiters(rig):
    hub, path, _, _ = rig
    hub.cfg['local']['wait_timeout'] = None
    local.write_endpoints(path, [])
    agent = hub['agent']
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(agent.chat, MSG)
        time.sleep(.02)
        hub.close()
        with pytest.raises(local.LocalUnavailable, match='closed'):
            future.result(timeout=1)


def test_json_repair_keeps_request_metadata(rig, monkeypatch):
    hub, _, _, _ = rig
    seen = []
    def post(url, json, **kwargs):
        seen.append(json.copy())
        r = reply(json['model'])
        if len(seen) == 1:
            r.data['choices'][0]['message']['content'] = 'not JSON'
        return r
    monkeypatch.setattr(local.requests, 'post', post)
    assert hub['agent'].chat(MSG) == {'value': 'ok'}
    assert len(seen) == 2 and len(seen[1]['messages']) == 3
    assert hub.tracker.calls == 2


def test_local_template_options_and_endpoint_auth(rig, monkeypatch):
    hub, path, _, calls = rig
    local.write_endpoints(path, [{'url': 'http://a:8000/v1', 'api_key_env': 'TEST_LOCAL_KEY'}])
    monkeypatch.setenv('TEST_LOCAL_KEY', 'test-only-secret')
    hub.cfg['llm_defaults']['extra_body'] = {'chat_template_kwargs': {'enable_thinking': False}}
    old_post = local.requests.post
    def post(url, headers, **kwargs):
        assert headers['Authorization'] == 'Bearer test-only-secret'
        return old_post(url, headers=headers, **kwargs)
    monkeypatch.setattr(local.requests, 'post', post)
    hub['agent'].chat(MSG)
    assert calls[-1][1]['chat_template_kwargs'] == {'enable_thinking': False}
    assert 'test-only-secret' not in hub.tracker.log_path.read_text()


def test_multiple_advertised_models_need_selection(rig, monkeypatch):
    hub, path, _, calls = rig
    monkeypatch.setattr(local.requests, 'get', lambda *a, **k: Response({'data': [{'id': 'one'}, {'id': 'two'}]}))
    local.write_endpoints(path, [{'url': 'http://a:8000/v1', 'model': 'two'}])
    hub['agent'].chat(MSG)
    assert calls[-1][1]['model'] == 'two'

