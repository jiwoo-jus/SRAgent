"""What a reviewer can do to any output, outside the to-do list.

  add_comment / resolve_comment   free-text comments on a study, an output or the whole review
                                  (optionally sent to the agent as feedback; that is a job)
  edit                            manual edits WITHOUT the LLM: eligibility decision, extracted values,
                                  risk-of-bias domains, synthesis statements. Every edit is a new
                                  version (actor=user) with the note in the history. When other
                                  outputs depend on the edited one, a to-do offers to update them.
  rerun                           re-run one agent step (screening, extraction, risk of bias) for one
                                  study, with the model chosen for the job and an optional note. The
                                  new result and the dependent revisions it causes are computed in a
                                  sandbox and proposed as a change set; accepting it applies them all.
  propagate_from                  after a manual edit: revise the outputs that depend on it (proposal)
"""
from __future__ import annotations

import copy
import dataclasses
import json
import time

from .context import Ctx
from .store import study_of

RERUN_STEPS = {"screen": "Screening (eligibility)", "extract": "Data extraction", "rob": "Risk of bias"}
JUDGMENTS = ["low", "some_concerns", "high", "unclear"]
_ORDER = {"low": 0, "unclear": 1, "some_concerns": 2, "high": 3}


# ------------------------------------------------------------------ comments
def add_comment(ctx: Ctx, target: str, text: str, send: bool = False) -> dict:
    from . import provenance
    if send:
        provenance.require_judgment_allowed(ctx, "send feedback to the agent")
    st = ctx.store
    cl = st.meta.setdefault("comments", [])
    c = {"id": f"C{len(cl) + 1}", "target": target or "review", "text": text.strip(), "ts": time.time(),
         "send_to_agent": bool(send), "status": "open"}
    cl.append(c)
    st._log("comment", c["target"], "user", text.strip()[:500], None, c)
    ctx.save()          # recorded in provenance by the operation (ops/review.py: comment)
    return c


def resolve_comment(ctx: Ctx, cid: str, note: str = "") -> dict | None:
    st = ctx.store
    for c in st.meta.get("comments", []):
        if c["id"] == cid:
            c["status"] = "resolved"
            c["resolution"] = note
            st._log("resolve_comment", c["target"], "user", note or "resolved", cid, None)
            ctx.save()
            return c
    return None


def feedback_target(st, target: str) -> str | None:
    """Node the agent should look at for a comment ('study:<rid>' -> its extraction, else screening)."""
    if not target or target == "review":
        return None
    if target.startswith("study:"):
        rid = target.split(":", 1)[1]
        n = st.get(f"ext:{rid}")
        if n and n["status"] == "active":
            return n["id"]
        from .selection import node_id
        return node_id(st, rid)
    return target


# ------------------------------------------------------------------ manual edits (no LLM)
def _dependents(st, node_id: str) -> list[str]:
    rid = study_of(node_id)
    out = []
    if node_id.startswith("ext:"):
        if st.get(f"rob:{rid}") and st.get(f"rob:{rid}")["status"] == "active":
            out.append(f"rob:{rid}")
    if node_id.startswith(("ext:", "rob:")):
        out += [n["id"] for n in st.by_type("synthesis", active_only=True) if rid in n["data"]["cites"]]
    return out


def edit(ctx: Ctx, op: dict) -> dict:
    """The study page's edit {kind: screen|ext|rob|syn, rid|node, note, ...}: the operation it stands for (sragent/ops).
    Returns {"ok", "message"}."""
    from . import ops
    kind, note = op.get("kind"), (op.get("note") or "").strip()
    if kind == "screen":
        return ops.run(ctx, "eligibility.decide", {"rid": op["rid"], "decision": op.get("decision"),
                                                   "criterion": op.get("criterion"), "note": note})
    if kind == "ext":
        return ops.run(ctx, "extract.edit_values", {"rid": op["rid"], "fields": op.get("fields") or {}, "note": note})
    if kind == "rob":
        if not op.get("changes") and op.get("confirm", True):
            return ops.run(ctx, "rob.confirm", {"rids": [op["rid"]], "note": note})
        return ops.run(ctx, "rob.edit_domains", {"rid": op["rid"], "changes": op.get("changes") or [], "note": note,
                                                 "overall": op.get("overall")})
    if kind == "syn":
        return ops.run(ctx, "synthesis.edit_statement", {"node": op["node"], "text": op.get("text"), "cites": op.get("cites"),
                                                         "scope": op.get("scope"), "note": note})
    return {"ok": False, "message": f"unknown edit kind {kind}"}


# ------------------------------------------------------------------ re-run / propagate (proposals)
def _new_cs(prefix: str, **kw) -> dict:
    t0 = time.time()
    return {"id": f"{prefix}{int(t0 * 1000) % 10**9}", "changes": [], "status": "proposed", "created": t0,
            "mode": {"verify": False, "propagate": True}, **kw}


def _finish(ctx: Ctx, sb: Ctx, cs: dict, target: str, usd0: float, t0: float) -> dict:
    from . import feedback
    cs["cost_usd"] = round(ctx.hub.tracker.spent - usd0, 5)
    cs["seconds"] = round(time.time() - t0, 1)
    if cs["changes"]:
        feedback.sandbox_delta(ctx.store, sb.store, cs)
        feedback.record_bases(ctx.store, cs)
        if ctx.store.get(target):
            ctx.store.flag(target, f"{cs['label']} {cs['id']} pending review", actor="user", source="rerun")
    else:
        cs["status"] = "no_change"
    feedback._save_changeset(ctx, cs)
    ctx.save()
    return cs


