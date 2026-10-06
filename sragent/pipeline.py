"""Resumable API review pipeline."""
from .context import Ctx
from .stages import planning, search, screening

STAGES = ['plan', 'search', 'screen']

def run(ctx: Ctx, until: str = "screen", stop_after_plan: bool = False):
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
    ctx.save()
    ctx.log("[pipeline] done")
