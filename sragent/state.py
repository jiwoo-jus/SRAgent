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
verified_extraction_input: evaluation runs). A schema-2 review is migrated on load (graph.migrate), its store.json
kept first as store.schema2.bak.json.
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


# ---------------------------------------------------------------- schema 3: meta -> nodes
def migrate_meta(st) -> list[str]:
    """Move the side notes of an older review into nodes (idempotent). The moved meta keys are removed, so nothing
    can read a stale copy."""
    m, done = st.meta, []
    if m.get("protocol_approved") is not None and st.get("protocol") and not st.get(APPROVAL):
        a = m["protocol_approved"] if isinstance(m["protocol_approved"], dict) else {}
        st.write(APPROVAL, "approval", {"note": a.get("note", ""), "ts": a.get("ts"), "by": "user"},
                 depends_on=["protocol"], actor="migrate", reason="protocol approval (from meta)")
        done.append("protocol approval")
    m.pop("protocol_approved", None)
    if m.get("search_strategy") and st.get("protocol") and not st.get(STRATEGY):
        st.write(STRATEGY, "strategy", m["search_strategy"], depends_on=["protocol"], actor="migrate",
                 reason="search strategy (from meta)")
        if st.get("search") and STRATEGY not in st.get("search")["depends_on"]:
            n = st.get("search")
            n["depends_on"] = sorted(set(n["depends_on"]) | {STRATEGY})
            n.setdefault("dep_versions", {})[STRATEGY] = st.get(STRATEGY)["version"]
            st.reindex()
        done.append("search strategy")
    m.pop("search_strategy", None)
    if st.by_type("screen"):
        refresh_cohort(st)
    if m.get("synthesis_considered") is not None and not st.get(SCOPE):
        d = {"considered": sorted(m["synthesis_considered"])}
        if m.get("synthesis_written_with"):
            d["written_with"] = m["synthesis_written_with"]
        st.write(SCOPE, "synthesis_scope", d, depends_on=[COHORT], actor="migrate",
                 reason="studies taken into the synthesis (from meta)")
        done.append("synthesis scope")
    m.pop("synthesis_considered", None)
    m.pop("synthesis_written_with", None)
    if m.get("snowball") and not st.get(SNOWBALL):
        st.write(SNOWBALL, "snowball", m["snowball"], depends_on=[COHORT], actor="migrate", reason="citation chasing (from meta)")
        done.append("citation chasing")
    m.pop("snowball", None)
    cited = [rid for rid, how in (m.get("found_via") or {}).items() if "citation" in (how or [])
             and st.get(f"rec:{rid}") and not st.get(f"rec:{rid}")["data"].get("found_via")]
    if cited and st.get(SNOWBALL):     # records citation chasing found again: noted on the snowball node
        d = snowball(st)
        d["also_cited"] = sorted(set(d.get("also_cited") or []) | set(cited))
        st.write(SNOWBALL, "snowball", d, depends_on=[COHORT], actor="migrate", reason="records found again (from meta)")
    if m.pop("found_via", None) is not None:
        done.append("records citation chasing found again (on the snowball node)")
    for rid in (m.get("pdf_wanted") or {}):
        if st.get(f"rec:{rid}"):
            set_pdf_request(st, rid, actor="migrate", wanted=True)
    for rid in (m.get("pdf_unavailable") or []):
        if st.get(f"rec:{rid}"):
            set_pdf_request(st, rid, actor="migrate", unavailable=True)
    if m.pop("pdf_wanted", None) is not None or m.pop("pdf_unavailable", None) is not None:
        done.append("full-text requests")
    m.pop("pdf_unavailable", None)
    m.pop("waiting_for_pdfs", None)                      # derived now (fulltext.waiting_for_pdfs)
    if m.get("rules_applied") and not st.get(RULES):
        save_rules_applied(st, m["rules_applied"])
        done.append("screening rules applied")
    m.pop("rules_applied", None)
    return done
