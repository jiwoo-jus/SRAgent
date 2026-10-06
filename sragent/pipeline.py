"""End-to-end orchestration. Each stage is resumable: re-running skips finished work."""
from __future__ import annotations

from .context import Ctx
from .stages import extraction, fulltext, planning, report, rob, screening, search, synthesis

STAGES = ["plan", "search", "screen", "fulltext", "extract", "rob", "synthesize", "report"]


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
        fulltext.fetch_all(ctx)
        screening.fulltext_screen_all(ctx, protocol)
    if "extract" in order:
        extraction.extract_all(ctx)
    if "rob" in order:
        rob.rob_all(ctx)
    if "synthesize" in order and not ctx.store.by_type("synthesis"):
        synthesis.synthesize(ctx)
    if "report" in order:
        report.write_reports(ctx)
    ctx.save()
    ctx.log(f"[pipeline] done. cost so far: {ctx.hub.tracker.summary()['spent_usd']} USD")
