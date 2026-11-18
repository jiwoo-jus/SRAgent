"""Local web UI:  sragent ui [--config configs/<run>.yaml]   ->  http://127.0.0.1:8765

Standard library only (http.server). Every run file under configs/ can be opened (`?config=<path>` in the
URL; the Configs page lists, edits and creates them); each open run file has its own App.
Long work (pipeline stages, PDF processing, feedback, evaluation) runs as a background `sragent` subprocess,
one job at a time across all run files; further jobs wait in a queue.
Answers given while a job is running are queued too and applied when it finishes, so a job can
never overwrite a human decision.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, parse_qs, urlparse

import yaml

from .. import stagespec, state
from ..config import EFFORTS, MODEL_STAGES, STAGE_LABELS, is_benchmark, load_config, load_secrets, model_for
from ..config_lock import ConfigChanged
from ..context import Ctx
from ..llm import BUDGET_STOP
from .configs import Library

STATIC = Path(__file__).parent / "static"
STAGE_JOBS = ("run", "pdfs", "sync", "reassess_rob")   # pipeline work: every stage uses its own models (models.<stage>.<role>)

_STARTED = time.time()
_CODE: tuple[float, list[str]] = (0.0, [])


def _restart_notice(edited: list[str]) -> str:
    """Said on the page itself, because a page built by old code looks like a page with wrong numbers."""
    from .. import figlib
    names = ", ".join(figlib.x(n) for n in edited[:6]) + (f" and {len(edited) - 6} more" if len(edited) > 6 else "")
    return (f"<div style='background:#fff4e5;border:1px solid #e0a800;border-radius:6px;padding:12px 16px;margin:0 0 24px'>"
            f"<b>This page was built by the code this interface started with, not the code on disk.</b> "
            f"{len(edited)} source file{'s were' if len(edited) > 1 else ' was'} edited since then ({names}). "
            f"Restart the interface to build it with the current code.</div>")


def code_changed() -> list[str]:
    """The package's own source files that were edited after this process started. Python keeps the modules it
    imported at startup, so from then on the interface computes everything with the old code while the pages,
    which are read from disk, look current. Only a restart picks the edits up."""
    global _CODE
    if time.time() - _CODE[0] < 5:
        return _CODE[1]
    root = Path(__file__).resolve().parent.parent
    try:
        names = sorted(str(f.relative_to(root)) for f in root.rglob("*.py") if f.stat().st_mtime > _STARTED)
    except OSError:
        names = _CODE[1]
    _CODE = (time.time(), names)
    return names


class App:
    slot = threading.Lock()   # one job at a time across every open run file
    running: "App | None" = None

    def __init__(self, config_path: str):
        self.config_path = str(Path(config_path).resolve())
        self.lock = threading.RLock()
        self.job = None
        self.busy = None    # a model call asked from a page (protocol revision, search suggestions) is running
        self.queue: list[dict] = []
        self.pending_answers: list[dict] = []
        self.stale = False
        self._load()
        threading.Thread(target=self._worker, daemon=True).start()

    def _load(self):
        files = self._config_files()
        self.cfg = load_config(self.config_path)
        self._cfg_mtimes = files
        self.run_dir = Path(self.cfg["run_dir"]).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._ctx = None
        self._mtime = None
        self.jobs_file = self.run_dir / "ui_jobs" / "jobs.jsonl"
        self.history = self._load_jobs()
        self.catalog = self._load_catalog()
        self.stale = False

    def _config_files(self) -> dict[str, float]:
        """The run file and every file it includes, with their modification times."""
        out: dict[str, float] = {}

        def walk(p: Path, depth: int = 0):
            if str(p) in out or depth > 8 or not p.is_file():
                return
            out[str(p)] = p.stat().st_mtime
            try:
                incs = (yaml.safe_load(p.read_text()) or {}).get("include") or []
            except (yaml.YAMLError, AttributeError):
                return
            for inc in incs if isinstance(incs, list) else []:
                walk((p.parent / str(inc)).resolve(), depth + 1)
        walk(Path(self.config_path))
        return out

    def config_changed(self) -> bool:
        """True when the run file or one of its includes was edited (in the UI or elsewhere) since loading."""
        return self.stale or self._config_files() != self._cfg_mtimes

    def idle(self) -> bool:
        return self.job is None and not self.queue and not self.pending_answers

    def reload(self):
        """After a config file was saved: re-read the run file (only between jobs)."""
        with self.lock:
            if self.idle():
                self._load()

    # ------------------------------------------------------------ context (reloaded when the store changes)
    def ctx(self) -> Ctx:
        with self.lock:
            sp = self.run_dir / "store.json"
            m = sp.stat().st_mtime if sp.exists() else None
            if self._ctx is None or m != self._mtime:
                self._ctx = Ctx.create(self.cfg, self.run_dir)
                self._ctx.quiet = True
                self._mtime = m
                self._auto_rules()
            return self._ctx

    def _auto_rules(self):
        """Apply the (deterministic, free) screening rules to an existing review once per rule set,
        so obvious exclusions never show up as human tasks. Never while a job is writing the store."""
        from .. import rules
        ctx = self._ctx
        if self.job is not None or not ctx.store.get("protocol") or not ctx.store.by_type("screen"):
            return
        want = rules.enabled_rules(ctx)
        ra = state.rules_applied(ctx.store) or {}
        if ra.get("rules") == want and ra.get("version") == rules.RULES_VERSION:
            return
        from .. import lease
        if not lease.try_acquire(self.run_dir):    # a job or a command is writing the review: next time
            return
        try:
            changes = rules.apply(ctx)
            ctx.save()
            self.saved()
        finally:
            lease.release(self.run_dir)
        ctx.log(f"[rules] applied {want}: {len(changes)} records excluded by rules")

    def saved(self):
        sp = self.run_dir / "store.json"
        self._mtime = sp.stat().st_mtime if sp.exists() else None

    # ------------------------------------------------------------ job records (global task history)
    def _load_jobs(self) -> list[dict]:
        if not self.jobs_file.exists():
            return []
        last = {}
        for line in self.jobs_file.read_text().splitlines():
            try:
                j = json.loads(line)
                last[j["id"]] = j
            except (ValueError, KeyError):
                pass
        out = list(last.values())
        for j in out:   # a UI restart while a job ran: the subprocess is gone
            if j.get("status") in ("running", "queued"):
                j["status"] = "interrupted"
        return sorted(out, key=lambda j: j.get("created") or 0)   # a job record without a timestamp sorts first

    def _record(self, spec: dict):
        self.jobs_file.parent.mkdir(exist_ok=True)
        keep = {k: spec.get(k) for k in ("id", "action", "label", "rid", "target", "step", "status", "created", "started",
                                         "ended", "usd", "returncode", "cmd", "log", "error", "note", "text", "comment_id")}
        a = spec.get("agent") or {}
        keep["model"] = self.stage_summary() if spec.get("action") in STAGE_JOBS else \
            ((a.get("model") if a else "") or model_for(self.cfg, None).get("model", ""))
        with open(self.jobs_file, "a") as f:
            f.write(json.dumps(keep) + "\n")

    @staticmethod
    def label_for(spec: dict) -> str:
        a = spec.get("action")
        if a == "run":
            return f"Run review up to {spec.get('until', 'report')}"
        if a == "rerun":
            from ..actions import RERUN_STEPS
            return f"Re-run {RERUN_STEPS.get(spec.get('step'), spec.get('step'))} for {spec.get('rid')}"
        if a == "propagate":
            return f"Update outputs after your edit of {spec.get('target')}"
        if a == "feedback":
            return f"Feedback on {spec.get('target') or 'the review'}: {(spec.get('text') or '')[:60]}"
        return {"pdfs": "Process added PDFs", "sync": "Update outputs after decisions",
                "reassess_rob": "Redraft every risk-of-bias assessment",
                "eval": f"Evaluation ({spec.get('suite', 'all')})"}.get(a, a)

    # ------------------------------------------------------------ catalog
    def _load_catalog(self) -> dict:
        """API models offered in the web interface: every model in the model catalog (configs/models.yaml),
        with its price. Local models come from the endpoint list."""
        uses = {}
        for s_ in MODEL_STAGES:
            for r in stagespec.roles_of(s_):
                uses.setdefault(model_for(self.cfg, s_, r).get("model"), []).append(f"{s_} {r}")
        api = []
        for name, e in (self.cfg.get("_catalog") or {}).items():
            if e.get("provider") == "local":
                continue
            pr = e.get("pricing") or {}
            api.append({"id": name, "provider": e["provider"], "roles": None,
                        "input_per_m": float(pr.get("input_per_m", 0)), "output_per_m": float(pr.get("output_per_m", 0)),
                        "note": ("used by this run file for " + ", ".join(uses[name]) if name in uses else "in the model catalog")
                                + (" · long prompts cost more" if pr.get("long_context") else "")})
        return {"api": api}

    def api_env_abs(self) -> str | None:
        env = self.cfg["llm_defaults"].get("api_env")
        if not env:
            return None
        cd = Path(self.cfg.get("_config_dir", "."))
        for c in (cd / env, Path.cwd() / env, Path(env).expanduser(), cd / Path(env).name, Path.cwd() / Path(env).name,
                  Path(self.config_path).parent / Path(env).name):
            if c.exists():
                return str(c.resolve())
        return None

    def models(self) -> dict:
        secrets = load_secrets(self.api_env_abs(), None)
        api = []
        for m in self.catalog.get("api", []):
            keys = {"openai": ["openai_api_key"], "gemini": ["gemini_api_key"],
                    "anthropic": ["anthropic_api_key", "claude_api_key"]}.get(m["provider"], [])
            api.append({**m, "key_available": any(secrets.get(k) for k in keys), "kind": "api"})
        cur = {r: {k: model_for(self.cfg, None, r).get(k) for k in ("provider", "model", "reasoning_effort")}
               for r in ("writer", "judge", "reviewer", "judge2")}
        settings = self._settings()
        return {"api": api, "local": self._local_endpoints(), "current": cur, "settings": settings,
                "models": self.model_rows(), "efforts": {k: list(v) for k, v in EFFORTS.items()},
                "benchmark": is_benchmark(self.cfg)}   # known answers: record sampling and evaluation

    def _local_endpoints(self) -> dict:
        """The live endpoint list (`sragent local-endpoints`). Listed, not probed: probing opens SSH tunnels."""
        from ..local import endpoint_path, validate_endpoints
        path = endpoint_path(self.cfg)
        try:
            entries = [e for e in validate_endpoints(yaml.safe_load(path.read_text())) if e.get("enabled", True)]
        except (OSError, ValueError, TypeError, yaml.YAMLError) as e:
            return {"file": str(path), "endpoints": 0, "models": [], "error": str(e)[:120]}
        return {"file": str(path), "endpoints": len(entries), "models": sorted({e["model"] for e in entries})}

    def _settings(self) -> dict:
        p = self.run_dir / "ui_settings.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def model_entry(self, sel: dict) -> dict:
        if sel.get("provider") == "local":
            return {"provider": "local", "id": sel["model"], "input_per_m": 0.0, "output_per_m": 0.0}
        for m in self.catalog.get("api", []):
            if m["id"] == sel.get("model"):
                return m
        return {"provider": sel.get("provider", "gemini"), "id": sel.get("model"), "input_per_m": 0, "output_per_m": 0}

    def model_cell(self, sel: dict) -> dict:
        """A model picked in the interface ({provider, model, effort}) as a model cell."""
        m = self.model_entry(sel)
        eff = sel.get("effort") or None
        if m["provider"] == "local":   # served by the endpoint pool; request options come from the config
            return {"provider": "local", "model": m["id"], "base_url": None, "reasoning_effort": None,
                    "pricing": {"input_per_m": 0.0, "output_per_m": 0.0}}
        return {"provider": m["provider"], "model": m["id"], "base_url": None,
                "reasoning_effort": None if eff in (None, "default") else eff, "extra_body": {},
                "pricing": {"input_per_m": m["input_per_m"], "output_per_m": m["output_per_m"]}}

    # ------------------------------------------------------------ estimate
    def estimate(self, opts: dict) -> dict:
        from ..estimate import estimate, history, remaining_calls
        ctx = self.ctx()
        action = opts.get("action", "run")
        if action == "run":
            calls = remaining_calls(ctx, opts.get("max_records") or None, opts.get("until", "report"))
        elif action == "pdfs":
            from ..stages.fulltext import new_local_files
            n = len(new_local_files(ctx))
            calls = {"screen_full_text": n, "extract": n, "rob": n, "feedback": n}
        elif action == "sync":
            from ..sync import pending_changes
            pc = pending_changes(ctx)
            calls = {"extract": len(pc["need_extract"]), "rob": len(pc["need_rob"]),
                     "feedback": len(pc["new_for_synthesis"]) + 2 * len(pc["excluded_but_cited"]),
                     "synthesis_verify": 3 if (pc["new_for_synthesis"] or pc["excluded_but_cited"]) else 0}
        elif action == "reassess_rob":
            import math
            from ..estimate import ROB_FLAG_RATE
            st = ctx.store
            n = sum(1 for r in st.included_ids() if st.get(f"ext:{r}")
                    and not (st.get(f"rob:{r}") and st.get(f"rob:{r}")["data"].get("human_confirmed")))
            calls = {"rob": n, "rob_verify": math.ceil(n * ROB_FLAG_RATE)
                     if (ctx.cfg["pipeline"].get("verify_rob") or "off") == "flagged" else 0}
        elif action == "feedback":
            calls = {"feedback": 3}
        elif action == "rerun":
            step = {"screen": "screen_full_text", "extract": "extract", "rob": "rob"}.get(opts.get("step"), "extract")
            calls = {step: 1, "feedback": 2 if opts.get("propagate", True) else 0}
        elif action == "propagate":
            calls = {"feedback": 2}
        else:  # eval: rough, from the v2 run
            per = {"all": 4.0, "e2e": 0.0, "screening": 0.0, "stagewise": 0.35, "grounding": 0.7, "synthesis": 0.55,
                   "claim1": 1.6, "claim2": 2.0, "judge": 0.5}
            return {"model": "mixed", "rows": [], "total_usd": per.get(opts.get("suite", "all"), 3.5),
                    "range_usd": [0, per.get(opts.get("suite", "all"), 3.5) * 2],
                    "note": "Rough estimate with fresh calls (Gemini Flash agent, Gemini Pro judge); suites that need the "
                            "stage-wise review also build it (about $0.35)."}
        if action in STAGE_JOBS or not opts.get("agent"):   # each kind of call with its stage's model
            from ..estimate import estimate_stages
            est = estimate_stages(calls, self.cfg, history(self.run_dir.parent), _sizes(self.ctx()))
            est["spent_so_far"] = round(self.spent(), 4)
            return est
        m = self.model_entry(opts.get("agent") or {})
        est = estimate(calls, m["id"], m, (opts.get("agent") or {}).get("effort"), history(self.run_dir.parent))
        est["spent_so_far"] = round(self.spent(), 4)
        return est

    def spent(self) -> float:
        """Review calls plus every evaluation run (each evaluation run keeps its own usage.jsonl)."""
        tot = 0.0
        for p in [self.run_dir / "usage.jsonl", *sorted((self.run_dir / "eval" / "runs").glob("*/usage.jsonl"))]:
            if not p.exists():
                continue
            for line in p.read_text().splitlines():
                try:
                    tot += float(json.loads(line).get("usd", 0))
                except ValueError:
                    pass
        return tot

    # ------------------------------------------------------------ jobs
    def submit(self, spec: dict) -> dict:
        spec = {**spec, "id": uuid.uuid4().hex[:8], "status": "queued", "created": time.time()}
        spec["label"] = spec.get("label") or self.label_for(spec)
        # a job carries a model only when the person picked one for that job; otherwise every call follows
        # models.<stage>.<role>. Spending follows the run file's budget: (no cap of its own per job).
        self._record(spec)
        if spec.get("action") in ("run", "pdfs", "sync", "eval"):
            (self.run_dir / "ui_settings.json").write_text(json.dumps(
                {k: spec.get(k) for k in ("agent", "judge", "max_records", "until", "suite")}, indent=1))
        with self.lock:
            self.queue.append(spec)
        return spec

    def _write_job_config(self, spec: dict) -> Path:
        from ..local import endpoint_path
        over: dict = {"include": [self.config_path], "run_dir": str(self.run_dir)}
        api_env = self.api_env_abs()
        over["llm_defaults"] = {"api_env": api_env}
        # this file lives in run_dir: keep the endpoint list of the original config directory
        over["local"] = {"endpoints_file": str(endpoint_path(self.cfg).resolve())}
        if spec.get("agent") and spec.get("action") not in STAGE_JOBS:
            # a model picked for one re-run / feedback / evaluation job: its writers; pipeline jobs follow models
            over["job_model"] = self.model_cell(spec["agent"])
        if spec.get("judge"):
            over["eval"] = {"models": {"judge": self.model_cell(spec["judge"])}}
        if spec.get("max_records") not in (None, "", 0):
            over["pipeline"] = {"max_records": int(spec["max_records"])}
        p = self.run_dir / "ui_job_config.yaml"
        p.write_text(yaml.safe_dump(over, sort_keys=False))
        return p

    def _cmd(self, spec: dict, cfgp: Path) -> list[str]:
        base = [sys.executable, "-m", "sragent.cli"]
        a = spec["action"]
        if a == "run":
            return base + ["run", "--config", str(cfgp), "--until", spec.get("until", "report")]
        if a == "pdfs":
            return base + ["pdfs", "--config", str(cfgp)]
        if a == "sync":
            return base + ["sync", "--config", str(cfgp)]
        if a == "reassess_rob":
            return base + ["reassess-rob", "--config", str(cfgp)]
        if a == "feedback":
            c = base + ["feedback", "--config", str(cfgp), "--text", spec["text"]]
            if spec.get("target"):
                c += ["--target", spec["target"]]
            return c + (["--no-verify"] if spec.get("no_verify") else [])
        if a == "rerun":
            c = base + ["rerun", "--config", str(cfgp), "--rid", spec["rid"], "--step", spec["step"]]
            if spec.get("note"):
                c += ["--note", spec["note"]]
            return c + ([] if spec.get("propagate", True) else ["--no-propagate"])
        if a == "propagate":
            c = base + ["propagate", "--config", str(cfgp), "--node", spec["target"]]
            if spec.get("from_version") is not None:
                c += ["--from-version", str(spec["from_version"])]
            return c + (["--note", spec["text"]] if spec.get("text") else [])
        if a == "op":
            return base + ["op", spec["op"], "--config", str(cfgp), "--args", json.dumps(spec.get("args") or {})]
        if a == "eval":
            c = base + ["eval", "--config", str(cfgp), "--suite", spec.get("suite", "all")]
            if spec.get("n_cases"):
                c += ["--n-cases", str(spec["n_cases"])]
            return c
        raise ValueError(a)

    def _worker(self):
        while True:
            spec = None
            with self.lock:
                if self.job is None and not self.busy and self.queue and App.slot.acquire(blocking=False):
                    spec = self.queue.pop(0)
                    self.job = spec
                    App.running = self
            if spec is None:
                time.sleep(0.5)
                continue
            logs = self.run_dir / "ui_jobs"
            logs.mkdir(exist_ok=True)
            spec["log"] = str(logs / f"{spec['id']}.log")
            try:
                cfgp = self._write_job_config(spec)
                cmd = self._cmd(spec, cfgp)
                spec["cmd"] = " ".join(cmd[2:])
                spec["status"], spec["started"] = "running", time.time()
                self._record(spec)
                spent0 = self.spent()
                with open(spec["log"], "w") as lf:
                    lf.write(f"$ sragent {' '.join(cmd[3:])}\n")
                    lf.flush()
                    proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=os.getcwd(),
                                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
                    spec["pid"] = proc.pid
                    rc = proc.wait()
                spec["returncode"] = rc
                spec["status"] = "done" if rc == 0 else ("stopped" if spec.get("stop_requested") else
                                                         "budget" if rc == BUDGET_STOP else "failed")
                if spec["status"] == "budget":   # which limit: the interface offers to raise it and resume
                    spec["budget_stop"] = self._budget_stop(spec["started"])
                spec["usd"] = round(self.spent() - spent0, 4)
            except Exception as e:
                spec["status"], spec["error"] = "failed", str(e)
            spec["ended"] = time.time()
            self._record(spec)
            with self.lock:
                self.history = self.history[-500:] + [spec]
                self.job = None
                self._ctx = None
                App.running = None
                App.slot.release()
                self._drain_answers()

    def stop(self):
        with self.lock:
            if self.job and self.job.get("pid"):
                self.job["stop_requested"] = True
                try:
                    os.kill(self.job["pid"], 15)
                except OSError:
                    pass
            for q in self.queue:
                q["status"] = "cancelled"
                self._record(q)
            self.queue.clear()

    def cancel(self, jid: str) -> bool:
        with self.lock:
            for q in list(self.queue):
                if q["id"] == jid:
                    self.queue.remove(q)
                    q["status"] = "cancelled"
                    q["ended"] = time.time()
                    self._record(q)
                    self.history.append(q)
                    return True
        return False

    def jobs(self) -> dict:
        def view(j):
            return {k: j.get(k) for k in ("id", "action", "label", "rid", "target", "status", "created", "started", "ended",
                                          "usd", "returncode", "cmd", "error", "note", "text", "budget_stop")} | \
                   {"model": ((j.get("agent") or {}).get("model") if isinstance(j.get("agent"), dict) else j.get("model"))
                             or model_for(self.cfg, None).get("model", "")}
        with self.lock:
            cur = [view(self.job)] if self.job else []
            q = [view(x) for x in self.queue]
            h = [view(x) for x in self.history if not self.job or x["id"] != self.job["id"]]
        return {"running": cur, "queued": q, "done": h[::-1][:300], "pending_answers": len(self.pending_answers)}

    def job_log(self, jid: str) -> str:
        for j in ([self.job] if self.job else []) + self.history:
            if j and j["id"] == jid and j.get("log") and Path(j["log"]).exists():
                return Path(j["log"]).read_text(errors="ignore")[-60000:]
        p = self.run_dir / "ui_jobs" / f"{jid}.log"
        return p.read_text(errors="ignore")[-60000:] if p.exists() else "(no log)"

    def job_view(self) -> dict:
        with self.lock:
            j = dict(self.job) if self.job else None
            last = (self.history or [None])[-1]
        out = {"current": None, "queued": len(self.queue), "last": None}
        other = App.running
        if other is not None and other is not self and self.queue:
            out["waiting_for"] = other.run_dir.name   # queued here; another run file's job holds the slot
        for key, spec in (("current", j), ("last", last)):
            if spec:
                tail = ""
                if spec.get("log") and Path(spec["log"]).exists():
                    tail = "\n".join(Path(spec["log"]).read_text(errors="ignore").splitlines()[-80:])
                out[key] = {k: spec.get(k) for k in ("id", "action", "label", "status", "cmd", "started", "ended", "usd",
                                                     "returncode", "target", "budget_stop")} | {"log": tail}
        if out["current"]:
            out["current"]["progress"] = self._progress(out["current"].get("started"))
        return out

    def _progress(self, job_started) -> dict | None:
        """The running job's current step, done of total (progress.json, written by Ctx.map); None before its first
        long step or when the file is from an earlier job."""
        try:
            p = json.loads((self.run_dir / "progress.json").read_text())
        except (OSError, ValueError):
            return None
        return p if job_started and p.get("started", 0) >= job_started - 1 else None

    # ------------------------------------------------------------ changes queued while the folder is busy
    def _drain_answers(self):
        """Apply the changes queued while the folder was busy (called with self.lock held)."""
        from .. import lease
        if not self.pending_answers or self.job is not None or not lease.try_acquire(self.run_dir):
            return
        try:
            self._drain_held()
        finally:
            lease.release(self.run_dir)

    def _drain_held(self):
        from .. import todos
        ctx = self.ctx()
        jobs = []
        for ans in self.pending_answers:
            try:
                r = self._apply_op(ctx, ans)
                if r.get("job"):
                    jobs.append(r["job"])
            except Exception as e:  # keep going; the answer is still in human_decisions.jsonl
                todos.log_answer(ctx, {**ans, "error": str(e)}, "failed")
        self.pending_answers = []
        self.saved()
        for j in jobs:
            self.queue.append({**j, "id": uuid.uuid4().hex[:8], "status": "queued"})

    def _apply_op(self, ctx, op: dict) -> dict:
        """Every change the interface makes is an operation (sragent/ops)."""
        from .. import ops
        return ops.run(ctx, op["name"], op.get("args") or {})

    def stage(self, key: str) -> dict:
        """GET /api/stage?key=: one stage page (stagepage.py)."""
        from .stagepage import stage_page
        try:
            with self.lock:
                j = self.jobs()
                return stage_page(self.ctx(), key, job=self.job, jobs=j["running"] + j["queued"] + j["done"])
        except (KeyError, ValueError) as e:
            return {"ok": False, "message": str(e)}

    def preview(self, b: dict) -> dict:
        """POST /api/preview {name, args}: what an operation would change and reach, before it runs (ops.preview)."""
        from .. import ops
        try:
            with self.lock:
                return {"ok": True, **ops.preview(self.ctx(), b.get("name") or "", b.get("args") or {})}
        except (KeyError, ValueError, TypeError) as e:
            return {"ok": False, "message": str(e)}

    def op(self, b: dict) -> dict:
        """POST /api/op {name, args}: a model operation becomes a job (no model call in a web request); any other is
        applied now (queued while a job writes the review); a read is answered now."""
        from .. import ops
        try:
            o = ops.get(b.get("name") or "")
        except KeyError as e:
            return {"ok": False, "message": str(e)}
        args = b.get("args") or {}
        if o.kind == "llm":
            spec = self.submit({**ops.submit(o.name, args), "label": f"{o.name} {json.dumps(args)[:60]}",
                                **({"agent": b["agent"]} if b.get("agent") else {})})
            return {"ok": True, "job": spec, "message": f"{o.name} queued as job {spec['id']}"}
        if o.kind == "read":
            with self.lock:
                return ops.run(self.ctx(), o.name, args)
        r = self.mutate({"_op": "op", "name": o.name, "args": args, "todo_id": f"op:{o.name}"})
        if r.get("ok") and r.get("job") and not r.get("queued"):   # e.g. feedback on a flag: the agent's part is a job
            spec = self.submit({**r["job"], **({"agent": b["agent"]} if b.get("agent") else {})})
            r = {**r, "job": spec}
        return r

    def mutate(self, op: dict) -> dict:
        """Edits that change the store: applied now, or queued while a job is writing the store."""
        from .. import todos
        from .. import lease
        with self.lock:
            if self.job is not None or not lease.try_acquire(self.run_dir):   # a job or a command holds the folder
                self.pending_answers.append({**op, "todo_id": op.get("todo_id", f"op:{op.get('_op')}")})
                todos.log_answer(self.ctx(), {**op, "todo_id": op.get("todo_id", f"op:{op.get('_op')}")}, "queued")
                return {"ok": True, "queued": True, "message": "Saved. The review is being written by a job or a command, "
                                                               "so this is applied as soon as it is free."}
            try:
                r = self._apply_op(self.ctx(), op)
                self.saved()
            finally:
                lease.release(self.run_dir)
            return r

    def data_items(self, b: dict, lib: Library) -> dict:
        """The extraction items and quality domains changed in the interface, at any stage: the topic file is
        rewritten in place, the changed review-defining setting is accepted (recorded), and the update engine then
        plans the extraction of new items for every included study ("Out-of-date outputs")."""
        import re as _re
        if b.get("op") != "save":
            return {"ok": False, "message": f"unknown op {b.get('op')}"}
        with self.lock:
            if not self.idle():
                return {"ok": False, "message": "A job is running or queued; change the data items when it has finished."}

        def clean(items, kinds):
            out, seen = [], set()
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                name = str(it.get("name") or "").strip()
                desc = str(it.get("description") or "").strip()
                if not _re.fullmatch(r"[a-z][a-z0-9_]*", name):
                    raise ValueError(f"item name '{name}': letters, digits and _ only, starting with a letter")
                if not desc:
                    raise ValueError(f"item '{name}' needs a description")
                if name in seen:
                    raise ValueError(f"item '{name}' is listed twice")
                seen.add(name)
                e = {"name": name, "description": desc}
                if kinds and it.get("kind") in ("drug", "disease"):
                    e["kind"] = it["kind"]
                was = next((f for f in self.cfg["topic"].get("extraction_fields") or [] if f.get("name") == name), None) if kinds else None
                if was and was.get("kind") == "rows":    # a rows item's columns are written in the topic file; kept as they are
                    e.update(kind="rows", columns=was["columns"])
                out.append(e)
            if not out:
                raise ValueError("at least one item is needed")
            return out
        try:
            fields = clean(b.get("fields"), True)
            domains = clean(b.get("domains"), False) if b.get("domains") is not None else None
        except ValueError as e:
            return {"ok": False, "message": str(e)}
        target = None
        for path in self._config_files():
            try:
                data = yaml.safe_load(Path(path).read_text()) or {}
            except yaml.YAMLError:
                continue
            if isinstance(data.get("topic"), dict) and "extraction_fields" in data["topic"]:
                target = Path(path)
                break
        if target is None:
            return {"ok": False, "message": "no file of this run defines topic.extraction_fields"}
        try:
            text = lib.set_topic_list(target.read_text(), "extraction_fields", fields)
            if domains is not None:
                text = lib.set_topic_list(text, "rob_domains", domains)
        except ValueError as e:
            return {"ok": False, "message": str(e)}
        r = lib.save(str(target), text)
        if not r.get("ok"):
            return r
        self.stale = True
        self.reload()
        from .. import config_lock
        try:
            ctx = Ctx.create(self.cfg, self.run_dir, check_config=False)
            ctx.quiet = True
            if config_lock.pending(self.cfg, self.run_dir):
                config_lock.accept(ctx, "data items or quality domains edited in the interface")
            self._ctx = None
        except ValueError as e:
            return {"ok": False, "message": str(e)}
        from ..sync import pending_changes
        pc = pending_changes(self.ctx()).get("stale") or {}
        n = (pc.get("summary") or {}).get("extraction:recompute", 0) + (pc.get("summary") or {}).get("rob:recompute", 0)
        return {"ok": True, "message": f"saved to {lib.rel(target)}" + (f"; {n} output(s) to bring up to date (Out-of-date outputs)" if n else ""),
                "pending": pc}

    def model_rows(self) -> list[dict]:
        """The Settings table: models.default, then each stage with model cells, a cell per role: what the run file
        writes (`set`, None = inherited), what it resolves to, and whether the stage has started (its cells fixed)."""
        from .. import config_lock
        try:
            started = set(config_lock.started_stages(self.ctx().store))
        except Exception:
            started = set()
        written = self.cfg.get("models") or {}

        def cell(stage, role):
            c = model_for(self.cfg, None if stage == "default" else stage, role)
            raw = (written.get(stage) or {}).get(role)
            raw = {"model": raw} if isinstance(raw, str) else dict(raw or {})
            setv = {k: raw[k] for k in ("model", "reasoning_effort") if k in raw} or None
            if setv and setv.get("model"):
                setv["provider"] = c.get("provider")   # the table matches options by model and provider
            return {"set": setv, "effective": {k: c.get(k) for k in ("provider", "model", "reasoning_effort")}}

        rows = [{"stage": "default", "label": "Default", "note": "every stage and role not set below", "started": False,
                 "cells": {r: cell("default", r) for r in stagespec.ROLES}}]
        for st in MODEL_STAGES:
            rows.append({"stage": st, "label": STAGE_LABELS[st], "started": st in started,
                         "cells": {r: cell(st, r) for r in stagespec.roles_of(st)}})
        return rows

    def stage_summary(self) -> str:
        ms = list(dict.fromkeys(
            f"{c.get('model')}" + (f" ({c.get('reasoning_effort')})" if c.get("reasoning_effort") else "")
            for c in (model_for(self.cfg, st) for st in MODEL_STAGES if st != stagespec.FEEDBACK)))
        return ms[0] if len(ms) == 1 else "per stage: " + ", ".join(ms)

    def save_models(self, cells: dict, lib: Library) -> dict:
        """Write the model table into the run file (`models:`). The cells of a stage that has started are fixed."""
        with self.lock:
            if not self.idle():
                return {"ok": False, "message": "A job is running or queued; change the models when it has finished."}
        try:
            p = lib.resolve(self.config_path)
        except ValueError:
            return {"ok": False, "message": "This run file is outside configs/; edit its models: section in the file."}
        catalog = self.cfg.get("_catalog") or {}
        new: dict = {}
        for key, sel in (cells or {}).items():
            stage, role = key.split(".", 1)
            if not sel:
                continue
            e = {}
            if sel.get("model"):
                e["model"] = sel["model"]
                if sel.get("provider") and (catalog.get(sel["model"]) or {}).get("provider") != sel["provider"]:
                    e["provider"] = sel["provider"]   # a local model: not in the catalog
            if "reasoning_effort" in sel:
                e["reasoning_effort"] = sel["reasoning_effort"]
            if e:
                new.setdefault(stage, {})[role] = e["model"] if list(e) == ["model"] else e
        if not (new.get("default") or {}).get("writer"):
            return {"ok": False, "message": "Choose the default writer: it is the model of every stage and role not set."}
        out = lib.set_block(p.read_text(), "models", new, "models.<stage>.<role> over models.default; a started stage's are fixed")
        chk = lib.check(self.config_path, out)
        if chk["errors"]:
            return {"ok": False, "message": "; ".join(chk["errors"])}
        locked = [c["key"] for x in chk.get("locked") or [] for c in x["changes"] if c["key"].startswith("models.")]
        if locked:
            return {"ok": False, "message": "These stages have started, so their models are fixed: " + ", ".join(locked)}
        r = lib.save(self.config_path, out, p.stat().st_mtime)
        if r.get("ok"):
            self.stale = True
        return {"ok": r.get("ok", False), "message": "Saved in " + lib.rel(p) if r.get("ok") else r.get("message")}

    def accept_config(self, reason: str) -> dict:
        """Accept edited review-defining settings on purpose (recorded in provenance.jsonl and the report)."""
        from .. import config_lock
        with self.lock:
            if not self.idle():
                return {"ok": False, "message": "A job is running or queued; accept the change when it has finished."}
            ctx = Ctx.create(self.cfg, self.run_dir, check_config=False)
            ctx.quiet = True
            try:
                ch = config_lock.accept(ctx, reason)
            except ValueError as e:
                return {"ok": False, "message": str(e)}
            self._ctx = None
        return {"ok": True, "changes": ch, "message": f"Accepted {len(ch)} changed setting(s); recorded in provenance.jsonl."}

    def human_mode(self) -> str:
        from .. import provenance
        return provenance.mode(self.ctx())

    # ------------------------------------------------------------ state
    # ------------------------------------------------------------ budget
    def budget_view(self) -> dict:
        """budget: as the page shows it: the total and each stage's limit (None: unlimited) with what was spent."""
        from ..config import budget_limits
        try:
            b = budget_limits(self.cfg)
        except ValueError as e:
            return {"error": str(e)}
        spent, by = 0.0, {}
        p = self.run_dir / "usage.jsonl"
        if p.exists():
            for line in p.read_text().splitlines():
                try:
                    row = json.loads(line)
                    usd = float(row.get("usd") or 0)
                except (ValueError, AttributeError):
                    continue
                spent += usd
                if row.get("stage"):
                    by[row["stage"]] = by.get(row["stage"], 0.0) + usd
        return {"total_usd": b["total_usd"], "spent_usd": round(spent, 4),
                "stages": [{"key": k, "label": stagespec.label(k), "limit_usd": v, "spent_usd": round(by.get(k, 0.0), 4)}
                           for k, v in b["stages"].items()]}

    def save_budget(self, b: dict, lib: "Library") -> dict:
        """Write budget: into the run file (it can change at any time; a running job keeps the limits it started with)."""
        from ..config import UNLIMITED, budget_limits
        try:
            p = lib.resolve(self.config_path)
        except ValueError:
            return {"ok": False, "message": "This run file is outside configs/; edit its budget: section in the file."}
        try:
            new = {"total_usd": float(b.get("total_usd")),
                   "stages": {k: (UNLIMITED if v in (None, "", UNLIMITED) else float(v))
                              for k, v in (b.get("stages") or {}).items()}}
            budget_limits({"budget": new})
        except (TypeError, ValueError) as e:
            return {"ok": False, "message": str(e)}
        new["total_usd"] = int(new["total_usd"]) if new["total_usd"] == int(new["total_usd"]) else new["total_usd"]
        new["stages"] = {k: (int(v) if isinstance(v, float) and v == int(v) else v) for k, v in new["stages"].items()}
        out = lib.set_block(p.read_text(), "budget", new, "USD limits of this run folder: the total, and each stage "
                            "that calls a model (a number or unlimited)", flow=False)
        chk = lib.check(self.config_path, out)
        if chk["errors"]:
            return {"ok": False, "message": "; ".join(chk["errors"])}
        r = lib.save(self.config_path, out, p.stat().st_mtime)
        if r.get("ok"):
            self.stale = True
        return {"ok": r.get("ok", False), "message": "Saved in " + lib.rel(p) if r.get("ok") else r.get("message")}

    def _budget_stop(self, since: float | None) -> dict | None:
        """The limit that stopped the job (budget_stop.json, written by CostTracker.check) when it is this job's."""
        try:
            d = json.loads((self.run_dir / "budget_stop.json").read_text())
        except (OSError, ValueError):
            return None
        if since and d.get("ts", 0) < since - 1:
            return None
        if d.get("stage"):
            d["label"] = stagespec.label(d["stage"])
        return d

    def resume(self, jid: str) -> dict:
        """Run a job stopped by a limit again (after the limit was raised): it continues where it stopped."""
        with self.lock:
            old = next((j for j in reversed(self.history) if j.get("id") == jid), None)
        if old is None:
            return {"ok": False, "message": "That job is no longer in the list; start it again from its stage."}
        keep = {k: v for k, v in old.items() if k not in ("id", "status", "created", "started", "ended", "usd", "returncode",
                                                          "cmd", "log", "pid", "error", "budget_stop", "stop_requested")}
        spec = self.submit(keep)
        return {"ok": True, "message": f"{spec['label']} queued again", "job": spec}

    def report_pages(self, config: str = "") -> tuple[str, dict]:
        """The report as the interface serves it: one page, or a main page and appendix pages when it is large
        (report.split_report). Built once per version of the store."""
        from urllib.parse import quote
        from ..stages.report import _with_meta, place_links, report_html, split_report
        ctx = self.ctx()
        key = (self._mtime, config)
        if getattr(self, "_report", (None,))[0] != key:
            main, parts = split_report(_with_meta(ctx, report_html(ctx)))
            q = "?config=" + quote(config) if config else ""
            part = lambda n: f"/report_parts/{n}{q}"
            self._report = (key, place_links(main, f"/report{q}", part), {n: place_links(t, f"/report{q}", part) for n, t in parts.items()})
        return self._report[1], self._report[2]

    def manuscript_page(self) -> str:
        """The report for readers, built from the review's report_plan.yaml (free, no model call), or the reason it
        cannot be built. Rebuilt whenever the store changed, so it always shows the current data."""
        from .. import figlib, reportplan
        ctx = self.ctx()
        page = lambda b: f"<!doctype html><meta charset='utf-8'><title>Report for readers</title><body style='font:15px/1.6 Helvetica,Arial,sans-serif;margin:40px;max-width:46em'>{b}</body>"
        pp = reportplan.plan_path(ctx)
        if not pp.exists():
            return page(f"<h1>No report for readers</h1><p>This review has no <code>report_plan.yaml</code> beside its run file "
                        f"(<code>{figlib.x(str(pp))}</code>). The verification report is under Output \u00b7 Report.</p>")
        edited = code_changed()
        key = (self._mtime, pp.stat().st_mtime, tuple(edited))   # the data, the plan itself, and the code that reads them
        if getattr(self, "_ms", (None,))[0] != key:
            r = reportplan.render(ctx)
            html = Path(r["path"]).read_text() if r.get("ok") else page(
                "<h1>The report plan has problems</h1><ul>" + "".join(f"<li>{figlib.x(p)}</li>" for p in r["problems"]) + "</ul>")
            if edited:
                html = html.replace("<body>", "<body>" + _restart_notice(edited), 1)
            self._ms = (key, html)
        return self._ms[1]

    def run_estimate(self, op: str, args: dict) -> dict:
        """Before a model job starts: what it may cost, by the stages whose limits apply, and whether that fits.
        `<stage>.run` (run up to that stage) is estimated from the work left; other jobs say only what room is left."""
        from ..estimate import ROW_CELL, estimate_stages, history, remaining_calls
        from .. import ops
        bv = self.budget_view()
        out = {"op": op, "budget": bv, "known": False}
        if bv.get("error"):
            return out
        try:
            o = ops.get(op)
        except KeyError:
            o = None
        until = {"protocol.draft": "protocol", "synthesis.write": "report"}.get(op) or \
            (o.stage if o is not None and op.endswith(".run") and o.stage in stagespec.BY_KEY else None)
        if until:   # a stage's Run: the pipeline up to that stage
            ctx = self.ctx()
            calls = remaining_calls(ctx, None, until)
            est = estimate_stages(calls, self.cfg, history(self.run_dir.parent), _sizes(self.ctx()))
            per: dict[str, float] = {}
            for r in est["rows"]:
                k = ROW_CELL.get(r["stage"], ("feedback", "writer"))[0]
                per[k] = per.get(k, 0.0) + r["usd"]
            out.update(known=True, calls=sum(calls.values()), total_usd=est["total_usd"], range_usd=est["range_usd"])
        elif op == "eligibility.verify":   # a second reading of full-text decisions: one verifier call per study
            from ..stages.screening import verify_todo
            ctx = self.ctx()
            calls = {"screen_verify": len(verify_todo(ctx.store, args.get("mode") or "pending", args.get("rids") or None))}
            est = estimate_stages(calls, self.cfg, history(self.run_dir.parent), _sizes(self.ctx()))
            per = {"eligibility": est["total_usd"]}
            out.update(known=True, calls=calls["screen_verify"], total_usd=est["total_usd"], range_usd=est["range_usd"])
        elif op in ("extract.read_figures", "extract.studies"):
            ctx = self.ctx()
            if op == "extract.read_figures":      # one call per figure with rows still to read
                from .. import figures
                calls = {"extract_figures": figures.pending(ctx, args.get("rids") or None)["figures"]}
            else:
                calls = {"extract": len([r for r in args.get("rids") or [] if r in set(ctx.store.included_ids())])}
            est = estimate_stages(calls, self.cfg, history(self.run_dir.parent), _sizes(ctx))
            per = {"extract": est["total_usd"]}
            out.update(known=True, calls=sum(calls.values()), total_usd=est["total_usd"], range_usd=est["range_usd"])
        if out["known"]:
            out["stages"] = []
            for st in bv["stages"]:
                if st["key"] in per:
                    room = None if st["limit_usd"] is None else round(st["limit_usd"] - st["spent_usd"], 4)
                    out["stages"].append({**st, "usd": round(per[st["key"]], 3), "room_usd": room,
                                          "fits": room is None or per[st["key"]] <= room})
        out["room_total_usd"] = round(bv["total_usd"] - bv["spent_usd"], 4)
        out["fits_total"] = (not out["known"]) or out["total_usd"] <= out["room_total_usd"]
        return out

    def state(self) -> dict:
        from .. import todos
        from ..stages.fulltext import fulltext_dir, needed_pdfs, new_local_files
        from .steps import steps
        with self.lock:
            self._drain_answers()      # changes queued while a command (outside the interface) held the folder
        ctx = self.ctx()
        tlist = todos.build(ctx) if ctx.store.get("protocol") else []
        if self.job is not None and self.job.get("action") in ("sync", "pdfs"):
            tlist = [t for t in tlist if t["kind"] != "sync"]   # already being done
        pend = {a["todo_id"] for a in self.pending_answers}
        for t in tlist:
            t["pending"] = t["id"] in pend
        nf = new_local_files(ctx) if ctx.store.get("protocol") else []
        sync_t = next((t for t in tlist if t["kind"] == "sync"), None)
        from .. import graph
        stale = graph.stale_summary(ctx.store) if ctx.store.get("protocol") else {"n": 0, "by_type": {}, "pinned": 0}
        stale["est_usd"] = None
        if sync_t:
            import re as _re
            m = _re.search(r"\$([0-9.]+)", sync_t["question"])
            stale["est_usd"] = float(m.group(1)) if m else None
        from ..stages.report import open_flags
        of = open_flags(ctx) if ctx.store.get("protocol") else {"n": 0, "by_source": {}}
        return {"next": todos.next_action(ctx, tlist, self.job is not None, len(nf)),
                "code_changed": code_changed(), "stale": stale, "open_flags": {"n": of["n"], "by_source": of["by_source"]},
                "ruled": todos.rule_exclusions(ctx),
                "run": {"name": self.run_dir.name, "run_dir": str(self.run_dir), "config": self.config_path,
                        "config_rel": getattr(self, "config_rel", self.config_path), "benchmark": is_benchmark(self.cfg),
                        "human_mode": self.human_mode(),
                        "spent_usd": round(self.spent(), 4), "topic": ctx.topic.get("question", ""),
                        "manuscript": (Path(self.cfg["_config_dir"]) / "report_plan.yaml").exists()},
                "steps": steps(ctx, tlist), "todos": tlist, "job": self.job_view(), "budget": self.budget_view(),
                "registry": [{"key": s.key, "label": s.label, "llm": s.llm} for s in stagespec.STAGES],
                "fulltext": {"dir": str(fulltext_dir(ctx)), "needed": needed_pdfs(ctx) if ctx.store.get("protocol") else [],
                             "new_files": nf}}

    def test_model(self, sel: dict) -> dict:
        """One tiny call (a few tokens; well under $0.001) to check the key, credit and model name."""
        from ..llm import LLM, CostTracker
        cfg = json.loads(json.dumps(self.cfg))
        cfg["llm_defaults"]["api_env"] = self.api_env_abs()
        cfg["job_model"] = {**self.model_cell(sel), "max_retries": 0}
        cfg["local"]["wait_timeout"] = 30   # do not wait indefinitely for a local endpoint here
        llm = None
        try:
            from ..config import budget_limits   # within the run's total (a tracker of its own would count past spending)
            llm = LLM(cfg, model_for(cfg, None), CostTracker(budget_limits(cfg), self.run_dir / "usage.jsonl"), None)
            t0 = time.time()
            out = llm.chat([{"role": "user", "content": 'Return the JSON {"ok": true}'}], tag="ui_test_model",
                           max_tokens=200, use_cache=False)
            return {"ok": bool(out.get("ok")), "seconds": round(time.time() - t0, 1), "model": llm.name}
        except Exception as e:
            return {"ok": False, "error": str(e)[:400]}
        finally:
            if llm is not None and llm.local_pool is not None:
                llm.local_pool.close()


