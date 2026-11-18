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


def operation(name: str, stage: str, kind: str, reach: bool = False, category: str = "human_judgment", log: bool = True):
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
            depth = getattr(ctx.store, "op_depth", 0)   # an operation run by another one (decide_many: decide per study)
            ctx.store.op_depth = depth + 1
            try:
                with ctx.store.because(op=name):
                    res = fn(ctx, *a, **kw)
            except ValueError as e:
                res = {"ok": False, "message": str(e)}
            finally:
                ctx.store.op_depth = depth
            res = res if isinstance(res, dict) else {"ok": True, "result": res}
            res.setdefault("ok", True)
            res.setdefault("message", "done" if res["ok"] else "refused")
            if kind == "human" and _log and log:
                args = dict(zip(params, a)) | kw
                _log_decision(ctx, name, args, res)
                if res["ok"]:
                    note = str(args.get("note") or args.get("text") or "")[:300]
                    target = args.get("rid") or args.get("node") or args.get("target") or args.get("rids")
                    provenance.record(ctx, category, name, target=target if not isinstance(target, list) else None,
                                      rid=args.get("rid"), rids=args.get("rids"), note=note)
            if res["ok"] and kind != "read" and depth == 0:   # saved once, by the outermost operation
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
    from . import protocol, search, selection, extract, rob, synthesis, review, stage  # noqa: F401


def preview(ctx, name: str, args: dict | None = None) -> dict:
    """What an operation would do, before it runs (04 §5.2): a person's or deterministic operation is applied to a copy
    of the review in a scratch folder (nothing logged, no model call) and compared with the review as it is: the
    outputs it changes, what becomes out of date (and what a person's decision keeps), and the estimated cost of
    bringing it up to date. A model operation is not run: its reach is that of the output it recomputes."""
    import dataclasses
    import shutil
    import tempfile
    from pathlib import Path
    from .. import engine, impact
    op = get(name)
    args = dict(args or {})
    st = ctx.store
    base = {(i["node"], i["action"]) for i in engine.plan(ctx)["items"]}
    if name == "update_all":   # the update engine's whole plan: what it will do to each out-of-date output, and the cost
        p = engine.plan(ctx)
        items = [i for i in p["items"] if i["action"] not in ("render", "current")]
        by_type: dict[str, list] = {}
        for i in items:
            if i["action"] != "kept":
                by_type.setdefault(i["type"], []).append(i["node"])
        return {"op": name, "kind": op.kind, "runs_as_job": True, "ok": True, "changes": [], "items": items,
                "summary": engine._summary(items), "cost": engine._cost(ctx, items), "reach": {t: sorted(v) for t, v in by_type.items()},
                "kept": [i["node"] for i in items if i["action"] == "kept"], "lines": engine.describe({"items": items})[:30],
                "stages": impact.table(ctx, st, items=items, apply_items=True)}
    if name == "retrieve.process_added_files":
        from ..stages.fulltext import new_local_files
        files = new_local_files(ctx)
        return {"op": name, "kind": op.kind, "runs_as_job": True, "ok": True, "changes": [f"ft:{r}" for r in files],
                "items": [], "summary": {}, "cost": {}, "reach": {}, "kept": [], "lines": [], "stages": [],
                "message": f"{len(files)} new or changed file(s): {', '.join(files[:20])}" if files else "no new files in the full-text folder"}
    if op.kind == "llm" and not name.endswith(".rerun_all"):
        rid = args.get("rid")
        target = args.get("target") if name == "feedback" else None
        if target:                                   # study:<rid> or an output: the output the agent will look at
            from ..actions import feedback_target
            target = feedback_target(st, target) or target
        if target and target.startswith(("screen:", "elig:")):
            from ..selection import node_id
            target = node_id(st, target.split(":", 1)[1])
        node = args.get("node") or target or ({"extract": f"ext:{rid}", "rob": f"rob:{rid}"}.get(op.stage) or
                                              (f"elig:{rid}" if st.get(f"elig:{rid}") else f"screen:{rid}") if rid else None)
        pv = engine.preview(ctx, node) if node else {"items": [], "summary": {}, "cost": {}, "reach": {}, "lines": []}
        return {"op": name, "kind": op.kind, "runs_as_job": True, "changes": [node] if node else [], **pv,
                "stages": impact.table(ctx, st, changed=[node] if node else [], items=pv["items"],
                                       apply_note="the model's new answer, shown to you as a proposal to accept or reject")}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for f in ("protocol.yaml",):
            if (ctx.run_dir / f).exists():
                shutil.copy2(ctx.run_dir / f, tmp / f)
        sb_store = st.clone()
        sb_store.run_dir = tmp
        sb_store.copied_from = sb_store.copied_from | {tmp.resolve()}   # a preview is never saved (a large review: ~10 s each)
        sb = dataclasses.replace(ctx, store=sb_store, run_dir=tmp)
        fn = get(name.replace(".rerun_all", ".clear")).fn if name.endswith(".rerun_all") else op.fn
        res = fn(sb, _log=False, **{k: v for k, v in args.items() if not (name.endswith(".rerun_all") and k != "note")})
        d = sb_store.diff(st)

        def reach(mode=None):
            return [i for i in engine.plan(sb, reassess_excluded=mode)["items"] if (i["node"], i["action"]) not in base and i["action"] != "render"]

        new = reach()
        changed = sorted(set(d["new"]) | set(d["changed"]))
        stages = impact.table(ctx, st, sb_store, changed, new)
        modes = {}
        if name == "protocol.edit_criteria" and res.get("ok", True):    # the person chooses in the preview, seeing both
            by_mode = {m: reach(m) for m in ("all", "needed")}
            reads = {m: sum(i["action"] == "recompute" and i["type"] in ("screen", "eligibility") for i in its) for m, its in by_mode.items()}
            if reads["all"] != reads["needed"]:
                modes = {"selected": ctx.cfg["pipeline"]["reassess_excluded"],
                         **{m: {"reads": reads[m], "cost": engine._cost(ctx, its), "stages": impact.table(ctx, st, sb_store, changed, its)}
                            for m, its in by_mode.items()}}
    by_type: dict[str, list] = {}
    for i in new:
        by_type.setdefault(i["type"], []).append(i["node"])
    return {"op": name, "kind": op.kind, "runs_as_job": op.kind == "llm", "ok": res.get("ok", True), "message": res.get("message", ""),
            "changes": changed, "stages": stages, "items": new, "summary": engine._summary(new),
            "cost": engine._cost(ctx, new), "reach": {t: sorted(v) for t, v in by_type.items()},
            "kept": [i["node"] for i in new if i["action"] == "kept"], "lines": engine.describe({"items": new})[:30],
            **({"reassess_excluded": modes} if modes else {})}
