"""Resumable API review pipeline."""
from .context import Ctx
from .stages import planning, search, screening, fulltext, extraction

STAGES = ['plan', 'search', 'screen', 'fulltext', 'extract']

def run(ctx: Ctx, until: str = "extract", stop_after_plan: bool = False):
    order = STAGES[:STAGES.index(until) + 1]
    protocol = planning.plan(ctx)
    ctx.save()
    if stop_after_plan or until == "plan":
        ctx.log(f"[pipeline] Review the protocol at {ctx.run_dir / 'protocol.yaml'}")
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
    ctx.save()
    ctx.log("[pipeline] done")