# ---------------------------------------------------------------- workspace (every run file under configs/)
class Workspace:
    """The run files the UI can open, each with its own App (made on first use), and the config files."""

    def __init__(self, config_path: str | None, configs_dir: str | None = None):
        if not configs_dir and config_path:   # the configs/ folder the run file lives in, else ./configs
            parts = Path(config_path).resolve().parts
            if "configs" in parts:
                configs_dir = str(Path(*parts[:len(parts) - parts[::-1].index("configs")]))
        self.lib = Library(configs_dir or "configs")
        self.apps: dict[str, App] = {}
        self.lock = threading.Lock()
        self.default = str(Path(config_path).resolve()) if config_path else None
        if self.default:
            self.get(None)   # fail at startup, as before, if the given run file does not load

    def _path(self, cfg: str | None) -> Path | None:
        if not cfg:
            return Path(self.default) if self.default else None
        p = Path(cfg).expanduser()
        p = (p if p.is_absolute() else self.lib.base / p).resolve()
        if self.default and str(p) == self.default:
            return p
        p = self.lib.resolve(str(p))
        if self.lib.kind_of(p) != "run":
            raise ValueError(f"{self.lib.rel(p)} is not a run file (configs/<review|benchmark>/<topic>/<run>.yaml)")
        return p

    def get(self, cfg: str | None) -> App | None:
        p = self._path(cfg)
        if p is None:
            return None
        with self.lock:
            app = self.apps.get(str(p))
            if app is None:
                app = self.apps[str(p)] = App(str(p))
                app.config_rel = self.lib.rel(p)
        if app.config_changed():
            app.reload()   # raises when the edited file no longer loads; the error is shown with a link to fix it
        return app

    def changed(self):
        """A config file was saved or created: open run files re-read their config before the next use."""
        with self.lock:
            for a in self.apps.values():
                a.stale = True

    def configs(self, current: str | None) -> dict:
        out = self.lib.listing()
        live = {a.config_rel: a for a in self.apps.values()}
        cur = None
        try:
            p = self._path(current)
            cur = self.lib.rel(p) if p else None
        except ValueError:
            pass
        for r in out["runs"]:
            a = live.get(r["path"])
            r["current"] = r["path"] == cur
            r["job"] = ("running" if a.job else "queued") if a and (a.job or a.queue) else None
        if cur and not any(r["path"] == cur for r in out["runs"]):   # a run file outside configs/
            try:
                a = self.get(current)
                out["runs"].insert(0, {"path": cur, "kind": "", "topic": "", "name": Path(cur).stem, "current": True,
                                       "agent": "{provider}:{model}".format(**model_for(a.cfg, None)),
                                       "human_mode": a.cfg["pipeline"]["human_mode"], "question": (a.cfg.get("topic") or {}).get("question", ""),
                                       "status": "", "run_dir": str(a.run_dir), "outside": True})
            except Exception:
                pass
        out["current"] = cur
        out["default"] = self.lib.rel(self.default) if self.default else None
        try:
            out["catalog_models"] = self.lib.catalog_models()
        except ValueError:
            out["catalog_models"] = []
        return out


