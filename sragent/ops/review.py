"""Operations across stages: comments, feedback and proposals, edits and their dependents, updating, previews."""
from __future__ import annotations

import json

from . import operation


# ---------------------------------------------------------------- comments
@operation("comment", "review", "human", category="annotation")
def comment(ctx, target: str, text: str, send: bool = False) -> dict:
    """A comment on an output or study; with send=True the agent is asked to act on it (a feedback job)."""
    from .. import actions
    if not (text or "").strip():
        raise ValueError("empty comment")
    c = actions.add_comment(ctx, target or "review", text.strip(), send)
    return {"message": f"comment {c['id']} saved", "comment": c,
            **({"job": {"action": "feedback", "target": actions.feedback_target(ctx.store, target), "text": text}} if send else {})}


@operation("comment.resolve", "review", "human", category="annotation")
def comment_resolve(ctx, id: str, note: str = "") -> dict:
    """Mark a comment as resolved."""
    from .. import actions
    c = actions.resolve_comment(ctx, id, note)
    if not c:
        raise ValueError(f"no comment {id}")
    return {"message": "resolved"}


# ---------------------------------------------------------------- feedback and proposals
@operation("feedback", "review", "llm", reach=True)
def give_feedback(ctx, text: str, target: str | None = None, verify: bool | None = None, pmid: str | None = None,
                  propagate: bool | None = None, auto_accept: bool | None = None) -> dict:
    """Tell the agent what is wrong (on an output, or on the review); it checks the evidence and proposes a change."""
    from .. import feedback, provenance
    provenance.require_judgment_allowed(ctx, "feedback")
    provenance.record(ctx, "human_judgment", "feedback", target=target, note=text[:300])
    cs = feedback.handle_feedback(ctx, text, target=target, pmid=pmid, verify=verify, propagate=propagate,
                                  auto_accept=auto_accept)
    if cs.get("status") == "accepted":
        from ..stages import report
        report.write_reports(ctx)
    return {"message": f"{cs['id']}: {cs.get('status')}", "changeset": cs["id"], "status": cs.get("status"),
            "verdict": cs.get("verdict"), "explanation": cs.get("explanation", ""), "changes": [c["node"] for c in cs["changes"]]}


@operation("feedback.clarify", "review", "human")
def clarify(ctx, changeset: str, option: str, text: str = "") -> dict:
    """Answer the agent's question about your feedback (answer: it tries again) or withdraw the feedback."""
    p = ctx.run_dir / "feedback" / f"{changeset}.json"
    if not p.exists():
        raise ValueError(f"no proposal {changeset}")
    cs = json.loads(p.read_text())
    cs["status"] = "clarified" if option == "answer" else "withdrawn"
    cs["clarification_answer"] = text
    p.write_text(json.dumps(cs, indent=1, default=str))
    if option == "answer":
        return {"message": "sent to the agent with your clarification",
                "job": {"action": "feedback", "target": cs.get("target"), "text": f"{cs['feedback']}\nClarification: {text}"}}
    return {"message": "feedback withdrawn"}


@operation("feedback.disagreement", "review", "human")
def disagreement(ctx, node: str, flag: str, option: str, text: str = "") -> dict:
    """The agent checked the source and did not apply your feedback: agree (closed) or insist (it proposes your change)."""
    st = ctx.store
    n = st.get(node)
    f = next((x for x in (n or {}).get("flags", []) if x["id"] == flag), None)
    if not f:
        raise ValueError(f"no flag {flag} on {node}")
    if option == "agree":
        st.resolve_flag(node, flag, f"human agreed with the agent. {text}".strip(), actor="user")
        return {"message": "closed"}
    orig = f["text"].split("-> agent:", 1)[0].split(":", 1)[-1].strip()
    st.resolve_flag(node, flag, "human insisted; re-sent without the evidence check", actor="user")
    return {"message": "the agent will propose your change for approval",
            "job": {"action": "feedback", "target": node, "text": (orig + (" " + text if text else "")).strip(), "no_verify": True}}


@operation("proposal.accept", "review", "human", reach=True)
def accept(ctx, changeset: str, note: str = "") -> dict:
    """Apply a proposal (refused when what it changes has moved on since it was made)."""
    from .. import feedback
    from ..stages import report
    try:
        feedback.accept(ctx, changeset)
    except feedback.StaleProposal as e:
        raise ValueError(str(e))
    report.write_reports(ctx)
    return {"message": "changes applied, report regenerated"}


@operation("proposal.reject", "review", "human")
def reject(ctx, changeset: str, note: str = "") -> dict:
    """Reject a proposal; the original stays."""
    from .. import feedback
    feedback.reject(ctx, changeset, note)
    return {"message": "proposal rejected; the original stays"}


# ---------------------------------------------------------------- your edits and what depends on them
@operation("edit.update_dependents", "review", "llm", reach=True)
def update_dependents(ctx, node: str, note: str = "", from_version: int | None = None) -> dict:
    """The agent proposes updates to the outputs that use something you edited."""
    from .. import actions
    cs = actions.propagate_from(ctx, node, from_version=from_version, note=note)
    return {"message": f"proposal {cs['id']}: {cs.get('explanation', '')}", "changeset": cs["id"], "status": cs.get("status")}


@operation("edit.dependents_fine", "review", "human")
def dependents_fine(ctx, node: str, note: str = "") -> dict:
    """The outputs that use your edit are fine as they are: they are marked current with it."""
    from ..attention import edit_state
    e = edit_state(ctx.store, ctx.store.get(node) or {"status": "missing"}) or {"dependents": []}
    for cid in e["dependents"]:
        ctx.store.touch(cid)
    return {"message": f"{len(e['dependents'])} output(s) marked current"}


# ---------------------------------------------------------------- flags and the update
@operation("flag.answer", "review", "human")
def flag_answer(ctx, node: str, flag: str, option: str, text: str = "") -> dict:
    """Answer an open flag (a self-check result, an unrecognised term, an eligibility doubt): ok closes it, fix asks
    the agent (a feedback job; the result is a proposal)."""
    st = ctx.store
    n = st.get(node)
    f = next((x for x in (n or {}).get("flags", []) if x["id"] == flag), None)
    if not f:
        raise ValueError(f"no flag {flag} on {node}")
    if option == "ok":
        st.resolve_flag(node, flag, f"human: fine as is. {text}".strip(), actor="user")
        return {"message": "flag closed"}
    fb = f"{text} (context: {f['text'][:300]})" if text else f"Please check and fix: {f['text']}"
    st.resolve_flag(node, flag, "sent to the agent as feedback", actor="user")
    return {"message": "the agent will propose a revision; it appears here for your approval",
            "job": {"action": "feedback", "target": node, "text": fb}}


@operation("update_all", "review", "llm", reach=True)
def update_all(ctx) -> dict:
    """Bring every out-of-date output up to date (the update engine, under update_policy)."""
    from .. import sync
    r = sync.sync_dependents(ctx)
    return {"message": f"{len(r.get('changes', []))} output(s) changed", **r}


@operation("preview", "review", "read")
def preview(ctx, node: str) -> dict:
    """What a change to this output would reach, before it is made (no model call)."""
    from .. import engine
    return engine.preview(ctx, node)


@operation("snooze", "review", "human", category="annotation")
def snooze(ctx, item: str) -> dict:
    """Hide a To-do item until something changes."""
    s = ctx.store.meta.setdefault("snoozed", [])
    if item not in s:
        s.append(item)
    return {"message": "snoozed"}
