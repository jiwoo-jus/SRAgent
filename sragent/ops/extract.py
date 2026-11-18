"""Operations of stage 7: data extraction."""
from __future__ import annotations

import copy
import json
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
    from .. import rows
    spec = {f["name"]: f for f in ctx.topic.get("extraction_fields", [])}
    kinds = {k: f.get("kind") for k, f in spec.items()}
    d = copy.deepcopy(n["data"])
    changed = []
    for name, val in (fields or {}).items():
        item = d["fields"].setdefault(name, {"value": "NR"})
        if name in spec and rows.is_rows(spec[name]):     # rows: a JSON list, or one JSON object per line
            new = rows.from_person(spec[name], val)
            old = item.get("rows") or []
            keep = {json.dumps(rows.row_text(r, rows.columns_of(spec[name]))): r for r in old}
            for r in new:      # an unchanged row keeps its located sentences
                same = keep.get(json.dumps(rows.row_text(r, rows.columns_of(spec[name]))))
                if same and not r.get("evidence"):
                    r.update({k: same[k] for k in ("evidence", "spans", "quote", "check") if k in same})
            if [rows.row_text(r, rows.columns_of(spec[name])) for r in new] == [rows.row_text(r, rows.columns_of(spec[name])) for r in old]:
                continue
            item["edited"] = {"by": "user", "before": item.get("value"), "before_rows": old, "note": note, "ts": time.time()}
            item["rows"] = new
            item["value"] = rows.summary(spec[name], new)
            changed.append(name)
            continue
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


@operation("extract.export_rows", "extract", "deterministic")
def export_rows(ctx) -> dict:
    """Write meta_rows.csv: every row of the rows items (one row per result) of every included study."""
    from .. import rows
    if not any(rows.is_rows(f) for f in ctx.topic.get("extraction_fields") or []):
        return {"ok": False, "message": "no extraction item of kind rows in this review"}
    path, n = rows.export_csv(ctx)
    return {"message": f"{n} rows written to {path.name}", "path": str(path), "rows": n}


@operation("extract.studies", "extract", "llm")
def studies(ctx, rids: list[str]) -> dict:
    """Extract these included studies now, without running the stages before (a pilot of the data items, say)."""
    from ..stages.extraction import extract_all
    inc = set(ctx.store.included_ids())
    todo = [r for r in rids or [] if r in inc]
    if not todo:
        return {"ok": False, "message": "none of these studies is included"}
    extract_all(ctx, only=todo)
    done = [r for r in todo if (ctx.store.get(f"ext:{r}") or {}).get("status") == "active"]
    return {"message": f"{len(done)} of {len(todo)} extracted", "extracted": done,
            "skipped": sorted(set(rids) - set(todo)), "set_aside": sorted(set(todo) - set(done))}


@operation("extract.read_figures", "extract", "llm")
def read_figures(ctx, rids: list[str] | None = None) -> dict:
    """Estimate from the page images the values of results a paper shows only in a figure (marked as estimates)."""
    from .. import figures
    r = figures.read_all(ctx, rids or None)
    return {"message": f"{r['filled']} of {r['rows']} figure-only rows estimated from {r['figures']} figures"
            + (f"; {len(r['skipped'])} studies skipped" if r["skipped"] else ""), **r}
