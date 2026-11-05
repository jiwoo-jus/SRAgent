"""Bring the review up to date after human input (decisions, added PDFs), dependency-aware.

process_pdfs(ctx)      convert new/changed user files in <run_dir>/fulltext, re-screen those studies at
                       full text (unless a human already decided), re-extract included ones (under
                       update_policy.extraction); risk of bias and synthesis follow through the update engine
sync_dependents(ctx)   make extraction / RoB / synthesis consistent with the current eligibility
                       decisions: extract + assess newly included studies, deactivate excluded ones,
                       integrate new studies into the synthesis and remove excluded ones from it
Both write a change summary to the run log and regenerate the reports.
"""
from __future__ import annotations

from . import selection, state
from .context import Ctx
from .stages import extraction, fulltext, report, screening


def process_pdfs(ctx: Ctx) -> dict:
    st = ctx.store
    protocol = st.get("protocol")["data"]
    new = fulltext.new_local_files(ctx)
    ctx.log(f"[pdfs] {len(new)} new or changed full-text files: {new}")
    rescreened, reextracted, unreadable = [], [], []
    for rid in new:
        state.set_pdf_request(st, rid, actor="agent", wanted=False)   # the file is here now; decide with it
        sc = selection.view(st, rid)
        human = sc and sc.get("human_decision")   # a person decided: the file is read, the decision stays
        if sc and not human and not sc.get("rule") and sc["decision"] != "exclude":
            out = screening.review_one(ctx, protocol, rid, refetch=True)   # full-text review with the supplied file
            rescreened.append(rid)
        else:
            out = fulltext.fetch_one(ctx, rid)["kind"]
        if (st.get(f"ft:{rid}") or {}).get("data", {}).get("kind") != "fulltext":
            unreadable.append(rid)
            continue
        if rid in st.included_ids():
            had = st.get(f"ext:{rid}")
            mode = (ctx.cfg.get("update_policy") or {}).get("extraction", "auto")
            if had and mode != "auto":
                # update_policy.extraction: a person sees the re-extraction first (a re-run from the study page is
                # a proposal); nothing is rewritten here
                if not any(f["status"] == "open" and f.get("source") == "pdf" for f in had["flags"]):
                    st.flag(had["id"], "A full text was added after this extraction. Re-run the extraction from the "
                                       f"study page to update it (update_policy.extraction: {mode}).", actor="agent", source="pdf")
            else:
                extraction.extract_one(ctx, rid)
                if rid not in st.included_ids():   # the eligibility re-check after extraction set the study aside
                    rescreened += [] if rid in rescreened else [rid]
                else:
                    reextracted.append(rid)
            # the risk of bias and the synthesis statements that depend on the new values are now out of date: the
            # update engine below (sync_dependents) brings them up to date under update_policy, like any other change
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
    considered = set(state.considered(st) or (inc if not has_syn else []))
    cited = {c for n in st.by_type("synthesis", active_only=True) for c in n["data"]["cites"]}
    if has_syn and not state.considered(st):
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
