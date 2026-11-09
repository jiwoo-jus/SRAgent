"""Operations of stage 1: the protocol. protocol.yaml is an export of the stored protocol (decision 3 of 04 §9)."""
from __future__ import annotations

from . import operation


@operation("protocol.draft", "protocol", "llm")
def draft(ctx) -> dict:
    """Draft the protocol from the question and the seed eligibility text (one model call)."""
    from .. import pipeline
    pipeline.run(ctx, until="protocol")
    return {"message": "protocol drafted"}


@operation("protocol.approve", "protocol", "human")
def approve(ctx, note: str = "") -> dict:
    """Approve the protocol as it is now (a later change asks for approval again)."""
    from .. import state
    if not ctx.store.get("protocol"):
        raise ValueError("draft the protocol first")
    state.approve(ctx.store, note)
    return {"message": "protocol approved"}


@operation("protocol.edit_criteria", "protocol", "human", reach=True)
def edit_criteria(ctx, criteria: list[dict], note: str = "", reassess_excluded: str = "") -> dict:
    """Change, add or remove criteria by hand (no model call); dependent decisions are re-derived or re-assessed."""
    from ..stages import planning
    r = planning.edit_criteria(ctx, criteria, note=note, reassess_excluded=reassess_excluded)
    return {"message": "criteria saved; approve the protocol" if r.get("applied") else "nothing changed", **r}


@operation("protocol.revise", "protocol", "llm", reach=True)
def revise(ctx, instruction: str) -> dict:
    """Ask the agent for a change in plain words (one call to the protocol stage's model)."""
    from .. import provenance
    from ..stages import planning
    provenance.require_judgment_allowed(ctx, "protocol.revise")
    with ctx.at("protocol"):
        r = planning.revise_protocol(ctx, instruction)
    provenance.record(ctx, "human_judgment", "protocol.revise", note=instruction[:300], changed=r.get("changed"))
    return {"message": "protocol revised; approve it again" if r.get("applied") else "nothing changed: " + (r.get("explanation") or ""), **r}


@operation("protocol.import_file", "protocol", "human", reach=True)
def import_file(ctx, note: str = "") -> dict:
    """Apply a protocol.yaml edited by hand: stored as your change, dependent decisions re-derived or re-assessed."""
    import yaml
    from ..stages import planning
    if not ctx.store.get("protocol"):
        raise ValueError("draft the protocol first")
    on_disk = yaml.safe_load(open(ctx.run_dir / "protocol.yaml")) or {}
    if not planning.file_differs(ctx):
        return {"message": "protocol.yaml matches the review's protocol: nothing to import"}
    r = planning.apply_protocol(ctx, on_disk, actor="user", reason="protocol.yaml edited by a person and imported",
                                record={"note": note} if note else None)
    return {"message": f"protocol re-applied: {r['decisions_changed']} decisions changed", **r}


@operation("protocol.export_file", "protocol", "deterministic")
def export_file(ctx) -> dict:
    """Write protocol.yaml from the review's protocol (discards a hand edit not imported)."""
    from ..stages import planning
    planning.export_file(ctx)
    return {"message": f"wrote {ctx.run_dir / 'protocol.yaml'}"}
