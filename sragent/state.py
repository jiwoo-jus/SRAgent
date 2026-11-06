"""Review state that lived in store.meta, kept as graph nodes (docs/refactor/04-refactor-plan.md §3, phase 2b).

Each piece is a node with real edges, so the dependency graph explains when it no longer holds:

  approval:protocol   a person approved the protocol           -> protocol   (stale = protocol changed since)
  strategy            the search strategy chosen with the team -> protocol   (search -> strategy)
  cohort              the included set                         (content-versioned: same set, same version)
  syn_scope           the included set the synthesis took in   -> cohort     (stale = studies to integrate/remove)
  snowball            citation chasing: seeds, records added   -> cohort
  pdf:<rid>           a person asked for / gave up on a PDF    -> rec:<rid>  (not on ft:<rid>: a request is not a
                                                                              new full text, and extraction, risk of
                                                                              bias and screening depend on ft:<rid>)
  rules               the screening rules last applied          -> cfg:screening_rules

What stays in meta (tests/test_operations.py::META_ALLOWED checks it): presentation and history (snoozed, comments,
protocol_revisions: the conversation shown on the protocol card), the settings lock (config_lock, config_changes),
counters (flag_seq, schema) and job bookkeeping (rob_reassess: where a long redraft resumes; gold_input,
verified_extraction_input: evaluation runs).
"""
from __future__ import annotations

import copy
import time

APPROVAL = "approval:protocol"
STRATEGY = "strategy"
COHORT = "cohort"
SCOPE = "syn_scope"
SNOWBALL = "snowball"
RULES = "rules"


def _data(st, nid: str) -> dict | None:
    n = st.get(nid)
    return n["data"] if n and n["status"] == "active" else None


def _current(st, nid: str, dep: str) -> bool:
    """The node saw the dependency's current version."""
    n, d = st.get(nid), st.get(dep)
    return bool(n and d and n["status"] == "active" and (n.get("dep_versions") or {}).get(dep) == d["version"])


# ---------------------------------------------------------------- protocol approval
def approval(st) -> dict | None:
    """The approval of the protocol as it is now (None when never approved, or approved before it last changed)."""
    return copy.deepcopy(_data(st, APPROVAL)) if _current(st, APPROVAL, "protocol") else None


def approve(st, note: str = "", actor: str = "user") -> None:
    st.write(APPROVAL, "approval", {"note": note, "ts": time.time(), "by": actor}, depends_on=["protocol"],
             actor=actor, reason="protocol approved")


# ---------------------------------------------------------------- search strategy
def strategy(st) -> dict:
    """The strategy notes: variants (the agent's query and alternatives with counts), the chat, the draft and the
    confirmed query. A copy: change it and save it with save_strategy()."""
    return copy.deepcopy(_data(st, STRATEGY) or {"variants": [], "chat": [], "confirmed": None})


def save_strategy(st, data: dict, actor: str = "agent", reason: str = "search strategy") -> None:
    st.write(STRATEGY, "strategy", data, depends_on=["protocol"], actor=actor, reason=reason)


def confirmed_query(st) -> dict | None:
    c = (_data(st, STRATEGY) or {}).get("confirmed") or {}
    return copy.deepcopy(c) if c.get("query") else None


# ---------------------------------------------------------------- cohort and what the synthesis took in
def refresh_cohort(st) -> None:
    """The included set as a node; a new version only when the set changes."""
    inc = st.included_ids()
    if (_data(st, COHORT) or {}).get("included") != inc:
        st.write(COHORT, "cohort", {"included": inc}, actor="system", reason="included set changed")


def considered(st) -> list[str] | None:
    """The studies the synthesis took into account (None: no synthesis has recorded it)."""
    d = _data(st, SCOPE)
    return list(d["considered"]) if d else None


def set_considered(st, ids, written_with: dict | None = None) -> None:
    """The synthesis took these studies in. `written_with`: what the first synthesis rested on (kept afterwards)."""
    refresh_cohort(st)
    d = {"considered": sorted(ids)}
    w = written_with or (_data(st, SCOPE) or {}).get("written_with")
    if w:
        d["written_with"] = w
    st.write(SCOPE, "synthesis_scope", d, depends_on=[COHORT], actor="agent", reason="studies taken into the synthesis")


# ---------------------------------------------------------------- citation chasing
def snowball(st) -> dict | None:
    return copy.deepcopy(_data(st, SNOWBALL))


def save_snowball(st, data: dict) -> None:
    refresh_cohort(st)
    st.write(SNOWBALL, "snowball", data, depends_on=[COHORT], reason="citation chasing")


# ---------------------------------------------------------------- full texts a person asked for or gave up on
def pdf_request(st, rid: str) -> dict:
    return copy.deepcopy(_data(st, f"pdf:{rid}") or {})


def set_pdf_request(st, rid: str, actor: str = "user", **fields) -> None:
    """fields: wanted (a person waits for this PDF), unavailable (declared not obtainable)."""
    old = pdf_request(st, rid)
    d = {**old, **fields}
    if d == old or (not st.get(f"pdf:{rid}") and not any(fields.values())):   # nothing to record
        return
    st.annotate(f"pdf:{rid}", d, ntype="pdf_request", depends_on=[f"rec:{rid}"], actor=actor,
                reason=", ".join(f"{k}: {v}" for k, v in fields.items()))


def wanted(st) -> set[str]:
    return {n["id"].split(":", 1)[1] for n in st.by_type("pdf_request", active_only=True) if n["data"].get("wanted")}


def unavailable(st) -> set[str]:
    return {n["id"].split(":", 1)[1] for n in st.by_type("pdf_request", active_only=True) if n["data"].get("unavailable")}


# ---------------------------------------------------------------- screening rules
def rules_applied(st) -> dict | None:
    """The rules last applied (rules, version, n), None when never applied or the rule setting changed since."""
    return copy.deepcopy(_data(st, RULES)) if _current(st, RULES, "cfg:screening_rules") or \
        (st.get(RULES) and not st.get("cfg:screening_rules")) else None


def save_rules_applied(st, data: dict) -> None:
    st.write(RULES, "rules_run", data, depends_on=["cfg:screening_rules"] if st.get("cfg:screening_rules") else [],
             actor="rule", reason="screening rules applied")
