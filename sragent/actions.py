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
    ctx.save()
    provenance.record(ctx, "human_judgment" if send else "annotation", "feedback" if send else "comment",
                      target=c["target"], rid=study_of(c["target"].replace("study:", "rec:")), note=text[:300])
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
        for k in ("ext", "screen"):
            n = st.get(f"{k}:{rid}")
            if n and n["status"] == "active":
                return n["id"]
        return f"screen:{rid}"
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


def _mark_dependents(st, node_id: str, from_version: int, what: str):
    deps = _dependents(st, node_id)
    if not deps:
        return None
    for f in st.get(node_id)["flags"]:   # one open notice per node is enough; keep the oldest version
        if f["status"] == "open" and f.get("source") == "manual_edit":
            f["text"] = f"{f['text'].split(' || ')[0]} || also: {what}"[:600]
            return f
    f = st.flag(node_id, f"You edited this output ({what}). {len(deps)} dependent output(s) may now be out of date: "
                f"{', '.join(deps)}", actor="user", source="manual_edit")
    f["from_version"] = from_version
    f["dependents"] = deps
    return f


def edit(ctx: Ctx, op: dict) -> dict:
    """op = {kind: screen|ext|rob|syn, rid|node, note, ...}. Returns {"ok", "message"}."""
    from . import provenance
    from .stages import report
    try:
        provenance.require_judgment_allowed(ctx, f"manual edit ({op.get('kind')})")
    except provenance.NotAllowed as e:
        return {"ok": False, "message": str(e)}
    st = ctx.store
    kind, note = op["kind"], (op.get("note") or "").strip()
    sfx = f" Note: {note}" if note else ""
    if kind == "screen":
        rid = op["rid"]
        n = st.get(f"screen:{rid}")
        dec = op["decision"]
        if dec not in ("include", "exclude", "awaiting"):
            return {"ok": False, "message": f"unknown decision {dec}"}
        if dec == "include" and (st.get(f"ft:{rid}") or {}).get("data", {}).get("kind") != "fulltext":
            return {"ok": False, "message": f"Final inclusion needs a full-text review; supply {rid}.pdf first."}
        d = copy.deepcopy(n["data"]) if n else {"stage": "title_abstract", "criteria": []}
        before = d.get("decision")
        d["decision"] = dec
        d["because"] = [op.get("criterion") or "human"] if dec == "exclude" else []
        d["human_decision"] = {"decision": dec, "note": note, "reason": op.get("criterion"), "ts": time.time()}
        if d.get("rule"):
            d["rule_restored"] = {"rule": d.pop("rule"), "note": "manual decision"}
        deps = n["depends_on"] if n else [f"rec:{rid}", "protocol"]
        st.upsert(f"screen:{rid}", "screen", d, depends_on=deps, actor="user",
                  reason=f"manual eligibility decision: {before} -> {dec}.{sfx}")
        for f in st.get(f"screen:{rid}")["flags"]:
            if f["status"] == "open" and f.get("source") in ("screening", None, "feedback") and "pending review" not in f["text"]:
                st.resolve_flag(f"screen:{rid}", f["id"], f"human decided: {dec}", actor="user")
        msg = f"{rid}: {before} → {dec}"
        if before != dec and st.by_type("extraction"):
            msg += ". Extraction, risk of bias and synthesis follow via 'Update outputs' in the To-do list."
    elif kind == "ext":
        rid = op["rid"]
        n = st.get(f"ext:{rid}")
        kinds = {f["name"]: f.get("kind") for f in ctx.topic.get("extraction_fields", [])}
        d = copy.deepcopy(n["data"])
        changed = []
        for name, val in (op.get("fields") or {}).items():
            item = d["fields"].setdefault(name, {"value": "NR"})
            val = str(val).strip()
            if val == item.get("value"):
                continue
            item["edited"] = {"by": "user", "before": item.get("value"), "note": note, "ts": time.time()}
            item["value"] = val
            if kinds.get(name) in ("drug", "disease") and val.upper() != "NR":
                try:
                    from .stages.extraction import known_terms, validate_field_terms
                    item["terms"] = validate_field_terms(val, kinds[name], None, known_terms(ctx))
                except Exception:   # offline: keep the value, drop stale validation
                    item.pop("terms", None)
            changed.append(name)
        if not changed:
            return {"ok": True, "message": "nothing changed"}
        v0 = n["version"]
        st.upsert(n["id"], "extraction", d, depends_on=n["depends_on"], evidence=n["evidence"], actor="user",
                  reason=f"manual edit of {', '.join(changed)}.{sfx}")
        _mark_dependents(st, n["id"], v0, "edited " + ", ".join(changed))
        msg = f"saved {', '.join(changed)} (v{v0 + 1})"
    elif kind == "rob":
        rid = op["rid"]
        n = st.get(f"rob:{rid}")
        d = copy.deepcopy(n["data"])
        changed = []
        by = {x.get("domain"): x for x in d["domains"]}
        for ch in op.get("changes") or []:
            x = by.get(ch.get("domain"))
            if not x:
                continue
            j = ch.get("judgment") or x.get("judgment")
            r = ch.get("rationale")
            if j == x.get("judgment") and (r is None or r == x.get("rationale")):
                continue
            if j not in JUDGMENTS:
                return {"ok": False, "message": f"unknown judgment {j}"}
            x.setdefault("judgment_agent", x.get("judgment"))
            x["judgment"] = j
            if r is not None and r != x.get("rationale"):
                x["rationale_agent"] = x.get("rationale_agent") or x.get("rationale")
                x["rationale"] = r
            x["human_note"] = note
            changed.append(f"{x['domain']}={j}")
        if op.get("overall"):
            d["overall"] = op["overall"]
        elif changed:
            d["overall"] = max((x.get("judgment") for x in d["domains"]), key=lambda j: _ORDER.get(j, 1))
        if op.get("confirm", True):
            d["human_confirmed"] = {"ts": time.time(), "note": (("changed " + ", ".join(changed) + ". ") if changed else "") + note}
        if not changed and not op.get("confirm", True):
            return {"ok": True, "message": "nothing changed"}
        v0 = n["version"]
        st.upsert(n["id"], "rob", d, depends_on=n["depends_on"], evidence=n["evidence"], actor="user",
                  reason=(f"manual risk-of-bias edit: {', '.join(changed)}." if changed else "risk of bias confirmed by human.") + sfx)
        if changed:
            _mark_dependents(st, n["id"], v0, "changed " + ", ".join(changed))
        msg = (f"{len(changed)} domain(s) changed, overall {d['overall']}" if changed else "confirmed")
    elif kind == "syn":
        from .stages import synthesis
        nid = op["node"]
        n = st.get(nid)
        d = copy.deepcopy(n["data"])
        text = (op.get("text") or d["text"]).strip()
        cites = op.get("cites", d["cites"])
        if isinstance(cites, str):
            cites = [c.strip() for c in cites.replace(";", ",").split(",") if c.strip()]
        cites, fixes = synthesis.reconcile_citations(st, text, cites)
        d.update({"text": text, "cites": cites, "scope": op.get("scope", d.get("scope", ""))})
        d.pop("verification", None)
        d["edited"] = {"by": "user", "before": n["data"]["text"], "note": note, "ts": time.time()}
        if fixes:
            d["citation_fixes"] = fixes
        st.upsert(nid, "synthesis", d, depends_on=synthesis.statement_deps(st, cites), actor="user",
                  reason=f"manual edit of the statement.{sfx}")
        for f in st.get(nid)["flags"]:
            if f["status"] == "open" and f.get("source") == "synthesis_verify":
                st.resolve_flag(nid, f["id"], "statement edited by human", actor="user")
        msg = "statement saved" + (f" ({'; '.join(fixes)})" if fixes else "")
    else:
        return {"ok": False, "message": f"unknown edit kind {kind}"}
    ctx.save()
    if st.by_type("synthesis"):
        report.write_reports(ctx)
    with open(ctx.run_dir / "human_decisions.jsonl", "a") as f:
        f.write(json.dumps({"todo_id": f"edit:{kind}:{op.get('rid') or op.get('node')}", "option": "manual_edit",
                            "edit": op, "status": "applied", "ts": time.time()}) + "\n")
    provenance.record(ctx, "human_judgment", f"manual_edit:{kind}", rid=op.get("rid") or study_of(op.get("node") or ""),
                      target=op.get("node") or f"{kind}:{op.get('rid')}", note=note[:300])
    return {"ok": True, "message": msg}


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
        feedback.record_bases(ctx.store, cs)
        cs["sandbox_nodes"] = {c["node"]: sb.store.get(c["node"]) for c in cs["changes"]}
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
    nid = {"screen": f"screen:{rid}", "extract": f"ext:{rid}", "rob": f"rob:{rid}"}[step]
    if step in ("extract", "rob") and rid not in st.included_ids():
        raise ValueError(f"{rid} is not included; re-run screening or change the decision first")
    if step == "rob" and not st.get(f"ext:{rid}"):
        raise ValueError(f"{rid} has no extraction yet")
    t0, usd0 = time.time(), ctx.hub.tracker.spent
    cs = _new_cs("RR", kind="rerun", label=f"Re-run of {RERUN_STEPS[step].lower()}", rid=rid, step=step,
                 target=nid, note=note, model=ctx.agent.name,
                 feedback=f"Re-run {RERUN_STEPS[step].lower()} for study {rid} with {ctx.agent.name}"
                          + (f". Note: {note}" if note else ""), verdict="rerun")
    cs["mode"]["propagate"] = propagate
    sb = dataclasses.replace(ctx, store=st.clone(), note=note)
    before_node = st.get(nid)
    before = copy.deepcopy(before_node["data"]) if before_node else None
    cache = sb.agent.cache
    if fresh:
        sb.agent.cache = None       # a re-run asks the model again instead of returning the cached answer
    try:
        if step == "screen":
            prev = (before or {}).get("stage")
            ft = st.get(f"ft:{rid}")
            stage = "full_text" if prev == "full_text" or (ft and ft["data"].get("kind") == "fulltext") else "title_abstract"
            if stage == "full_text" and not ft:
                fulltext.fetch_one(sb, rid)
            screening.screen_one(sb, st.get("protocol")["data"], rid, stage, tag_suffix="_rerun")
        elif step == "extract":
            extraction.extract_one(sb, rid)
        else:
            rob.rob_one(sb, rid)
    finally:
        sb.agent.cache = cache
    after = sb.store.get(nid)["data"]
    if before_node and before.get("human_decision"):
        cs["explanation"] = f"Note: this replaces your earlier manual decision ({before['human_decision']['decision']})."
    if before is None or feedback._changed(before, after, sb.store.get(nid)["type"]) or \
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
    if from_version is None:
        f = next((f for f in n["flags"] if f["status"] == "open" and f.get("source") == "manual_edit"), None)
        from_version = (f or {}).get("from_version", n["version"] - 1)
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
    for f in st.get(node_id)["flags"]:
        if f["status"] == "open" and f.get("source") == "manual_edit":
            st.resolve_flag(node_id, f["id"], f"sent to the agent ({cs['id']})", actor="user")
    cs["explanation"] = f"{len(cs['changes'])} dependent output(s) would change."
    return _finish(ctx, sb, cs, node_id, usd0, t0)
