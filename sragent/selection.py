"""Where a study's eligibility stands, from its two decision nodes (phase 2c; no imports from the package).

  screen:<rid>   title/abstract screening (stage 3): 'forward' or 'exclude' (a rule exclusion too)
  elig:<rid>     full-text screening (stage 5): 'include' | 'exclude' | 'awaiting'; a person's eligibility decision
                 is recorded here too, with or without a full text
  ft:<rid>       retrieval (stage 4): a forwarded study whose full text could not be obtained or read is
                 'fulltext_unavailable' / 'parse_failed', derived from this node, never stored as a decision

The node in charge of a study (`decision_node`): its elig node when the study was forwarded (or a person decided
there), else its screen node. Once a study has an elig node its screen node is superseded: a protocol change is
re-derived and re-assessed on the full-text assessments, as when one node held both stages.

`view()` gives the combined record every reader used before the split (decision, stage, because, criteria, ...).
"""
from __future__ import annotations

OPEN_AT_TA = ("forward", "include")         # passed title/abstract ('include': an earlier person's decision there)
RETRIEVAL_FAILURES = ("fulltext_unavailable", "parse_failed")
HUMAN_KEYS = ("human_decision", "human_decision_pending_fulltext")


def screen_id(rid: str) -> str:
    return f"screen:{rid}"


def elig_id(rid: str) -> str:
    return f"elig:{rid}"


def _active(st, nid):
    n = st.get(nid)
    return n if n and n["status"] == "active" else None


def human_decided(d: dict) -> bool:
    return any(d.get(k) for k in HUMAN_KEYS)


def forwarded(st, rid: str) -> bool:
    sc = _active(st, screen_id(rid))
    return bool(sc and sc["data"].get("decision") in OPEN_AT_TA)


def decision_node(st, rid: str):
    """The node that holds the study's current eligibility decision (None: not screened)."""
    el = _active(st, elig_id(rid))
    sc = st.get(screen_id(rid))
    if sc is not None and sc["status"] != "active":
        sc = None              # cleared (retired): not screened at title/abstract any more
    if el and (sc is None or sc["data"].get("decision") in OPEN_AT_TA or human_decided(el["data"])):
        return el
    return sc


def _retrieval(st, rid: str) -> tuple[str, str] | None:
    """(status, note) when the study's full text was looked for and is not usable; None otherwise."""
    ft = (st.get(f"ft:{rid}") or {}).get("data")
    if not ft or ft.get("kind") == "fulltext":
        return None
    if ft.get("parse_failed"):
        return "parse_failed", f"the supplied file {ft.get('local_file')} has no usable text"
    return "fulltext_unavailable", "no open-access full text found; PDF requested"


def view(st, rid: str) -> dict | None:
    """The study's eligibility as one record (a copy): the data of its decision node, with the retrieval state of a
    forwarded study that has no usable full text, and the title/abstract decision kept as `title_abstract`."""
    n = decision_node(st, rid)
    if n is None:
        return None
    d = dict(n["data"])
    sc = st.get(screen_id(rid))
    if n["id"].startswith("elig:"):
        if sc:
            d.setdefault("title_abstract", {k: sc["data"].get(k) for k in ("decision", "because")})
        hd = d.get("human_decision") or {}
        r = _retrieval(st, rid)
        if r and (d.get("pending_fulltext") or (hd.get("decision") == "include")):
            # a person's inclusion whose full text went missing: it applies again once the text is back
            d["human_decision_pending_fulltext"] = d.pop("human_decision", None) or d.get("human_decision_pending_fulltext")
            d.update({"decision": r[0], "stage": "retrieval", "because": [], "retrieval_note": r[1]})
        return d
    if d.get("decision") in OPEN_AT_TA and not d.get("rule"):
        r = _retrieval(st, rid)
        if r:
            d["title_abstract"] = {k: d.get(k) for k in ("decision", "because")}
            d.update({"decision": r[0], "stage": "retrieval", "because": [], "retrieval_note": r[1]})
    return d


def screened(st) -> list[tuple[str, dict]]:
    """(rid, view) for every screened study, in screening order."""
    out = []
    for n in st.by_type("screen"):
        rid = n["id"].split(":", 1)[1]
        v = view(st, rid)
        if v is not None:
            out.append((rid, v))
    return out


def included(st) -> list[str]:
    """Record ids whose final eligibility decision is include."""
    return sorted(rid for rid, v in screened(st) if v.get("decision") == "include")


def node_id(st, rid: str) -> str:
    """Id of the decision node (for flags, links and previews); the screen node when the study is not screened yet."""
    n = decision_node(st, rid)
    return n["id"] if n else screen_id(rid)


def study_node(st, rid: str) -> dict | None:
    """The study's eligibility in the shape of a node, for readers that used the one screen node: `data` is view(),
    `flags`, `status`, `version` come from the decision node, `id` is the study key screen:<rid> and `node_id` the
    decision node's own id."""
    n = decision_node(st, rid)
    if n is None:
        return None
    return {"id": screen_id(rid), "node_id": n["id"], "type": "screen", "data": view(st, rid), "flags": n["flags"],
            "status": n["status"], "version": n["version"], "depends_on": n["depends_on"]}


def study_nodes(st, active_only: bool = False) -> list[dict]:
    """study_node() for every screened study, in screening order."""
    out = []
    for n in st.by_type("screen"):
        s = study_node(st, n["id"].split(":", 1)[1])
        if s is not None and (not active_only or s["status"] == "active"):
            out.append(s)
    return out
