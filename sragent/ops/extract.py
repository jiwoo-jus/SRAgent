"""Operations of stage 7: data extraction."""
from __future__ import annotations

import copy
import time

from . import operation


@operation("extract.run", "extract", "llm")
def run(ctx, fresh: bool = False) -> dict:
    """Extract every included study not extracted yet (and the stages before, if needed)."""
    from .. import pipeline
    from .stage import fresh_answers
    with fresh_answers(ctx, fresh):
        pipeline.run(ctx, until="extract")
    return {"message": "extraction done"}


@operation("extract.edit_values", "extract", "human", reach=True)
def edit_values(ctx, rid: str, fields: dict, note: str = "") -> dict:
    """Correct extracted values by hand; the agent keeps them in any later extraction."""
    st = ctx.store
    n = st.get(f"ext:{rid}")
    if not n:
        raise ValueError(f"{rid} has no extraction")
    kinds = {f["name"]: f.get("kind") for f in ctx.topic.get("extraction_fields", [])}
    d = copy.deepcopy(n["data"])
    changed = []
    for name, val in (fields or {}).items():
        item = d["fields"].setdefault(name, {"value": "NR"})
        val = str(val).strip()
        if val == item.get("value"):
            continue
        item["edited"] = {"by": "user", "before": item.get("value"), "note": note, "ts": time.time()}
        item["value"] = val
        if kinds.get(name) in ("drug", "disease") and val.upper() != "NR":
            try:
                from ..stages.extraction import known_terms, validate_field_terms
                item["terms"] = validate_field_terms(val, kinds[name], None, known_terms(ctx))
            except Exception:   # offline: keep the value, drop stale validation
                item.pop("terms", None)
        changed.append(name)
    if not changed:
        return {"message": "nothing changed"}
    st.annotate(n["id"], d, evidence=n["evidence"], actor="user",
                reason=f"manual edit of {', '.join(changed)}." + (f" Note: {note}" if note else ""))
    return {"message": f"saved {', '.join(changed)} (v{n['version']})"}


@operation("extract.recompute", "extract", "llm", reach=True)
def recompute(ctx, rid: str, note: str = "", propagate: bool = True, fresh: bool = True) -> dict:
    """Extract one study again (a proposal; your edited values are kept)."""
    from .. import actions
    cs = actions.rerun(ctx, rid, "extract", note=note, propagate=propagate, fresh=fresh)
    return {"message": f"proposal {cs['id']}: {cs.get('explanation', '')}", "changeset": cs["id"], "status": cs.get("status")}


@operation("extract.revalidate_terms", "extract", "deterministic")
def revalidate_terms(ctx) -> dict:
    """Check drug and disease terms of the stored extractions again (no model calls)."""
    from ..stages.extraction import revalidate_terms as rv
    return {"message": "terms checked again", "counts": rv(ctx)}
