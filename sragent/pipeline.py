"""End-to-end orchestration. Each stage is resumable: re-running skips finished work."""
from __future__ import annotations

from .context import Ctx
from .stages import extraction, fulltext, planning, report, rob, screening, search, synthesis

STAGES = ["plan", "search", "screen", "fulltext", "snowball", "extract", "rob", "synthesize", "report"]


def run(ctx: Ctx, until: str = "report", stop_after_plan: bool = False):
    order = STAGES[: STAGES.index(until) + 1]
    protocol = planning.plan(ctx)
    ctx.save()
    if stop_after_plan or until == "plan":
        ctx.log(f"[pipeline] protocol written to {ctx.run_dir / 'protocol.yaml'} — review/edit it, then run again.")
        return
    if "search" in order and ctx.store.get("search") is None:
        search.search(ctx, protocol)
        ctx.save()
    if "screen" in order:
        screening.screen_all(ctx, protocol)
    if "fulltext" in order:
        screening.fulltext_review(ctx, protocol)     # retrieve + screen every forwarded record on its full text
    if "snowball" in order:
        from .stages import snowball
        snowball.snowball(ctx, protocol)             # citation chasing from the included studies
    changed = 0
    if "extract" in order:
        changed += extraction.extract_all(ctx)
    if "rob" in order:
        changed += rob.rob_all(ctx)
    st = ctx.store
    considered = set(st.meta.get("synthesis_considered") or [])
    human_edited = any(n["data"].get("edited") for n in st.by_type("synthesis", active_only=True))
    if "synthesize" in order and human_edited and (changed or considered != set(st.included_ids())):
        ctx.log("[pipeline] the synthesis has human edits; it is not re-written (use 'Update outputs' in the To-do list)")
    elif "synthesize" in order and (not st.by_type("synthesis", active_only=True) or changed or considered != set(st.included_ids())):
        if st.by_type("synthesis", active_only=True):
            ctx.log("[pipeline] included studies or their data changed: the synthesis is written again")
        synthesis.synthesize(ctx)
    if "report" in order:
        report.write_reports(ctx)
    ctx.save()
    ctx.log(f"[pipeline] done. cost so far: {ctx.hub.tracker.summary()['spent_usd']} USD")
