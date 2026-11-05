"""Notes a person should see that follow from the graph, computed when asked (phase 2d, 04 §3).

Four kinds of notice used to be flags that code opened and resolved by hand at a dozen places (and that could drift
from the state they described). They are now read off the graph each time, so they hold exactly while the state
they describe holds, and nobody has to close them:

  screening          a full-text decision is undecided (awaiting) and no person has decided
  screening_unclear  a record was included by pipeline.fulltext_unclear: include although criteria stayed unclear
  reassess           a person decided a record and the agent's latest reading differs (agent_decision)
  source_changed     a risk-of-bias assessment a person confirmed rests on a full text that changed since
  manual_edit        a person edited an extraction or a risk-of-bias assessment and outputs that use it are out of
                     date with the edit: answered by updating them (a proposal) or by marking them current

Each note has the shape of an open flag (id, text, status, source, actor, derived=True) so the report, the study
page and the To-do list show stored flags and derived notes alike. Stored flags remain for what is an event, not a
state: a self-check result, an unrecognised term, an eligibility doubt, a disagreement with feedback.
"""
from __future__ import annotations

SOURCES = ("screening", "screening_unclear", "reassess", "source_changed", "manual_edit")


def _note(nid: str, source: str, text: str, **extra) -> dict:
    return {"id": f"N:{source}", "node": nid, "text": text, "status": "open", "source": source, "actor": "agent",
            "derived": True, **extra}


def notes(st) -> list[dict]:
    """Every derived note of the review."""
    from . import graph
    from .selection import decision_node
    from .stages.screening import criterion_reasons
    protocol = (st.get("protocol") or {}).get("data") or {}
    out = []
    for n in st.by_type("eligibility", active_only=True) + st.by_type("screen", active_only=True):
        rid = n["id"].split(":", 1)[1]
        if decision_node(st, rid) is not n:
            continue
        d = n["data"]
        hd = d.get("human_decision") or {}
        if n["type"] == "eligibility" and d.get("decision") == "awaiting" and not hd:
            v = d.get("screen_verify") or {}
            if v.get("outcome") == "pending_disagreement":
                text = (f"Awaiting classification: the screener and the verifier disagree after full-text screening "
                        f"(verifier: {v.get('decision')}; {v.get('explanation', '')[:200]}). Please decide.")
            else:
                text = (f"Awaiting classification: criteria {d.get('because')} unclear after full-text screening "
                        f"(source: {st.text_of(rid)[1]}). Please decide. {criterion_reasons(protocol, d)}").rstrip()
            out.append(_note(n["id"], "screening", text))
        elif n["type"] == "eligibility" and d.get("decision") == "include" and d.get("unclear_by_rule") == "include" and not hd:
            out.append(_note(n["id"], "screening_unclear",
                             f"Included by rule although criteria {d.get('because')} were still unclear after full-text "
                             f"screening (pipeline.fulltext_unclear: include). Check this inclusion. "
                             f"{criterion_reasons(protocol, d)}".rstrip()))
        ad = d.get("agent_decision")
        if hd and ad in ("include", "exclude", "forward") and ad != hd.get("decision"):
            out.append(_note(n["id"], "reassess", f"The agent's latest reading says '{ad}'; your decision "
                                                  f"'{hd.get('decision')}' is kept. Please check."))
    for n in st.by_type("rob", active_only=True):
        if n["data"].get("human_confirmed"):
            moved = [r["dep"] for r in graph.stale_reasons(st, n["id"]) if r["dep"].startswith("ft:")]
            if moved:
                out.append(_note(n["id"], "source_changed", "The full text changed after you confirmed this assessment; "
                                 "it is kept. Ask for a re-assessment if the new text should change it."))
    for n in st.by_type("extraction", active_only=True) + st.by_type("rob", active_only=True):
        e = edit_state(st, n)
        if e:
            out.append(_note(n["id"], "manual_edit",
                             f"You edited this output. {len(e['dependents'])} dependent output(s) may now be out of date: "
                             f"{', '.join(e['dependents'])}", from_version=e["from_version"], dependents=e["dependents"]))
    return out


def _edited(n: dict) -> bool:
    d = n["data"]
    if n["type"] == "extraction":
        return any(isinstance(v, dict) and v.get("edited") for v in (d.get("fields") or {}).values())
    return n["type"] == "rob" and any(x.get("human_note") is not None or x.get("judgment_agent") is not None
                                      for x in d.get("domains") or [])


def edit_state(st, n: dict) -> dict | None:
    """For an output a person edited: the outputs that use it and are out of date with it (not the rendered report),
    and the version they last saw (what an update proposal revises from). None when there is nothing to do."""
    if n["status"] != "active" or not _edited(n):
        return None
    if any(f["status"] == "open" and "pending review" in f.get("text", "") for f in n["flags"]):
        return None      # an update proposal for it waits for the person
    stale = []
    seen = []
    for cid in st.children(n["id"]):
        c = st.get(cid)
        if not c or c["status"] != "active" or c["type"] == "report":
            continue
        v = (c.get("dep_versions") or {}).get(n["id"])
        if v is not None and v < n["version"]:
            stale.append(cid)
            seen.append(v)
    return {"dependents": sorted(stale), "from_version": min(seen)} if stale else None


def by_node(st) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for x in notes(st):
        out.setdefault(x["node"], []).append(x)
    return out


def open_flags(st, n: dict, derived: dict[str, list[dict]] | None = None) -> list[dict]:
    """A node's open stored flags and its derived notes."""
    d = by_node(st) if derived is None else derived
    return [f for f in n["flags"] if f["status"] == "open"] + d.get(n["id"], [])


def settle_legacy(st) -> int:
    """Schema 5: flags of these kinds stored by earlier versions are resolved (the notes are derived now)."""
    k = 0
    for n in st.nodes.values():
        for f in n["flags"]:
            if f["status"] == "open" and f.get("source") in SOURCES:
                f.update({"status": "resolved", "resolution": "derived from the graph since schema 5", "by": "system"})
                k += 1
    return k
