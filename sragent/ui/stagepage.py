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


RETRIEVAL_LABEL = {"fulltext_unavailable": "no full text", "parse_failed": "unreadable file"}


def _retrieval_status(decision: str | None, req: dict) -> str:
    """A forwarded study without a usable full text: why (no full text found / the file has no text), and whether a
    person declared that no copy can be had. The two reasons stay apart, as in the report's flow diagram."""
    base = RETRIEVAL_LABEL.get(decision)
    if req.get("unavailable"):
        return f"{base} · not obtainable" if base else "not obtainable"
    return base or ("requested" if req.get("wanted") else "not retrieved yet")


def _text_source(fd: dict) -> str:
    """Where the study's text came from, for the Full texts page (only the abstract when no full text was found)."""
    if fd.get("kind") != "fulltext":
        return f"abstract only ({fd['local_file']} has no usable text)" if fd.get("parse_failed") else "abstract only"
    o = fd.get("origin", "")
    return "your file" if o.startswith("user file") else f"agent: {o}"


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
            fd = (ft or {}).get("data") or {}
            req = state.pdf_request(st, rid)
            kind = fd.get("kind")
            status = "full text" if kind == "fulltext" else _retrieval_status(v.get("decision"), req)
            acts = []
            if kind != "fulltext" and not req.get("unavailable"):
                acts = [{"op": "retrieve.declare_unobtainable", "args": {"rids": [rid]}, "label": "Not obtainable"}]
            if req.get("wanted") and not req.get("unavailable"):    # a reviewer asked for another copy: stop waiting for it
                acts.append({"op": "retrieve.decide_without", "args": {"rids": [rid]}, "label": "Decide without"})
            rows.append(_row(st, rid, derived, ft, decision=status, by=_text_source(fd) if ft else "",
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
            dec = v.get("decision")
            if dec in RETRIEVAL_LABEL:      # forwarded but not screened yet: its full text is missing (stage 4)
                by, dec = "", f"not screened: {_retrieval_status(dec, state.pdf_request(st, rid))}"
            rows.append(_row(st, rid, derived, el, decision=dec, by=by, because=v.get("because") or [],
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
                acts = [{"op": "extract.recompute", "args": {"rid": rid}, "label": "Re-run"}] if n and n["status"] == "active" else \
                       [{"op": "extract.studies", "args": {"rids": [rid]}, "label": "Extract"}]      # selectable: several at once
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


def _eligibility_verify(ctx) -> tuple[tuple, dict, int]:
    """The Verify button of full-text screening follows pipeline.verify_screening: every full-text decision not yet
    read twice (full_text, all), the undecided ones and those the screener contradicted itself on (conflict), or
    only the undecided ones."""
    from ..stages.screening import verify_todo
    vs = ctx.cfg["pipeline"].get("verify_screening")
    mode = "full_text" if vs in ("full_text", "all") else "conflict" if vs == "conflict" else "pending"
    n = len(verify_todo(ctx.store, mode))
    label = {"full_text": "Verify unread", "conflict": "Verify undecided and conflicting"}.get(mode, "Verify undecided") + (f" ({n})" if n else "")
    return ("eligibility.verify", label), {"mode": mode}, n


def _status(spec, rows: list[dict], waiting: int, st) -> str:
    if waiting:
        return "needs you"
    if any(r["stale"] for r in rows):
        return "out of date"
    return "done" if spec.started(st) else "not started"


def _budget_of(ctx, key: str) -> dict | None:
    """This stage's limit under budget: (limit_usd None: unlimited) and the total; None for a stage without model calls."""
    from ..config import MODEL_STAGES, budget_limits
    if key not in MODEL_STAGES:
        return None
    try:
        b = budget_limits(ctx.cfg)
    except ValueError:
        return None
    return {"limit_usd": b["stages"].get(key), "total_usd": b["total_usd"]}


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
    vargs, vreason = {}, "" if v else "this stage has no verifier step"
    if key == "eligibility":
        v, vargs, n = _eligibility_verify(ctx)
        vreason = "" if n else "nothing left for the verifier to read"
    tb.append({"key": "verify", "label": v[1] if v else "Verify", "op": v[0] if v else None, "args": vargs,
               "enabled": bool(v) and spec.started(st) and not running and not vreason, "reason": vreason, "preview": False})
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
            "budget": _budget_of(ctx, key),
            "pending": {"items": len(mine), "est_usd": engine._cost(ctx, mine).get("total_usd") if mine else 0},
            "toolbar": tb, "downloads": _downloads(ctx, key), "extras": _extras(ctx, key), "inputs": inputs, "dependents": dict(sorted(down.items())), "items": rows, "needs_you": waiting,
            "jobs": [j for j in (jobs or []) if _job_stage(j) == key][:20]}


def _extras(ctx, key: str) -> dict:
    """What a review with rows items adds to a stage page (no model calls): on Data extraction the figure-only rows
    still to read; on Synthesis the pooled results (sragent/meta.py), computed from the current rows."""
    from .. import figures, meta
    from ..rows import is_rows
    if not any(is_rows(f) for f in ctx.topic.get("extraction_fields") or []):
        return {}
    if key == "extract":
        p = figures.pending(ctx)
        return {"figures": {**p, "no_pdf": len(p["no_pdf"]), "no_pdf_ids": p["no_pdf"][:40]}}
    if key == "synthesis" and meta.plan_of(ctx.topic):
        try:
            res = meta.analyse(ctx)
        except Exception as e:
            return {"meta": {"error": str(e)[:300]}}
        gb = res["group_by"]
        groups = [{"group": " · ".join(g["group"][k] for k in gb), "measure": g["measure"], "k": g["k"],
                   **({"ratio": g["pooled"]["ratio"], "ci": g["pooled"]["ratio_ci"], "i2": g["pooled"]["i2"],
                       "prediction": g["pooled"].get("ratio_prediction"), "sensitivity": g["sensitivity"],
                       "estimated": sum(1 for s in g["studies"] if s["estimated"]),
                       "plot": f"/meta/forest_{meta._name(g['group'], g['measure'])}.svg"} if g["pooled"] else {})}
                  for g in res["groups"]]
        return {"meta": {"rows": res["rows"], "rows_used": res["rows_used"], "min_studies": res["min_studies"], "not_used": res["not_used"],
                         "possible_duplicates": res["possible_duplicates"], "groups": groups,
                         "written": (ctx.run_dir / "meta" / "results.json").exists()}}
    return {}


def _downloads(ctx, key: str) -> list[dict]:
    """Files a stage page offers: the rows of the rows items (one row per result) for the analysis."""
    from ..rows import is_rows
    if key == "extract" and any(is_rows(f) for f in ctx.topic.get("extraction_fields") or []):
        return [{"label": "Rows for the meta-analysis (CSV)", "href": "/meta_rows.csv",
                 "hint": "one line per result of every included study, with its cited sentence ids; also written as meta_rows.csv with each report"}]
    if key == "synthesis" and (ctx.run_dir / "meta" / "effects.csv").exists():
        return [{"label": "Effect sizes (CSV)", "href": "/meta/effects.csv", "hint": "one line per study and group"},
                {"label": "Pooled table (Markdown)", "href": "/meta/summary.md", "hint": "the pooled estimates as a table"}]
    return []


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
