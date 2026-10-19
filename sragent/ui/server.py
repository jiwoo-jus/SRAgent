"""Local web UI:  sragent ui --config configs/<run>.yaml   ->  http://127.0.0.1:8765

Standard library only (http.server). Long work (pipeline stages, PDF processing, feedback, evaluation)
runs as a background `sragent` subprocess, one job at a time; further jobs wait in a queue.
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
from urllib.parse import parse_qs, urlparse

import yaml

from ..config import is_benchmark, load_config, load_secrets, role_cfg
from ..context import Ctx

STATIC = Path(__file__).parent / "static"


class App:
    def __init__(self, config_path: str):
        self.config_path = str(Path(config_path).resolve())
        self.cfg = load_config(self.config_path)
        self.run_dir = Path(self.cfg["run_dir"]).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._ctx = None
        self._mtime = None
        self.job = None
        self.queue: list[dict] = []
        self.pending_answers: list[dict] = []
        self.jobs_file = self.run_dir / "ui_jobs" / "jobs.jsonl"
        self.history = self._load_jobs()
        self.catalog = self._load_catalog()
        threading.Thread(target=self._worker, daemon=True).start()

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
        ra = ctx.store.meta.get("rules_applied") or {}
        if ra.get("rules") == want and ra.get("version") == rules.RULES_VERSION:
            return
        changes = rules.apply(ctx)
        ctx.save()
        self.saved()
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
        return sorted(out, key=lambda j: j.get("created", 0))

    def _record(self, spec: dict):
        self.jobs_file.parent.mkdir(exist_ok=True)
        keep = {k: spec.get(k) for k in ("id", "action", "label", "rid", "target", "step", "status", "created", "started",
                                         "ended", "usd", "returncode", "cmd", "log", "error", "note", "text", "comment_id")}
        a = spec.get("agent") or {}
        keep["model"] = (a.get("model") if a else "") or role_cfg(self.cfg, "agent").get("model", "")
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
                "eval": f"Evaluation ({spec.get('suite', 'all')})"}.get(a, a)

    # ------------------------------------------------------------ catalog
    def _load_catalog(self) -> dict:
        """API models offered in the Run panel: every (provider, model, price) named in a model setup file
        (configs/models/*.yaml), with the roles that file gives it. Local models come from the endpoint list."""
        from ..config import deep_merge
        found: dict[tuple, dict] = {}
        for f in sorted(Path(self.cfg["models_dir"]).expanduser().glob("*.yaml")):
            setup = yaml.safe_load(open(f)) or {}
            base = setup.get("llm_defaults") or {}
            for role in ("agent", "judge", "reviewer", "judge2"):
                rc = deep_merge(base, (setup.get("roles") or {}).get(role) or {})
                if rc.get("provider") in (None, "local") or not rc.get("model"):
                    continue
                key = (rc["provider"], rc["model"])
                pr = rc.get("pricing") or {}
                m = found.setdefault(key, {"id": rc["model"], "provider": rc["provider"], "roles": [],
                                           "input_per_m": float(pr.get("input_per_m", 0)),
                                           "output_per_m": float(pr.get("output_per_m", 0)),
                                           "note": f"{role} in {f.name}"})
                if role not in m["roles"]:
                    m["roles"].append(role)
        return {"api": list(found.values())}

    def api_env_abs(self) -> str | None:
        rc = role_cfg(self.cfg, "agent")
        env = rc.get("api_env")
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
        cur = {r: {k: role_cfg(self.cfg, r).get(k) for k in ("provider", "model", "reasoning_effort")}
               for r in ("agent", "judge", "reviewer", "judge2")}
        settings = self._settings()
        return {"api": api, "local": self._local_endpoints(), "current": cur, "settings": settings,
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

    def role_override(self, sel: dict) -> dict:
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
        if not spec.get("agent"):
            spec.update({k: v for k, v in self._last_models().items() if k not in spec})
        self._record(spec)
        if spec.get("action") in ("run", "pdfs", "sync", "eval"):
            (self.run_dir / "ui_settings.json").write_text(json.dumps(
                {k: spec.get(k) for k in ("agent", "judge", "max_records", "until", "extra_budget", "suite")}, indent=1))
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
        roles = {}
        if spec.get("agent"):
            roles["agent"] = self.role_override(spec["agent"])
        if spec.get("judge"):
            roles["judge"] = self.role_override(spec["judge"])
        if roles:
            over["roles"] = roles
        if spec.get("max_records") not in (None, "", 0):
            over["pipeline"] = {"max_records": int(spec["max_records"])}
        extra = float(spec.get("extra_budget") or 2.0)
        over["budget_usd"] = round(self.spent() + extra, 4)
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
                if self.job is None and self.queue:
                    spec = self.queue.pop(0)
                    self.job = spec
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
                spec["status"] = "done" if rc == 0 else ("stopped" if spec.get("stop_requested") else "failed")
                spec["usd"] = round(self.spent() - spent0, 4)
            except Exception as e:
                spec["status"], spec["error"] = "failed", str(e)
            spec["ended"] = time.time()
            self._record(spec)
            with self.lock:
                self.history = self.history[-500:] + [spec]
                self.job = None
                self._ctx = None
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
                                          "usd", "returncode", "cmd", "error", "note", "text")} | \
                   {"model": ((j.get("agent") or {}).get("model") if isinstance(j.get("agent"), dict) else j.get("model"))
                             or role_cfg(self.cfg, "agent").get("model", "")}
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
        for key, spec in (("current", j), ("last", last)):
            if spec:
                tail = ""
                if spec.get("log") and Path(spec["log"]).exists():
                    tail = "\n".join(Path(spec["log"]).read_text(errors="ignore").splitlines()[-80:])
                out[key] = {k: spec.get(k) for k in ("id", "action", "label", "status", "cmd", "started", "ended", "usd",
                                                     "returncode", "target")} | {"log": tail}
        return out

    # ------------------------------------------------------------ answers
    def answer(self, ans: dict) -> dict:
        from .. import todos
        with self.lock:
            if self.job is not None:
                self.pending_answers.append(ans)
                todos.log_answer(self.ctx(), ans, "queued")
                return {"ok": True, "queued": True,
                        "message": "Saved. A job is running, so this is applied as soon as it finishes."}
            res = todos.apply(self.ctx(), ans)
            self.saved()
        if res.get("job"):
            self.submit({**self._last_models(), **res["job"]})
        return res

    def _drain_answers(self):
        from .. import todos
        if not self.pending_answers:
            return
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
            self.queue.append({**self._last_models(), **j, "id": uuid.uuid4().hex[:8], "status": "queued"})

    def _apply_op(self, ctx, op: dict) -> dict:
        from .. import actions, todos
        kind = op.get("_op", "answer")
        if kind == "edit":
            return actions.edit(ctx, op["edit"])
        if kind == "bulk":
            n = todos.bulk(ctx, op["kind"], op.get("ids", []), op.get("text", ""), op.get("items"))
            return {"ok": True, "message": f"{n} saved"}
        if kind == "resolve_comment":
            return {"ok": bool(actions.resolve_comment(ctx, op["id"], op.get("note", ""))), "message": "resolved"}
        if kind == "comment":
            c = actions.add_comment(ctx, op["target"], op["text"], op.get("send", False))
            if op.get("job_id"):
                c["job_id"] = op["job_id"]
                ctx.save()
            return {"ok": True, "message": f"comment {c['id']} saved", "comment": c}
        return todos.apply(ctx, op)

    def mutate(self, op: dict) -> dict:
        """Edits that change the store: applied now, or queued while a job is writing the store."""
        from .. import todos
        with self.lock:
            if self.job is not None:
                self.pending_answers.append({**op, "todo_id": op.get("todo_id", f"op:{op.get('_op')}")})
                todos.log_answer(self.ctx(), {**op, "todo_id": op.get("todo_id", f"op:{op.get('_op')}")}, "queued")
                return {"ok": True, "queued": True, "message": "Saved. A job is running, so this is applied as soon as it finishes."}
            r = self._apply_op(self.ctx(), op)
            self.saved()
            return r

    def comment(self, b: dict) -> dict:
        """Save a comment; with send=True also ask the agent (a feedback job, model from b['agent'])."""
        from .. import actions
        text = (b.get("text") or "").strip()
        if not text:
            return {"ok": False, "message": "empty comment"}
        target = b.get("target") or "review"
        job_id = None
        if b.get("send") and self.human_mode() == "agent_only":
            return {"ok": False, "message": "Agent-only mode: comments can be saved, but people may not send feedback "
                                            "to the agent (only requested full texts can be supplied)."}
        if b.get("send"):
            ft = actions.feedback_target(self.ctx().store, target)
            spec = self.submit({"action": "feedback", "target": ft, "text": text,
                                "rid": ft.split(":", 1)[1] if ft and ":" in ft else "",
                                **({"agent": b["agent"]} if b.get("agent") else {}),
                                **({"extra_budget": b["extra_budget"]} if b.get("extra_budget") else {})})
            job_id = spec["id"]
        r = self.mutate({"_op": "comment", "target": target, "text": text, "send": bool(b.get("send")), "job_id": job_id})
        if job_id:
            r["message"] = ("Sent to the agent (job " + job_id + "). It checks the comment against the source and proposes "
                            "a revision in the To-do list; progress is in Activity.")
        return r

    def human_mode(self) -> str:
        from .. import provenance
        return provenance.mode(self.ctx())

    def _last_models(self) -> dict:
        s = self._settings()
        return {k: s[k] for k in ("agent", "extra_budget") if s.get(k)}

    # ------------------------------------------------------------ state
    def state(self) -> dict:
        from .. import todos
        from ..stages.fulltext import fulltext_dir, needed_pdfs, new_local_files
        from .steps import steps
        ctx = self.ctx()
        tlist = todos.build(ctx) if ctx.store.get("protocol") else []
        if self.job is not None and self.job.get("action") in ("sync", "pdfs"):
            tlist = [t for t in tlist if t["kind"] != "sync"]   # already being done
        pend = {a["todo_id"] for a in self.pending_answers}
        for t in tlist:
            t["pending"] = t["id"] in pend
        nf = new_local_files(ctx) if ctx.store.get("protocol") else []
        return {"next": todos.next_action(ctx, tlist, self.job is not None, len(nf)),
                "ruled": todos.rule_exclusions(ctx),
                "run": {"name": self.run_dir.name, "run_dir": str(self.run_dir), "config": self.config_path,
                        "human_mode": self.human_mode(),
                        "spent_usd": round(self.spent(), 4), "topic": ctx.topic.get("question", "")},
                "steps": steps(ctx, tlist), "todos": tlist, "job": self.job_view(),
                "fulltext": {"dir": str(fulltext_dir(ctx)), "needed": needed_pdfs(ctx) if ctx.store.get("protocol") else [],
                             "new_files": nf}}

    def test_model(self, sel: dict) -> dict:
        """One tiny call (a few tokens; well under $0.001) to check the key, credit and model name."""
        from ..llm import LLM, CostTracker
        cfg = json.loads(json.dumps(self.cfg))
        cfg["llm_defaults"]["api_env"] = self.api_env_abs()
        cfg["roles"]["agent"] = self.role_override(sel)
        cfg["roles"]["agent"]["max_retries"] = 0
        cfg["local"]["wait_timeout"] = 30   # do not wait indefinitely for a local endpoint here
        llm = None
        try:
            llm = LLM(cfg, "agent", CostTracker(0.05, self.run_dir / "usage.jsonl"), None)
            t0 = time.time()
            out = llm.chat([{"role": "user", "content": 'Return the JSON {"ok": true}'}], tag="ui_test_model",
                           max_tokens=200, use_cache=False)
            return {"ok": bool(out.get("ok")), "seconds": round(time.time() - t0, 1), "model": llm.name}
        except Exception as e:
            return {"ok": False, "error": str(e)[:400]}
        finally:
            if llm is not None and llm.local_pool is not None:
                llm.local_pool.close()


# ---------------------------------------------------------------- HTTP
def make_handler(app: App):
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

        def _export(self, name: str, q: dict):
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

        def do_GET(self):
            u = urlparse(self.path)
            path, q = u.path, parse_qs(u.query)
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html")
                if path == "/api/state":
                    return self._send(200, app.state())
                if path == "/api/models":
                    return self._send(200, app.models())
                if path == "/api/job":
                    return self._send(200, app.job_view())
                if path == "/report":
                    from ..stages.report import report_html
                    ctx = app.ctx()
                    return self._send(200, report_html(ctx) if ctx.store.get("protocol") else "No review yet.", "text/html")
                if path == "/api/studies":
                    from ..studies import studies_list
                    return self._send(200, studies_list(app.ctx()))
                if path == "/api/study":
                    from ..studies import study_detail
                    return self._send(200, study_detail(app.ctx(), q.get("rid", [""])[0]))
                if path == "/api/jobs":
                    return self._send(200, app.jobs())
                if path == "/api/job_log":
                    return self._send(200, app.job_log(q.get("id", [""])[0]), "text/plain")
                if path == "/api/history":
                    from ..studies import history
                    return self._send(200, history(app.ctx(), actor=q.get("actor", [""])[0], q=q.get("q", [""])[0]))
                if path.startswith("/export/"):
                    return self._export(path[len("/export/"):], q)
                if path == "/needed_pdfs.csv":
                    from ..stages.fulltext import write_needed_pdfs
                    return self._send(200, write_needed_pdfs(app.ctx()).read_bytes(), "text/csv")
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                b = self._body()
                if path == "/api/estimate":
                    return self._send(200, app.estimate(b))
                if path == "/api/run":
                    return self._send(200, app.submit(b))
                if path == "/api/stop":
                    app.stop()
                    return self._send(200, {"ok": True})
                if path == "/api/answer":
                    return self._send(200, app.answer(b))
                if path == "/api/bulk":
                    return self._send(200, app.mutate({"_op": "bulk", **b}))
                if path == "/api/edit":
                    return self._send(200, app.mutate({"_op": "edit", "edit": b}))
                if path == "/api/comment":
                    return self._send(200, app.comment(b))
                if path == "/api/comment_resolve":
                    return self._send(200, app.mutate({"_op": "resolve_comment", **b}))
                if path in ("/api/rerun", "/api/propagate") and app.human_mode() == "agent_only":
                    return self._send(200, {"ok": False, "message": "Agent-only mode: re-runs and updates requested by a "
                                                                    "person are human interventions and are not allowed."})
                if path == "/api/rerun":
                    if b.get("step") not in ("screen", "extract", "rob") or not b.get("rid"):
                        return self._send(200, {"ok": False, "message": "choose a study and a step"})
                    spec = app.submit({"action": "rerun", "rid": b["rid"], "step": b["step"], "note": b.get("note", ""),
                                       "propagate": b.get("propagate", True),
                                       **({"agent": b["agent"]} if b.get("agent") else {}),
                                       **({"extra_budget": b["extra_budget"]} if b.get("extra_budget") else {})})
                    return self._send(200, {"ok": True, "job": spec["id"], "message":
                                            f"queued (job {spec['id']}); the result appears in the To-do list for approval"})
                if path == "/api/propagate":
                    spec = app.submit({"action": "propagate", "target": b["node"], "text": b.get("note", ""),
                                       **({"agent": b["agent"]} if b.get("agent") else {})})
                    return self._send(200, {"ok": True, "job": spec["id"], "message": f"queued (job {spec['id']})"})
                if path == "/api/cancel":
                    return self._send(200, {"ok": app.cancel(b.get("id", ""))})
                if path == "/api/test_model":
                    return self._send(200, app.test_model(b))
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._send(500, {"error": str(e)})
    return H


def serve(config_path: str, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    app = App(config_path)
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}"
    print(f"sragent UI for {app.run_dir.name}: {url}   (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