# ---------------------------------------------------------------- HTTP
def _sizes(ctx) -> dict:
    """Input tokens per call measured on this review (estimate.sizes); {} when it cannot be measured."""
    from ..estimate import sizes
    try:
        return sizes(ctx)
    except Exception:
        return {}


def make_handler(ws: Workspace):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (json.dumps(body, default=str).encode()
                                                          if ctype == "application/json" else body.encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _export(self, app: App, name: str, q: dict):
            from .. import studies
            ctx = app.ctx()
            run = app.run_dir.name
            if name == "studies.csv":
                rows = studies.studies_list(ctx)
                grp = q.get("group", [""])[0]
                rows = [r for r in rows if not grp or r["group"] == grp]
                return self._download(studies.to_csv(rows), f"{run}_studies.csv", "text/csv")
            if name == "review.json":
                d = studies.review_export(ctx, with_text=q.get("text", ["0"])[0] == "1")
                return self._download(json.dumps(d, indent=1, default=str), f"{run}_review.json", "application/json")
            if name == "history.csv":
                h = studies.history(ctx, limit=10**9)["changelog"]
                rows = [{**e, "changes": "; ".join(e["changes"]),
                         "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"]))} for e in h]
                return self._download(studies.to_csv(rows), f"{run}_history.csv", "text/csv")
            if name == "jobs.csv":
                j = app.jobs()
                return self._download(studies.to_csv(j["running"] + j["queued"] + j["done"]), f"{run}_llm_jobs.csv", "text/csv")
            if name.startswith("study/"):
                rid, _, ext = name[len("study/"):].rpartition(".")
                d = studies.study_detail(ctx, rid, with_text=True)
                if ext == "md":
                    return self._download(studies.study_markdown(d), f"{rid}.md", "text/markdown")
                return self._download(json.dumps(d, indent=1, default=str), f"{rid}.json", "application/json")
            return self._send(404, {"error": "unknown export"})

        def _download(self, text: str, fname: str, ctype: str):
            data = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _app(self, q: dict) -> App:
            app = ws.get(q.get("config", [None])[0])
            if app is None and len(ws.apps) == 1:       # a link without ?config= while one run is open: that run
                app = next(iter(ws.apps.values()))
            if app is None:
                raise LookupError("no run file is open: choose one on the Configs page")
            return app

        def do_GET(self):
            u = urlparse(self.path)
            path, q = u.path, parse_qs(u.query)
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html")
                if path.startswith("/static/"):          # the page's stylesheet and modules
                    f = (STATIC / path[len("/static/"):]).resolve()
                    if STATIC.resolve() not in f.parents or not f.is_file():
                        return self._send(404, {"error": "not found"})
                    ctype = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
                    return self._send(200, f.read_bytes(), ctype)
                if path == "/api/configs":
                    return self._send(200, ws.configs(q.get("config", [None])[0]))
                if path == "/api/config_file":
                    return self._send(200, ws.lib.read(q.get("path", [""])[0]))
                if path.startswith("/files/fulltext/"):
                    from ..stages.fulltext import fulltext_dir
                    app = self._app(parse_qs(u.query))
                    base = fulltext_dir(app.ctx()).resolve()
                    target = (base / unquote(path[len("/files/fulltext/"):])).resolve()
                    if base not in target.parents or not target.is_file():
                        return self._send(404, {"error": "no such file"})
                    ctype = "application/pdf" if target.suffix.lower() == ".pdf" else "text/plain; charset=utf-8"
                    body = target.read_bytes()
                    self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Disposition", f"inline; filename=\"{target.name}\""); self.end_headers()
                    return self.wfile.write(body)
                if path == "/api/state":
                    cfg_q = q.get("config", [None])[0]
                    try:
                        app = self._app(q)
                        return self._send(200, app.state())
                    except LookupError:
                        return self._send(200, {"no_config": True})
                    except ConfigChanged as e:   # a started review whose fixed settings were edited
                        rel = ws.lib.rel(ws._path(cfg_q))
                        return self._send(200, {"config_error": str(e), "config_changed": e.changes, "config": rel})
                    except Exception as e:
                        return self._send(200, {"config_error": f"{type(e).__name__}: {e}"[:600],
                                                "config": cfg_q or (ws.lib.rel(ws.default) if ws.default else None)})
                app = self._app(q)
                if path == "/api/models":
                    return self._send(200, app.models())
                if path == "/api/job":
                    return self._send(200, app.job_view())
                if path == "/manuscript":
                    return self._send(200, app.manuscript_page(), "text/html")
                if path == "/report" or path.startswith("/report_parts/"):
                    ctx = app.ctx()
                    if not ctx.store.get("protocol"):
                        return self._send(200, "No review yet.", "text/html")
                    main, parts = app.report_pages((q.get("config") or [""])[0])
                    if path == "/report":
                        return self._send(200, main, "text/html")
                    name = path[len("/report_parts/"):]
                    return self._send(200, parts[name], "text/html") if name in parts else self._send(404, {"error": "not found"})
                if path == "/api/studies":
                    from ..studies import studies_list
                    return self._send(200, studies_list(app.ctx()))
                if path == "/api/study":
                    from ..studies import study_detail
                    return self._send(200, study_detail(app.ctx(), q.get("rid", [""])[0]))
                if path == "/api/jobs":
                    return self._send(200, app.jobs())
                if path == "/api/impact":
                    from .. import engine
                    return self._send(200, engine.preview(app.ctx(), q.get("node", [""])[0]))
                if path == "/api/ops":
                    from .. import ops
                    return self._send(200, {"ops": ops.listing()})
                if path == "/api/stage":
                    return self._send(200, app.stage(q.get("key", [""])[0]))
                if path == "/api/needs_you":
                    with app.lock:
                        from .stagepage import needs_you
                        return self._send(200, needs_you(app.ctx()))
                if path == "/api/job_log":
                    return self._send(200, app.job_log(q.get("id", [""])[0]), "text/plain")
                if path == "/api/history":
                    from ..studies import history
                    return self._send(200, history(app.ctx(), actor=q.get("actor", [""])[0], q=q.get("q", [""])[0]))
                if path.startswith("/export/"):
                    return self._export(app, path[len("/export/"):], q)
                if path.startswith("/meta/"):       # pooled results written by synthesis.meta_analysis: plots, tables
                    name = path[len("/meta/"):]
                    f = app.run_dir / "meta" / name
                    import re as _re
                    if not _re.fullmatch(r"[A-Za-z0-9_.-]+\.(svg|csv|md|json)", name) or not f.exists():
                        return self._send(404, {"error": "not found"})
                    ctype = {"svg": "image/svg+xml", "csv": "text/csv", "md": "text/markdown", "json": "application/json"}[name.rsplit(".", 1)[1]]
                    return self._send(200, f.read_bytes(), ctype)
                if path == "/meta_rows.csv":
                    from ..rows import export_csv
                    return self._send(200, export_csv(app.ctx())[0].read_bytes(), "text/csv")
                if path == "/needed_pdfs.csv":
                    from ..stages.fulltext import write_needed_pdfs
                    return self._send(200, write_needed_pdfs(app.ctx()).read_bytes(), "text/csv")
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        def _configs_post(self, path: str, b: dict) -> dict:
            lib = ws.lib
            if path == "/api/config_check":
                return lib.check(b.get("path", ""), b.get("text", ""))
            if path == "/api/config_save":
                r = lib.save(b.get("path", ""), b.get("text", ""), b.get("mtime"), bool(b.get("create")))
            elif path == "/api/config_new":
                r = lib.new_topic(b) if b.get("what") == "topic" else lib.new_run(b)
            elif path == "/api/config_duplicate":
                r = lib.duplicate(b)
            else:
                return {"error": "not found"}
            if r.get("ok"):
                ws.changed()
            return r

        def do_POST(self):
            u = urlparse(self.path)
            path = u.path
            try:
                b = self._body()
                if path == "/api/models":
                    r = self._app(parse_qs(u.query)).save_models(b.get("cells") or {}, ws.lib)
                    if r.get("ok"):
                        ws.changed()
                    return self._send(200, r)
                if path == "/api/config_accept":
                    return self._send(200, self._app(parse_qs(u.query)).accept_config(b.get("reason", "")))
                if path.startswith("/api/config_"):
                    try:
                        return self._send(200, self._configs_post(path, b))
                    except ValueError as e:
                        return self._send(200, {"ok": False, "message": str(e)})
                app = self._app(parse_qs(u.query))
                if path == "/api/estimate":
                    return self._send(200, app.estimate(b))
                if path == "/api/run":
                    return self._send(200, app.submit(b))
                if path == "/api/stop":
                    app.stop()
                    return self._send(200, {"ok": True})
                if path == "/api/op":
                    return self._send(200, app.op(b))
                if path == "/api/preview":
                    return self._send(200, app.preview(b))
                if path == "/api/resolve":       # which operation a To-do answer stands for
                    from .. import todos
                    op = todos.to_operation(b)
                    return self._send(200, {"ok": True, "name": op[0], "args": op[1]} if op else
                                      {"ok": False, "message": f"unknown answer {b.get('todo_id')} -> {b.get('option')}"})
                if path == "/api/data_items":
                    r = app.data_items(b, ws.lib)
                    if r.get("ok"):
                        ws.changed()
                    return self._send(200, r)
                if path == "/api/budget":
                    r = app.save_budget(b, ws.lib)
                    if r.get("ok"):
                        ws.changed()
                    return self._send(200, r)
                if path == "/api/job_resume":
                    return self._send(200, app.resume(b.get("id", "")))
                if path == "/api/run_estimate":
                    return self._send(200, app.run_estimate(b.get("op", ""), b.get("args") or {}))
                if path == "/api/cancel":
                    return self._send(200, {"ok": app.cancel(b.get("id", ""))})
                if path == "/api/test_model":
                    return self._send(200, app.test_model(b))
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._send(500, {"error": str(e)})
    return H


def serve(config_path: str | None, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          configs_dir: str | None = None):
    ws = Workspace(config_path, configs_dir)
    srv = ThreadingHTTPServer((host, port), make_handler(ws))
    url = f"http://{host}:{port}"
    app = ws.get(None)
    print(f"sragent UI for {app.run_dir.name if app else 'the run files in ' + ws.lib.rel(ws.lib.root) + '/'}: {url}"
          "   (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