def rerun(ctx: Ctx, rid: str, step: str, note: str = "", propagate: bool = True, fresh: bool = True) -> dict:
    from . import feedback, provenance
    provenance.require_judgment_allowed(ctx, "re-run requested by a person")
    provenance.record(ctx, "human_judgment", f"rerun_request:{step}", rid=rid, note=note[:300])
    from .stages import extraction, fulltext, rob, screening
    st = ctx.store
    from .selection import elig_id, node_id, view
    ft = st.get(f"ft:{rid}")
    # screening is re-run at the stage the study is at: on its full text when it has one (elig:<rid>)
    stage = "full_text" if (view(st, rid) or {}).get("stage") == "full_text" or (ft and ft["data"].get("kind") == "fulltext") \
        else "title_abstract"
    nid = {"screen": elig_id(rid) if stage == "full_text" else f"screen:{rid}", "extract": f"ext:{rid}", "rob": f"rob:{rid}"}[step]
    if step in ("extract", "rob") and rid not in st.included_ids():
        raise ValueError(f"{rid} is not included; re-run screening or change the decision first")
    if step == "rob" and not st.get(f"ext:{rid}"):
        raise ValueError(f"{rid} has no extraction yet")
    t0, usd0 = time.time(), ctx.hub.tracker.spent
    before_node = st.get(nid)
    before = copy.deepcopy(before_node["data"]) if before_node else None
    dec_id = node_id(st, rid)
    screen_before = copy.deepcopy((st.get(dec_id) or {}).get("data"))
    pstage = {"extract": "extract", "rob": "rob"}.get(step) or ("eligibility" if stage == "full_text" else "screen")
    llm = ctx.hub.agent_for(pstage)          # that stage's model (stage_models), or the model picked for this job
    cs = _new_cs("RR", kind="rerun", label=f"Re-run of {RERUN_STEPS[step].lower()}", rid=rid, step=step,
                 target=nid, note=note, model=llm.name,
                 feedback=f"Re-run {RERUN_STEPS[step].lower()} for study {rid} with {llm.name}"
                          + (f". Note: {note}" if note else ""), verdict="rerun")
    cs["mode"]["propagate"] = propagate
    sb = dataclasses.replace(ctx, store=st.clone(), note=note, stage=pstage)
    cache = llm.cache
    if fresh:
        llm.cache = None       # a re-run asks the model again instead of returning the cached answer
    try:
        if step == "screen":
            if stage == "full_text" and not ft:
                fulltext.fetch_one(sb, rid)
            screening.screen_one(sb, st.get("protocol")["data"], rid, stage, tag_suffix="_rerun", keep_human=False)
        elif step == "extract":
            extraction.extract_one(sb, rid)
        else:
            rob.rob_one(sb, rid)
    finally:
        llm.cache = cache
        sb.stage = None        # revising dependent outputs is feedback work (stage_models.feedback)
    after_node = sb.store.get(nid)
    after = after_node["data"] if after_node else None
    screen_after = (sb.store.get(dec_id) or {}).get("data")
    if step != "screen" and screen_before and screen_after and screen_before.get("decision") != screen_after.get("decision"):
        # extraction re-checked eligibility and changed the decision: that is part of what the reviewer accepts
        cs["changes"].append({"node": dec_id, "role": "dependent", "before": screen_before,
                              "after": copy.deepcopy(screen_after)})
    if before_node and before.get("human_decision"):
        cs["explanation"] = f"Note: this replaces your earlier manual decision ({before['human_decision']['decision']})."
    if after is not None and (before is None or feedback._changed(before, after, after_node["type"])) or \
            (step == "screen" and before.get("decision") != after.get("decision")):
        cs["changes"].append({"node": nid, "role": "target", "before": before, "after": after})
        same_decision = step == "screen" and before and before.get("decision") == after.get("decision")
        if propagate and before is not None and not same_decision:
            feedback._propagate(sb, cs, nid, before, after,
                                f"{cs['label']} requested by the reviewer. {note}".strip(), "rerun", 0, {nid})
    cs["explanation"] = (cs.get("explanation", "") + f" {len(cs['changes'])} output(s) would change.").strip()
    return _finish(ctx, sb, cs, nid, usd0, t0)


def propagate_from(ctx: Ctx, node_id: str, from_version: int | None = None, note: str = "") -> dict:
    """Revise the outputs that depend on a (manually edited) node; proposed as a change set."""
    from . import feedback, provenance
    provenance.require_judgment_allowed(ctx, "update after a manual edit")
    provenance.record(ctx, "human_judgment", "propagate_request", target=node_id, rid=study_of(node_id), note=note[:300])
    st = ctx.store
    n = st.get(node_id)
    if from_version is None:   # the version the out-of-date dependents last saw (attention.edit_state)
        from .attention import edit_state
        from_version = (edit_state(st, n) or {}).get("from_version", n["version"] - 1)
    old = next((h for h in n["history"] if h["version"] == from_version), None)
    if old is None:
        raise ValueError(f"{node_id} has no version {from_version}")
    t0, usd0 = time.time(), ctx.hub.tracker.spent
    cs = _new_cs("UP", kind="propagate", label="Update after your edit", target=node_id, rid=study_of(node_id),
                 model=ctx.agent.name, verdict="propagate",
                 feedback=f"Update the outputs that depend on your edit of {node_id} (v{from_version} → v{n['version']})"
                          + (f". Note: {note}" if note else ""))
    sb = dataclasses.replace(ctx, store=st.clone(), note=note)
    done = {node_id}
    feedback._propagate(sb, cs, node_id, old["data"], n["data"], f"manual edit by the reviewer. {note}".strip(),
                        "propagate", 0, done)
    cs["explanation"] = f"{len(cs['changes'])} dependent output(s) would change."
    return _finish(ctx, sb, cs, node_id, usd0, t0)
