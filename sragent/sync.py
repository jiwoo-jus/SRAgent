"""Bring the review up to date after human input (decisions, added PDFs), dependency-aware.

process_pdfs(ctx)      convert new/changed user files in <run_dir>/fulltext, re-screen those studies at
                       full text (unless a human already decided), re-extract and re-assess included ones
sync_dependents(ctx)   make extraction / RoB / synthesis consistent with the current eligibility
                       decisions: extract + assess newly included studies, deactivate excluded ones,
                       integrate new studies into the synthesis and remove excluded ones from it
Both write a change summary to the run log and regenerate the reports.
"""
from __future__ import annotations

from .context import Ctx
from .stages import extraction, fulltext, report, rob, screening, synthesis


def process_pdfs(ctx: Ctx) -> dict:
    st = ctx.store
    protocol = st.get("protocol")["data"]
    new = fulltext.new_local_files(ctx)
    ctx.log(f"[pdfs] {len(new)} new or changed full-text files: {new}")
    rescreened, reextracted, unreadable = [], [], []
    for rid in new:
        st.meta.get("pdf_wanted", {}).pop(rid, None)   # the file is here now; decide with it
        sc = st.get(f"screen:{rid}")
        human = sc and sc["data"].get("human_decision")   # a person decided: the file is read, the decision stays
        if sc and not human and not sc["data"].get("rule") and sc["data"]["decision"] != "exclude":
            out = screening.review_one(ctx, protocol, rid, refetch=True)   # full-text review with the supplied file
            rescreened.append(rid)
        else:
            out = fulltext.fetch_one(ctx, rid)["kind"]
        if (st.get(f"ft:{rid}") or {}).get("data", {}).get("kind") != "fulltext":
            unreadable.append(rid)
            continue
        if rid in st.included_ids():
            import copy
            old = copy.deepcopy(st.get(f"ext:{rid}")["data"]) if st.get(f"ext:{rid}") else None
            extraction.extract_one(ctx, rid)
            rob.rob_one(ctx, rid)
            reextracted.append(rid)
            if old is not None and st.by_type("synthesis", active_only=True):
                # the values may have changed with the full text: revise only the outputs that depend on them
                from . import feedback
                new_data = st.get(f"ext:{rid}")["data"]
                if feedback._changed(old, new_data, "extraction"):
                    cs = {"id": f"pdf_{rid}", "changes": []}
                    feedback._propagate(ctx, cs, f"ext:{rid}", old, new_data,
                                        "re-extracted from the full-text PDF supplied by the user", "pdf", 0, {f"ext:{rid}"})
                    ctx.log(f"[pdfs] {rid}: dependent outputs revised: {sorted({c['node'] for c in cs['changes']})}")
        ctx.save()
    screening.sync_awaiting_flags(ctx)
    res = {"processed": new, "rescreened": rescreened, "reextracted": reextracted, "unreadable": unreadable}
    res["sync"] = sync_dependents(ctx)
    fulltext.write_needed_pdfs(ctx)
    ctx.save()
    return res


def pending_changes(ctx: Ctx) -> dict:
    """What sync_dependents would do (no LLM calls) - used by the UI to-do list."""
    st = ctx.store
    inc = set(st.included_ids())
    need_extract = sorted(r for r in inc if st.get(f"ext:{r}") is None)
    need_rob = sorted(r for r in inc if st.get(f"rob:{r}") is None)
    has_syn = bool(st.by_type("synthesis", active_only=True))
    considered = set(st.meta.get("synthesis_considered") or (inc if not has_syn else []))
    cited = {c for n in st.by_type("synthesis", active_only=True) for c in n["data"]["cites"]}
    if has_syn and not st.meta.get("synthesis_considered"):
        considered = cited | {r for r in inc if st.get(f"ext:{r}")}
    new_for_syn = sorted(r for r in inc if r not in considered) if has_syn else []
    removed = sorted(c for c in cited if c not in inc)
    from . import engine
    p = engine.plan(ctx)
    work = [i for i in p["items"] if i["action"] not in ("keep", "render")]
    return {"need_extract": need_extract, "need_rob": need_rob, "new_for_synthesis": new_for_syn,
            "excluded_but_cited": removed, "has_synthesis": has_syn,
            "stale": {"n": len(work), "summary": p["summary"], "est_usd": p["cost"].get("total_usd"),
                      "kept": [i["node"] for i in p["items"] if i["action"] == "keep"], "lines": engine.describe(p)[:30]}}


def update_synthesis(ctx: Ctx, tp: str = "sync", pc: dict | None = None) -> dict:
    """Bring the review up to date (the update engine): screening re-assessed on changed criteria, the
    outputs of studies that left or joined the review, and the synthesis revised statement by statement.
    Returns the change set."""
    from . import engine
    return engine.execute(ctx, {"id": tp, "changes": []}, tag=tp, render=False)


def sync_dependents(ctx: Ctx) -> dict:
    """'Update now': everything out of date after a person's decisions, applied on the live store."""
    from . import engine
    pc = pending_changes(ctx)
    cs = engine.execute(ctx, {"id": "sync", "changes": []}, tag="sync", render=True)
    if not cs["changes"]:
        report.write_reports(ctx)
    ctx.save()
    summary = {**{k: v for k, v in pc.items() if k != "stale"}, "synthesis_changes": sorted({c["node"] for c in cs["changes"] if c["node"].startswith("syn:")}),
               "changes": sorted({c["node"] for c in cs["changes"]}), "kept": [k["node"] for k in cs["kept"]]}
    ctx.log(f"[sync] {summary}")
    return summary
