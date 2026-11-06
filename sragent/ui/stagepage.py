"""One page per stage (04 §5.1): the same template for all ten, built from the graph (no model calls).

stage_page(ctx, key) ->
  header     key, label, order, status (not started | done | out of date | needs you), counts, models, spent
  toolbar    the five buttons with the same meaning on every stage: run pending, re-run all (fresh), clear, verify,
             stop; each {op, label, enabled, reason, count, preview}; a disabled one says why
  inputs     what the stage's outputs depend on, and how many are out of date because of each
  dependents the stages that use this one's outputs
  items      one row per output (study, criterion, statement, ...): what it says, who decided, out of date or not,
             notes, and the operations a person can apply to that row ({op, args, label})
  needs_you  the rows that wait for a person (the same rows, filtered)
needs_you(ctx) -> the review's waiting items grouped by stage, each a link to its row on the stage page.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .. import attention, graph, selection, stagespec, state
from ..ops.stage import CLEARABLE, NOT_CLEARABLE
from ..store import record_of

RUN_OP = {"protocol": "protocol.draft", "search": "search.run", "screen": "screen.run", "retrieve": "retrieve.run",
          "eligibility": "eligibility.run", "snowball": None, "extract": "extract.run", "rob": "rob.run",
          "synthesis": "synthesis.write", "report": "report.render"}
VERIFY_OP = {"screen": ("screen.verify", "Verify exclusions"), "eligibility": ("eligibility.verify", "Verify undecided"),
             "rob": ("rob.verify", "Verify flagged")}
TYPES = {"protocol": ("protocol", "criterion", "approval"), "search": ("strategy", "search"), "screen": ("screen",),
         "retrieve": ("fulltext", "pdf_request"), "eligibility": ("eligibility",), "snowball": ("snowball",),
         "extract": ("extraction",), "rob": ("rob",), "synthesis": ("synthesis", "synthesis_scope"), "report": ("report",)}


def _title(st, rid: str) -> str:
    r = record_of(st, rid)
    return (r.get("title") or "(no title)")[:160]


def _row(st, rid: str, derived: dict, node: dict | None, **cols) -> dict:
    n = node or {}
    notes = [f["text"] for f in attention.open_flags(st, n, derived)] if n else []
    stale = graph.stale_reasons(st, n["id"]) if n and n.get("status") == "active" else []
    return {"id": n.get("id") or rid, "rid": rid, "title": _title(st, rid) if rid else "", "stale": [f"{r['dep']} {r['why']}" for r in stale],
            "notes": notes, **cols}


def _items(ctx, key: str, derived: dict) -> list[dict]:
    st = ctx.store
    rows = []
    if key == "protocol":
        appr = state.approval(st)
        for n in st.by_type("criterion"):
            c = n["data"]
            rows.append({"id": n["id"], "rid": "", "title": c.get("text", ""), "status": n["status"], "decision": c.get("type"),
                         "by": c.get("stage", "title_abstract"), "stale": [], "notes": [], "actions": []})
        rows.insert(0, {"id": "approval:protocol", "rid": "", "title": "Protocol approval", "decision": "approved" if appr else "not approved",
                        "by": "person" if appr else "", "stale": [], "notes": [], "status": "active",
                        "actions": [] if appr else [{"op": "protocol.approve", "args": {}, "label": "Approve"}]})
    elif key == "search":
        m = state.strategy(st)
        for v in m.get("variants") or []:
            rows.append({"id": f"variant:{v.get('id')}", "rid": "", "title": v.get("name", ""), "decision": str(v.get("count")),
                         "by": "agent", "detail": v.get("query", ""), "stale": [], "notes": [],
                         "actions": [] if st.get("search") else [{"op": "search.confirm_strategy", "args": {"query": v.get("query", "")}, "label": "Confirm"}]})
    elif key == "screen":
        for n in st.by_type("screen"):
            rid = n["id"].split(":", 1)[1]
            d = n["data"]
            by = "rule" if d.get("rule") else "person" if selection.human_decided(d) else "agent"
            acts = [{"op": "screen.recompute", "args": {"rid": rid}, "label": "Re-run"}]
            if d.get("rule") and d.get("decision") == "exclude":
                acts = [{"op": "screen.restore_rule_exclusion", "args": {"rids": [rid]}, "label": "Restore"}]
            rows.append(_row(st, rid, derived, n, decision=d.get("decision") if n["status"] == "active" else "cleared",
                             by=by, because=d.get("because") or [], status=n["status"], actions=acts))
    elif key == "retrieve":
        for rid, v in selection.screened(st):
            if not (selection.forwarded(st, rid) or v.get("stage") in ("retrieval", "full_text")):
                continue
            ft = st.get(f"ft:{rid}")
            req = state.pdf_request(st, rid)
            kind = (ft or {}).get("data", {}).get("kind")
            status = "full text" if kind == "fulltext" else ("not obtainable" if req.get("unavailable") else
                                                            "requested" if req.get("wanted") else v.get("decision") if v.get("stage") == "retrieval" else "not retrieved yet")
            acts = []
            if kind != "fulltext" and not req.get("unavailable"):
                acts = [{"op": "retrieve.declare_unobtainable", "args": {"rids": [rid]}, "label": "Not obtainable"},
                        {"op": "retrieve.decide_without", "args": {"rids": [rid]}, "label": "Decide without"}]
            rows.append(_row(st, rid, derived, ft, decision=status, by=str((ft or {}).get("data", {}).get("origin", "")),
                             status=(ft or {}).get("status", ""), actions=acts))
    elif key == "eligibility":
        for rid, v in selection.screened(st):
            if not selection.forwarded(st, rid) and not st.get(f"elig:{rid}"):
                continue
            el = st.get(f"elig:{rid}")
            d = (el or {}).get("data") or {}
            by = "person" if d.get("human_decision") else d.get("decided_by") or ("agent" if el else "")
            acts = [{"op": "eligibility.decide", "args": {"rid": rid, "decision": x}, "label": x.capitalize()}
                    for x in ("include", "exclude") if v.get("decision") != x]
            acts.append({"op": "eligibility.recompute", "args": {"rid": rid}, "label": "Re-run"})
            rows.append(_row(st, rid, derived, el, decision=v.get("decision"), by=by, because=v.get("because") or [],
                             status=(el or {}).get("status", ""), actions=acts))
    elif key == "snowball":
        for r in (state.snowball(st) or {}).get("added") or []:
            v = selection.view(st, r) or {}
            rows.append({"id": f"rec:{r}", "rid": r, "title": _title(st, r), "decision": v.get("decision", "not screened"),
                         "by": record_of(st, r).get("found_via", ""), "stale": [], "notes": [], "actions": []})
    elif key in ("extract", "rob"):
        t, p = ("extraction", "ext") if key == "extract" else ("rob", "rob")
        for rid in st.included_ids():
            n = st.get(f"{p}:{rid}")
            d = (n or {}).get("data") or {}
            if key == "extract":
                edited = sum(1 for x in (d.get("fields") or {}).values() if isinstance(x, dict) and x.get("edited"))
                dec, by = ("extracted" if n and n["status"] == "active" else "not extracted"), ("person" if edited else "agent" if n else "")
                acts = [{"op": "extract.recompute", "args": {"rid": rid}, "label": "Re-run"}]
            else:
                dec, by = (d.get("overall") if n and n["status"] == "active" else "not assessed"), ("person" if d.get("human_confirmed") else "agent" if n else "")
                acts = ([{"op": "rob.confirm", "args": {"rids": [rid]}, "label": "Confirm"}] if n and not d.get("human_confirmed") else []) + \
                       [{"op": "rob.recompute", "args": {"rid": rid}, "label": "Re-run"}]
            rows.append(_row(st, rid, derived, n, decision=dec, by=by, status=(n or {}).get("status", ""), actions=acts))
    elif key == "synthesis":
        for n in st.by_type("synthesis"):
            d = n["data"]
            rows.append({**_row(st, "", derived, n, decision=n["status"], by="person" if d.get("edited") else "agent",
                                status=n["status"], cites=d.get("cites") or [],
                                actions=[{"op": "synthesis.revise", "args": {"node": n["id"]}, "label": "Ask for a revision",
                                          "needs": "instruction"}] if n["status"] == "active" else []),
                         "title": d.get("text", "")})
    elif key == "report":
        r = st.get("report")
        if r:
            rows.append({"id": "report", "rid": "", "title": "report.md, report_evidence.html", "decision": f"version {r['version']}",
                         "by": "", "stale": [f"{x['dep']} {x['why']}" for x in graph.stale_reasons(st, "report")], "notes": [], "actions": []})
    return rows


def _status(spec, rows: list[dict], waiting: int, st) -> str:
    if waiting:
        return "needs you"
    if any(r["stale"] for r in rows):
        return "out of date"
    return "done" if spec.started(st) else "not started"


def spent_by_cell(ctx) -> dict[tuple, float]:
    """USD spent so far by (stage, role), from usage.jsonl."""
    import json
    out: dict[tuple, float] = defaultdict(float)
    p = ctx.run_dir / "usage.jsonl"
    for line in p.read_text().splitlines() if p.exists() else ():
        u = json.loads(line)
        out[(u.get("stage"), u.get("role"))] += float(u.get("usd") or 0)
    return out


def stage_page(ctx, key: str, job: dict | None = None, jobs: list[dict] | None = None) -> dict:
    from .. import engine
    from ..config_lock import STAGE_STARTED, cell
    st = ctx.store
    key = stagespec.run_until(key)
    spec = stagespec.BY_KEY[key]
    derived = attention.by_node(st)
    rows = _items(ctx, key, derived)
    waiting = needs_you(ctx).get(key, [])
    plan = engine.plan(ctx) if st.get("protocol") else {"items": []}
    mine = [i for i in plan["items"] if i["type"] in TYPES[key] and i["action"] not in ("kept", "render", "current")]
    running = bool(job)
    tb = []
    run = RUN_OP.get(key)
    tb.append({"key": "run", "label": f"Run pending ({len(mine)})" if mine else "Run", "op": run, "args": {},
               "enabled": bool(run) and not running, "reason": "" if run else "runs as part of the screening stages",
               "count": len(mine), "preview": False})
    tb.append({"key": "rerun_all", "label": "Re-run all (fresh)", "op": f"{key}.rerun_all" if key in CLEARABLE else None, "args": {},
               "enabled": key in CLEARABLE and spec.started(st) and not running, "reason": NOT_CLEARABLE.get(key, "" if spec.started(st) else "not started"),
               "preview": True})
    tb.append({"key": "clear", "label": "Clear stage…", "op": f"{key}.clear" if key in CLEARABLE else None, "args": {},
               "enabled": key in CLEARABLE and spec.started(st) and not running, "reason": NOT_CLEARABLE.get(key, "" if spec.started(st) else "not started"),
               "preview": True})
    v = VERIFY_OP.get(key)
    tb.append({"key": "verify", "label": v[1] if v else "Verify", "op": v[0] if v else None, "args": {},
               "enabled": bool(v) and spec.started(st) and not running, "reason": "" if v else "this stage has no verifier step", "preview": False})
    tb.append({"key": "stop", "label": "Stop", "op": None, "args": {}, "enabled": running, "reason": "" if running else "nothing running",
               "preview": False})
    # inputs: what this stage's outputs depend on, with how many are out of date because of each
    own = [n for t in TYPES[key] for n in st.by_type(t, active_only=True)]
    dep_count, dep_stale = Counter(), Counter()
    for n in own:
        for d in n["depends_on"]:
            k = d.split(":", 1)[0] + (":*" if ":" in d else "")
            dep_count[k] += 1
        for r in graph.stale_reasons(st, n["id"]):
            dep_stale[r["dep"].split(":", 1)[0] + (":*" if ":" in r["dep"] else "")] += 1
    inputs = [{"input": k, "outputs": c, "out_of_date": dep_stale.get(k, 0)} for k, c in sorted(dep_count.items())]
    down = Counter()
    for n in own:
        for c in st.children(n["id"]):
            cn = st.get(c)
            if cn and cn["status"] == "active" and cn["type"] not in TYPES[key]:
                down[cn["type"]] += 1
    spent = spent_by_cell(ctx)
    fixed = key in STAGE_STARTED and STAGE_STARTED[key](st)
    models = {r: {**cell(ctx.cfg, key, r), "fixed": fixed, "spent_usd": round(spent.get((key, r), 0.0), 4)}
              for r in (spec.roles if spec.llm else ())}
    off = key == "snowball" and not ctx.cfg["pipeline"].get("citation_chasing", True)
    return {"key": key, "label": spec.label, "order": spec.order, "llm": spec.llm,
            "status": "off (pipeline.citation_chasing)" if off else _status(spec, rows, len(waiting), st), "counts": {"items": len(rows), "out_of_date": sum(1 for r in rows if r["stale"]),
                                                                         "needs_you": len(waiting)},
            "models": models, "spent_usd": round(sum(u for (s, _), u in spent.items() if s == key), 4),
            "pending": {"items": len(mine), "est_usd": engine._cost(ctx, mine).get("total_usd") if mine else 0},
            "toolbar": tb, "inputs": inputs, "dependents": dict(sorted(down.items())), "items": rows, "needs_you": waiting,
            "jobs": [j for j in (jobs or []) if _job_stage(j) == key][:20]}


def _job_stage(j: dict) -> str | None:
    if j.get("action") == "op":
        return j.get("op", "").split(".")[0]
    return {"pdfs": "retrieve", "sync": "synthesis", "reassess_rob": "rob"}.get(j.get("action"))


STEP_TO_STAGE = {"feedback": "review"}


def needs_you(ctx) -> dict[str, list[dict]]:
    """The review's waiting items (the To-do list's required items), grouped by stage; each links to its stage page."""
    from .. import todos
    out: dict[str, list[dict]] = defaultdict(list)
    if not ctx.store.get("protocol"):
        return {}
    for t in todos.build(ctx):
        if t.get("optional") or t.get("snoozed"):
            continue
        stage = STEP_TO_STAGE.get(t.get("step"), t.get("step"))
        out[stage].append({"id": t["id"], "kind": t["kind"], "title": t.get("title", ""), "rid": t.get("rid"),
                           "question": t.get("question", ""), "link": f"#/stage/{stage}" + (f"?rid={t['rid']}" if t.get("rid") else "")})
    return dict(out)
