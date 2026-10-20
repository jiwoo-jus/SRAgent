"""End-to-end orchestration. Each stage is resumable: re-running skips finished work."""
from __future__ import annotations

from .context import Ctx
from .stages import extraction, fulltext, planning, report, rob, screening, search, synthesis

STAGES = ["plan", "search", "screen", "fulltext", "snowball", "extract", "rob", "synthesize", "report"]


def run(ctx: Ctx, until: str = "report", stop_after_plan: bool = False):
    order = STAGES[: STAGES.index(until) + 1]
    with ctx.at("plan"):
        protocol = planning.plan(ctx)
    ctx.save()
    if stop_after_plan or until == "plan":
        ctx.log(f"[pipeline] protocol written to {ctx.run_dir / 'protocol.yaml'} — review/edit it, then run again.")
        return
    if "search" in order and ctx.store.get("search") is None:
        with ctx.at("search"):
            search.search(ctx, protocol)
        ctx.save()
    if "screen" in order:
        with ctx.at("screen"):
            screening.screen_all(ctx, protocol)
    if "fulltext" in order:
        with ctx.at("fulltext"):
            screening.fulltext_review(ctx, protocol)     # retrieve + screen every forwarded record on its full text
    if "snowball" in order:
        from .stages import snowball
        with ctx.at("snowball"):
            snowball.snowball(ctx, protocol)             # citation chasing from the included studies
    changed = 0
    if "extract" in order:
        with ctx.at("extract"):
            changed += extraction.extract_all(ctx)
    if "rob" in order:
        with ctx.at("rob"):
            changed += rob.rob_all(ctx)
    st = ctx.store
    considered = set(st.meta.get("synthesis_considered") or [])
    human_edited = any(n["data"].get("edited") for n in st.by_type("synthesis", active_only=True))
    if "synthesize" in order and human_edited and (changed or considered != set(st.included_ids())):
        ctx.log("[pipeline] the synthesis has human edits; it is not re-written (use 'Update outputs' in the To-do list)")
    elif "synthesize" in order and not st.by_type("synthesis", active_only=True):
        with ctx.at("synthesize"):
            synthesis.synthesize(ctx)
    elif "synthesize" in order and considered != set(st.included_ids()):
        # an existing synthesis is updated, not re-written: only the statements the change affects
        from . import sync
        with ctx.at("synthesize"):
            cs = sync.update_synthesis(ctx, "pipeline")
        ctx.save()
        ctx.log(f"[pipeline] included studies changed: statements updated {sorted({c['node'] for c in cs['changes']})}")
    if "report" in order:
        report.write_reports(ctx)
    ctx.save()
    ctx.log(f"[pipeline] done. cost so far: {ctx.hub.tracker.summary()['spent_usd']} USD")
