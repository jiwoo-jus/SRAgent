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
    return {"need_extract": need_extract, "need_rob": need_rob, "new_for_synthesis": new_for_syn,
            "excluded_but_cited": removed, "has_synthesis": has_syn}


def update_synthesis(ctx: Ctx, tp: str = "sync", pc: dict | None = None) -> dict:
    """Bring an existing synthesis in line with the included studies statement by statement: statements
    citing a study that is no longer included are revised (and retired when no cited study is left), newly
    included studies are integrated, and every changed statement is checked again. Unaffected statements
    are left as they are. Returns the change set."""
    from . import feedback  # local import (feedback imports stages)
    st = ctx.store
    pc = pc or pending_changes(ctx)
    cs = {"id": tp, "changes": []}
    if not pc["has_synthesis"]:
        return cs
    for rid in pc["excluded_but_cited"]:
        for sid in [n["id"] for n in st.by_type("synthesis", active_only=True) if rid in n["data"]["cites"]]:
            feedback._revise_dependent(ctx, cs, sid, f"screen:{rid}", {"included": True, "study": rid},
                                       {"included": False, "study": rid}, tp=tp)
    for rid in pc["new_for_synthesis"]:
        feedback._include_study(ctx, cs, rid, tp=tp, integrate=True)
    for sid in sorted({c["node"] for c in cs["changes"] if c["node"].startswith("syn:")}):
        if st.get(sid) and st.get(sid)["status"] == "active":
            synthesis.verify_one(ctx, sid)
    st.meta["synthesis_considered"] = sorted(st.included_ids())
    return cs


def sync_dependents(ctx: Ctx) -> dict:
    from . import feedback  # local import (feedback imports stages)
    st = ctx.store
    pc = pending_changes(ctx)
    inc = set(st.included_ids())
    for kind in ("extraction", "rob"):
        for n in st.by_type(kind):
            rid = n["id"].split(":", 1)[1]
            want = "active" if rid in inc else "excluded"
            if n["status"] != want:
                st.set_status(n["id"], want, actor="agent", reason="sync with eligibility decisions")
    if pc["need_extract"]:
        extraction.extract_all(ctx, only=pc["need_extract"])
    need_rob = [r for r in inc if st.get(f"rob:{r}") is None and st.get(f"ext:{r}")]
    if need_rob:
        rob.rob_all(ctx, only=need_rob)
    cs = update_synthesis(ctx, "sync", pc)
    report.write_reports(ctx)
    ctx.save()
    summary = {**pc, "synthesis_changes": sorted({c["node"] for c in cs["changes"]})}
    ctx.log(f"[sync] {summary}")
    return summary
