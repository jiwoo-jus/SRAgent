"""The stage toolbar (04 §5.1): the same operations, with the same meaning, on every stage that produces one output per
study or statement.

  <stage>.clear       retire the agent's outputs of the stage, so the stage shows them as not done and what depends on
                      them shows out of date; what a person decided, confirmed or edited is kept, and so are rule
                      exclusions (the rules decide them, not a model)
  <stage>.rerun_all   clear, then run the stage again with the model's cache off (fresh answers), as one job

Stages without them (and why) are listed in NOT_CLEARABLE; the stage page shows the reason on the disabled button.
"""
from __future__ import annotations

import contextlib

from . import operation

CLEARABLE = {"screen": ("screen",), "eligibility": ("eligibility",), "extract": ("extraction",), "rob": ("rob",),
             "synthesis": ("synthesis",)}
NOT_CLEARABLE = {
    "protocol": "the protocol is changed with the review team (Ask the agent, Edit criteria), not cleared",
    "search": "a new search is a new review folder: the records and every decision rest on it",
    "retrieve": "full texts are files: add or replace them in the full-text folder",
    "snowball": "records found by citation chasing are screened like the others; clear screening instead",
    "report": "the report is rendered from the outputs each time",
}


@contextlib.contextmanager
def fresh_answers(ctx, fresh: bool):
    """The model is asked again instead of answering from the cache (answers are still written to it)."""
    if not fresh:
        yield
        return
    before = ctx.hub.cache_read
    ctx.hub.cache_read = False
    ctx.hub._clients.clear()
    try:
        yield
    finally:
        ctx.hub.cache_read = before
        ctx.hub._clients.clear()


def _kept(n: dict) -> str | None:
    """Why a node is kept on clear (None: it is cleared)."""
    from .. import graph
    if graph.human_pinned(n):
        return "decided, confirmed or edited by a person"
    if n["data"].get("rule"):
        return "a rule exclusion"
    return None


def clear_stage(ctx, stage: str) -> dict:
    st = ctx.store
    if stage == "synthesis":
        edited = [n["id"] for n in st.by_type("synthesis", active_only=True) if _kept(n)]
        if edited:
            raise ValueError(f"statements you edited ({', '.join(edited)}) would be lost: the synthesis is rewritten as a "
                             "whole. Update it instead, or undo the edits first.")
    cleared, kept = [], []
    for t in CLEARABLE[stage]:
        for n in st.by_type(t, active_only=True):
            why = _kept(n)
            if why:
                kept.append(n["id"])
                continue
            st.set_status(n["id"], "superseded" if t == "synthesis" else "retired", actor="user",
                          reason=f"stage {stage} cleared by a person")
            cleared.append(n["id"])
    return {"message": f"{len(cleared)} output(s) cleared, {len(kept)} kept", "cleared": cleared, "kept": kept,
            "count": len(cleared)}


def _run(ctx, stage: str, fresh: bool) -> None:
    from .. import pipeline
    with fresh_answers(ctx, fresh):
        pipeline.run(ctx, until="report" if stage == "synthesis" else stage)


def _make(stage: str) -> None:
    @operation(f"{stage}.clear", stage, "human", reach=True)
    def clear(ctx, note: str = "") -> dict:
        """Retire the agent's outputs of this stage (kept: what a person decided, confirmed or edited, and rule exclusions)."""
        return clear_stage(ctx, stage)

    @operation(f"{stage}.rerun_all", stage, "llm", reach=True)
    def rerun_all(ctx, note: str = "") -> dict:
        """Clear this stage, then run it again with fresh model answers (one job)."""
        from .. import provenance
        provenance.require_judgment_allowed(ctx, f"{stage}.rerun_all")
        r = clear_stage(ctx, stage)
        provenance.record(ctx, "human_judgment", f"{stage}.rerun_all", note=note[:300], cleared=len(r["cleared"]))
        ctx.save()
        _run(ctx, stage, fresh=True)
        return {"message": f"{len(r['cleared'])} output(s) redone with fresh answers; {len(r['kept'])} kept"}


for _s in CLEARABLE:
    _make(_s)
