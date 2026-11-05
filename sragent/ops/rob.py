"""Operations of stage 8: risk of bias."""
from __future__ import annotations

import copy
import time

from . import operation

JUDGMENTS = ("low", "some_concerns", "high", "unclear")
_ORDER = {"low": 0, "unclear": 1, "some_concerns": 2, "high": 3}


@operation("rob.run", "rob", "llm")
def run(ctx) -> dict:
    """Draft the risk of bias of every included study not assessed yet."""
    from .. import pipeline
    pipeline.run(ctx, until="rob")
    return {"message": "risk of bias drafted"}


@operation("rob.confirm", "rob", "human", reach=True)
def confirm(ctx, rids: list[str], note: str = "") -> dict:
    """Confirm drafts as they are; a confirmed assessment is not drafted again."""
    st = ctx.store
    done = []
    for rid in rids:
        n = st.get(f"rob:{rid}")
        if not n:
            continue
        d = copy.deepcopy(n["data"])
        d["human_confirmed"] = {"ts": time.time(), "note": note}
        st.annotate(n["id"], d, evidence=n["evidence"], actor="user", reason="risk of bias confirmed by a person")
        done.append(rid)
    return {"ok": bool(done) or not rids, "message": f"{len(done)} confirmed", "count": len(done)}


@operation("rob.edit_domains", "rob", "human", reach=True)
def edit_domains(ctx, rid: str, changes: list[dict], note: str = "", overall: str | None = None) -> dict:
    """Change any number of domain judgments (and rationales); the assessment counts as confirmed by you."""
    st = ctx.store
    n = st.get(f"rob:{rid}")
    if not n:
        raise ValueError(f"{rid} has no risk-of-bias assessment")
    d = copy.deepcopy(n["data"])
    by = {x.get("domain"): x for x in d["domains"]}
    changed = []
    for ch in changes or []:
        x = by.get(ch.get("domain"))
        if not x:
            continue
        j = ch.get("judgment") or x.get("judgment")
        r = ch.get("rationale")
        if j == x.get("judgment") and (r is None or r == x.get("rationale")):
            continue
        if j not in JUDGMENTS:
            raise ValueError(f"unknown judgment {j}")
        x.setdefault("judgment_agent", x.get("judgment"))
        x["judgment"] = j
        if r is not None and r != x.get("rationale"):
            x["rationale_agent"] = x.get("rationale_agent") or x.get("rationale")
            x["rationale"] = r
        x["human_note"] = note
        changed.append(f"{x['domain']}={j}")
    if overall:
        d["overall"] = overall
    elif changed:
        d["overall"] = max((x.get("judgment") for x in d["domains"]), key=lambda j: _ORDER.get(j, 1))
    d["human_confirmed"] = {"ts": time.time(), "note": (("changed " + ", ".join(changed) + ". ") if changed else "") + note}
    st.annotate(n["id"], d, evidence=n["evidence"], actor="user",
                reason=(f"risk of bias changed by a person: {', '.join(changed)}." if changed else "risk of bias confirmed by a person.")
                + (f" Note: {note}" if note else ""))
    return {"message": f"{len(changed)} domain(s) changed, overall {d['overall']}" if changed else "confirmed"}


@operation("rob.recompute", "rob", "llm", reach=True)
def recompute(ctx, rid: str, note: str = "", propagate: bool = True, fresh: bool = True) -> dict:
    """Assess one study again (a proposal shown with the current assessment)."""
    from .. import actions
    cs = actions.rerun(ctx, rid, "rob", note=note, propagate=propagate, fresh=fresh)
    return {"message": f"proposal {cs['id']}: {cs.get('explanation', '')}", "changeset": cs["id"], "status": cs.get("status")}


@operation("rob.verify", "rob", "llm")
def verify(ctx, rids: list[str] | None = None) -> dict:
    """A second look by the verifier at drafts with a reason (doubts, flags) not read twice yet."""
    from ..stages.rob import verify_existing
    r = verify_existing(ctx, only=rids or None)
    return {"message": f"{r['checked']} read again", **r}


@operation("rob.redraft_all", "rob", "llm", reach=True)
def redraft_all(ctx, rids: list[str] | None = None) -> dict:
    """Draft every assessment again, except those a person confirmed."""
    from ..stages.rob import reassess_all
    r = reassess_all(ctx, only=rids or None)
    return {"message": f"{r.get('redrafted', 0)} redrafted", **r}
