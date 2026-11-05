"""Operations: one function per thing a person or the agent can do (docs/refactor/04-refactor-plan.md §4, phase 3).

Every way into a review (the web interface, the To-do list, the command line, a job) calls these functions, so an
action has one implementation and behaves the same wherever it is started. The decorator gives each operation the
same handling:

  mode      kind="human": refused in the agent-only condition (provenance.require_judgment_allowed)
  log       kind="human": one line in human_decisions.jsonl and one provenance record, both named after the operation
  result    {"ok": bool, "message": str, ...}; a ValueError becomes ok=False with its message
  save      the store is saved after a successful operation
  job       kind="llm": `submit(ctx, name, args)` gives the job spec a caller hands to the job queue (the web
            interface never calls a model in its own thread); `run(ctx, name, args)` runs it here (CLI, jobs)

  sragent op --list            the operations, by stage
  sragent op NAME --args JSON  run one (CLI)
  POST /api/op {"name", "args"}
"""
from __future__ import annotations

import functools
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Callable

KINDS = ("human", "llm", "deterministic", "read")


@dataclass
class Op:
    name: str
    stage: str
    kind: str
    fn: Callable
    summary: str
    reach: bool = False                 # a change that can make other outputs out of date (preview before it)
    category: str = "human_judgment"    # provenance category of a human operation
    params: list[str] = field(default_factory=list)


REGISTRY: dict[str, Op] = {}


def operation(name: str, stage: str, kind: str, reach: bool = False, category: str = "human_judgment"):
    assert kind in KINDS, kind

    def deco(fn):
        assert name not in REGISTRY, f"operation {name} defined twice"
        params = [p for p in inspect.signature(fn).parameters if p != "ctx"]
        doc = (fn.__doc__ or "").strip().split("\n")[0]

        @functools.wraps(fn)
        def wrapper(ctx, *a, _log: bool = True, **kw):
            from .. import provenance
            if kind == "human" and category == "human_judgment":
                try:
                    provenance.require_judgment_allowed(ctx, name)
                except provenance.NotAllowed as e:
                    return {"ok": False, "message": str(e)}
            try:
                res = fn(ctx, *a, **kw)
            except ValueError as e:
                res = {"ok": False, "message": str(e)}
            res = res if isinstance(res, dict) else {"ok": True, "result": res}
            res.setdefault("ok", True)
            res.setdefault("message", "done" if res["ok"] else "refused")
            if kind == "human" and _log:
                args = dict(zip(params, a)) | kw
                _log_decision(ctx, name, args, res)
                if res["ok"]:
                    note = str(args.get("note") or args.get("text") or "")[:300]
                    target = args.get("rid") or args.get("node") or args.get("target") or args.get("rids")
                    provenance.record(ctx, category, name, target=target if not isinstance(target, list) else None,
                                      rid=args.get("rid"), rids=args.get("rids"), note=note)
            if res["ok"] and kind != "read":
                ctx.save()
            return res

        REGISTRY[name] = Op(name, stage, kind, wrapper, doc, reach, category, params)
        wrapper.op = REGISTRY[name]
        return wrapper
    return deco


def _log_decision(ctx, name: str, args: dict, res: dict) -> None:
    """One line per human operation: what was asked, with what, and whether it was applied. The shape the Activity
    page reads (todo_id, option) is kept: todo_id names the operation and its target, option its main value."""
    target = args.get("rid") or args.get("node") or args.get("target") or ",".join(args.get("rids") or []) or ""
    main = next((args[k] for k in ("decision", "option", "judgment") if args.get(k)), None)
    line = {"op": name, "todo_id": f"{name}:{target}" if target else name, "option": main or name.split(".")[-1],
            "args": {k: v for k, v in args.items() if k not in ("ctx",)}, "status": "applied" if res.get("ok") else "refused",
            "message": str(res.get("message", ""))[:300], "ts": time.time()}
    with open(ctx.run_dir / "human_decisions.jsonl", "a") as f:
        f.write(json.dumps(line, default=str) + "\n")


def get(name: str) -> Op:
    _load()
    if name not in REGISTRY:
        raise KeyError(f"unknown operation {name}; `sragent op --list` lists them")
    return REGISTRY[name]


def run(ctx, name: str, args: dict | None = None) -> dict:
    """Run an operation here (CLI, a job subprocess, tests)."""
    return get(name).fn(ctx, **(args or {}))


def submit(name: str, args: dict | None = None) -> dict:
    """The job spec for a model-calling operation: the web interface queues it (sragent op NAME in a subprocess)."""
    op = get(name)
    assert op.kind == "llm", f"{name} is not a model operation"
    return {"action": "op", "op": name, "args": args or {}}


def listing() -> list[dict]:
    _load()
    from ..stagespec import KEYS
    order = {k: i for i, k in enumerate(list(KEYS) + ["review"])}
    return [{"name": o.name, "stage": o.stage, "kind": o.kind, "reach": o.reach, "params": o.params, "summary": o.summary}
            for o in sorted(REGISTRY.values(), key=lambda o: (order.get(o.stage, 99), o.name))]


def _load() -> None:
    """Import the operation modules (they register themselves)."""
    from . import protocol, search, selection, extract, rob, synthesis, review  # noqa: F401
