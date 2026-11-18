"""What a change does, stage by stage, in words a reviewer reads (the preview dialog before an operation).

One row per stage the change reaches: where the stage stands now, what changes when the operation is applied, and
what then changes when the outputs are brought up to date (with its cost). Stages it does not reach are not listed.
"""
from __future__ import annotations

from .stagespec import STAGES

# node types that are not a stage's product but belong to one (the review's state, rule runs, requests)
_EXTRA = {"approval": "protocol", "strategy": "search", "rules_run": "screen", "pdf_request": "retrieve",
          "cohort": "eligibility", "synthesis_scope": "synthesis", "snowball": "snowball"}
STAGE_OF_TYPE = {t: s.key for s in STAGES for t in s.produces if not t.startswith("[")} | _EXTRA
STAGE_OF_TYPE["record"] = "search"

ACTION_WORDS = {"retire": "set aside", "reactivate": "brought back", "recompute": "redone by the model",
                "first": "done by the model for the first time (newly included)",
                "rederive": "decided again from the stored answers (no model call)", "revise": "revised by the model",
                "current": "marked current (nothing to redo)", "kept": "kept unchanged (you decided or edited it)",
                "render": "rendered again", "take_in": "taken into the synthesis (the model revises the statements)"}


def now(st, key: str) -> str:
    """Where a stage stands, in one phrase."""
    from . import selection, state
    if key == "protocol":
        return f"{'approved' if state.approval(st) else 'not approved'}, {len(st.by_type('criterion'))} criteria"
    if key == "search":
        return f"{len(st.by_type('record'))} records"
    if key == "screen":
        return f"{len(selection.screened(st))} screened"
    if key == "retrieve":
        return f"{sum(1 for n in st.by_type('fulltext') if n['data'].get('kind') == 'fulltext')} full texts"
    if key == "eligibility":
        return f"{len(st.included_ids())} included"
    if key == "extract":
        return f"{len(st.by_type('extraction', active_only=True))} extracted"
    if key == "rob":
        return f"{len(st.by_type('rob', active_only=True))} assessed"
    if key == "synthesis":
        return f"{len(st.by_type('synthesis', active_only=True))} statements"
    return ""


def _short(nid: str) -> str:
    return nid.split(":", 1)[1] if ":" in nid else nid


def _changed(live, after, nid: str) -> str | None:
    """One changed node, in words; None for what the stage row says already (the included count)."""
    from .studies import diff_summary
    a, b = after.get(nid), live.get(nid)
    if a is None or a["type"] == "cohort":
        return None
    if b is None:
        return f"{_short(nid)}: new"
    if a["type"] in ("screen", "eligibility") and a["data"].get("decision") != b["data"].get("decision"):
        return f"{_short(nid)}: {b['data'].get('decision')} → {a['data'].get('decision')}"
    if a["status"] != b["status"]:
        return f"{_short(nid)}: {b['status']} → {a['status']}"
    if a["data"] == b["data"]:      # only marked current with the new protocol
        return None
    what = "; ".join(diff_summary(a["type"], b["data"], a["data"]))
    return f"{_short(nid)}: {what[:140]}" if what else None


def _grouped(items: list[dict]) -> list[str]:
    """Planned steps of one stage: '2 set aside (G01, G02)'."""
    by: dict[str, list[str]] = {}
    for i in items:
        action = "take_in" if i["node"].startswith("syn:+") else i["action"]
        if action == "recompute" and any(str(w).startswith("included, not") for w in i.get("why") or []):
            action = "first"          # an output that does not exist yet (engine.plan: "included, not extracted")
        by.setdefault(action, []).append(i["rid"] or _short(i["node"]))
    out = []
    for action, ids in by.items():
        ids = sorted(set(ids))
        out.append(f"{len(ids)} {ACTION_WORDS.get(action, action)}" + (f" ({', '.join(ids[:6])}{' …' if len(ids) > 6 else ''})" if ids else ""))
    return out


def table(ctx, live, after=None, changed: list[str] = (), items: list[dict] = (), apply_items: bool = False,
          apply_note: str = "") -> list[dict]:
    """The rows of the preview: {stage, label, now, apply: [..], update: [..], usd}. `after`: the review once the
    operation is applied (a sandbox copy), `changed`: the nodes it changes, `items`: what then becomes out of date
    (the update engine's plan); apply_items: the operation is the update itself, so its plan is what applying does.
    apply_note: what applying does to the changed nodes when it cannot be worked out here (a model's answer)."""
    from .engine import _cost
    rows = {s.key: {"stage": s.key, "label": s.label, "now": now(live, s.key), "apply": [], "update": [], "usd": 0.0}
            for s in STAGES}
    for nid in changed:
        n = (after or live).get(nid) or live.get(nid)
        key = STAGE_OF_TYPE.get(n["type"]) if n else None
        if not key:
            continue
        line = apply_note and f"{_short(nid)}: {apply_note}" or (after is not None and _changed(live, after, nid))
        if line:
            rows[key]["apply"].append(line)
    if after is not None and after.included_ids() != live.included_ids():
        rows["eligibility"]["apply"].insert(0, f"{len(live.included_ids())} → {len(after.included_ids())} included")
    by_stage: dict[str, list[dict]] = {}
    for i in items:
        if i["action"] not in ("render", "current"):   # nothing to redo: not worth a line
            by_stage.setdefault(STAGE_OF_TYPE.get(i["type"], ""), []).append(i)
    for key, its in by_stage.items():
        if key in rows:
            rows[key]["apply" if apply_items else "update"] += _grouped(its)
            rows[key]["usd"] = round(_cost(ctx, its).get("total_usd") or 0.0, 4)
    return [r for r in rows.values() if r["apply"] or r["update"]]
