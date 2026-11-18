"""The dependency graph as an explicit model: node types, what each is derived from, and staleness.

Every output is a node whose `depends_on` names the nodes it was derived from. A node also records
`dep_versions`, the version of each dependency at the time the node was computed, so "is this node
out of date?" is a comparison, not a guess: a node is stale when a dependency has a newer version that changed
the part this node reads (READS: the search reads the protocol's question, PICO and key concepts, not its
criteria), was retired, or is gone.

The review's settings are nodes too (SETTINGS below: the question, the eligibility text, the screening
rules, the record source, the people mode, one node per extraction field and per risk-of-bias domain).
They are written from the config when a run starts (`sync_settings`) and change version only when the
value changes, so an accepted settings change (config_lock) makes exactly the outputs that used the old
value stale, and nothing else.

TYPES says, for each node type, what it depends on and how it is brought up to date. Step 1 of the
refactor (this module) makes the graph explicit and queryable; the engine that plans and executes the
updates is step 2. Until then `stale()` is a report, used by `sragent status` and the web interface.
"""
from __future__ import annotations

import json
from typing import Iterable

SCHEMA = 6   # the store format this version reads and writes (an older store is refused: context.Ctx.create)

# How a node type is brought up to date when a dependency changed (the vocabulary of the step-2 engine).
#   settings   a value from the config: changes only by a settings change (config_lock)
#   derive     written once from settings by the agent (protocol, search); a changed input needs a re-plan
#   assess     per-criterion assessments: a changed criterion is re-assessed for the records it applies to
#   rederive   the decision follows from the assessments by a fixed rule, no model call
#   recompute  computed from the source and the field/domain definitions: compute again
#   revise     text that cites other outputs: revise only the statements the change affects
#   render     deterministic from the nodes above: render again
TYPES: dict[str, dict] = {
    "setting":    {"depends": [], "update": "settings"},
    "protocol":   {"depends": ["cfg:question", "cfg:seed_eligibility"], "update": "derive"},
    "criterion":  {"depends": ["protocol"], "update": "derive"},
    "search":     {"depends": ["protocol", "cfg:records", "strategy?"], "update": "derive"},
    "record":     {"depends": ["search"], "update": "derive"},
    "fulltext":   {"depends": ["rec:<rid>"], "update": "recompute"},
    "screen":     {"depends": ["rec:<rid>", "protocol", "crit:*?", "cfg:screening_rules?"], "update": "assess"},
    "eligibility": {"depends": ["screen:<rid>", "ft:<rid>?", "protocol", "crit:*?", "cfg:fulltext_unclear?"], "update": "assess"},
    "extraction": {"depends": ["elig:<rid>", "ft:<rid>|rec:<rid>", "field:*?", "cfg:question"], "update": "recompute"},
    "rob":        {"depends": ["ext:<rid>", "ft:<rid>|rec:<rid>", "dom:*?", "cfg:rob_tool"], "update": "recompute"},
    "synthesis":  {"depends": ["ext:*?", "rob:*?", "cfg:question"], "update": "revise"},
    "report":     {"depends": ["syn:*?"], "update": "render"},
    # review state that lived in store.meta (state.py)
    "approval":   {"depends": ["protocol"], "update": "person"},            # stale: the protocol changed since approval
    "strategy":   {"depends": ["protocol"], "update": "person"},            # the search strategy chosen with the team
    "cohort":     {"depends": [], "update": "derived"},                     # the included set
    "synthesis_scope": {"depends": ["cohort"], "update": "integrate"},      # stale: studies to integrate or remove
    "snowball":   {"depends": ["cohort"], "update": "derive"},
    "pdf_request": {"depends": ["rec:<rid>"], "update": "person"},
    "rules_run":  {"depends": ["cfg:screening_rules?"], "update": "derive"},
}
# How to read "depends": an id is required as written; <rid> is the node's own study; `a|b` is one of them; `x:*` any
# number of nodes with that prefix (at least one; `x:*?` also none); a trailing `?` makes any edge optional.
# tests/test_offline.py::test_written_edges_are_the_declared_edges checks every node the pipeline and the operations write.


def edge_problems(node: dict) -> list[str]:
    """How a node's written edges differ from what its type declares (empty when they agree)."""
    spec = TYPES.get(node["type"])
    if spec is None:
        return [f"type {node['type']} is not declared in graph.TYPES"]
    rid = node["id"].split(":", 1)[1] if ":" in node["id"] else ""
    deps, used, out = list(node.get("depends_on") or []), set(), []
    for pat in spec["depends"]:
        optional = pat.endswith("?")
        alts = [a.replace("<rid>", rid) for a in pat.rstrip("?").split("|")]
        hit = [d for d in deps if any(d == a or (a.endswith(":*") and d.startswith(a[:-1])) for a in alts)]
        if not hit and not optional:
            out.append(f"missing {pat}")
        used |= set(hit)
    out += [f"undeclared {d}" for d in deps if d not in used]
    return out


HUMAN_MARKS = ("human_decision", "human_decision_pending_fulltext", "human_confirmed", "edited")


# ---------------------------------------------------------------- settings nodes

def settings_from_config(cfg: dict) -> dict[str, dict]:
    """{node id: data} for the review-defining settings (the same values config_lock fixes)."""
    topic = cfg.get("topic") or {}
    p = cfg.get("pipeline") or {}
    out = {
        "cfg:question": {"value": topic.get("question", "")},
        "cfg:seed_eligibility": {"value": topic.get("seed_eligibility", "")},
        "cfg:screening_rules": {"value": topic.get("screening_rules")},
        "cfg:records": {"value": topic.get("records") or {}},
        "cfg:human_mode": {"value": p.get("human_mode")},
        "cfg:fulltext_unclear": {"value": p.get("fulltext_unclear")},
        "cfg:rob_tool": {"value": topic.get("rob_tool_name", "")},
    }
    for f in topic.get("extraction_fields") or []:
        if isinstance(f, dict) and f.get("name"):
            out[f"field:{f['name']}"] = {k: v for k, v in f.items()}
    for d in topic.get("rob_domains") or []:
        if isinstance(d, dict) and d.get("name"):
            out[f"dom:{d['name']}"] = {k: v for k, v in d.items()}
    return json.loads(json.dumps(out, default=str))


def sync_settings(store, cfg: dict, actor: str = "config") -> list[str]:
    """Write the settings nodes; a node's version changes only when its value changed. A field or domain
    no longer in the config is retired. Returns the ids that changed."""
    want = settings_from_config(cfg)
    changed = []
    for nid, data in want.items():
        old = store.get(nid)
        if old is None or old["data"] != data or old["status"] != "active":
            store.write(nid, "setting", data, actor=actor, reason="review setting" if old is None else "review setting changed")
            changed.append(nid)
    for n in store.by_type("setting"):
        if n["id"] not in want and n["status"] == "active":
            store.set_status(n["id"], "retired", actor=actor, reason="removed from the review settings")
            changed.append(n["id"])
    return changed


def field_deps(cfg: dict) -> list[str]:
    return [f"field:{f['name']}" for f in (cfg.get("topic") or {}).get("extraction_fields") or []
            if isinstance(f, dict) and f.get("name")] + ["cfg:question"]


def domain_deps(cfg: dict) -> list[str]:
    return [f"dom:{d['name']}" for d in (cfg.get("topic") or {}).get("rob_domains") or []
            if isinstance(d, dict) and d.get("name")] + ["cfg:rob_tool"]


# ---------------------------------------------------------------- staleness

def human_pinned(node: dict) -> bool:
    """A person decided, confirmed or edited this node: an update must not overwrite it (it is proposed)."""
    d = node.get("data") or {}
    if any(d.get(k) for k in HUMAN_MARKS):
        return True
    return any(isinstance(v, dict) and v.get("edited") for v in (d.get("fields") or {}).values())


def _protocol_for_search(d: dict):
    return {k: d.get(k) for k in ("question", "pico", "key_concepts")}            # search.build_query, protocol_brief


def _row_columns(v: dict):
    """A rows item's rows without their located sentences (a change of a number or a column counts; re-locating
    the same evidence does not)."""
    return [{k: x for k, x in r.items() if k not in ("spans", "quote", "check")} for r in (v or {}).get("rows") or []]


def _extracted_values(d: dict):
    return {k: ((v or {}).get("value"), _row_columns(v)) for k, v in (d.get("fields") or {}).items()}    # rob._ext_brief


def _extracted_with_quotes(d: dict):
    return {k: ((v or {}).get("value"), (v or {}).get("quote", ""), _row_columns(v))
            for k, v in (d.get("fields") or {}).items()}   # extraction.table_text


def _record_content(d: dict):
    """What a record says about the study, as screening, extraction and quality assessment read it. Its
    bibliographic details (journal, volume, issue, pages) are for the reference list, so completing them does not
    make a decision out of date."""
    return {k: d.get(k) for k in ("title", "abstract", "pub_types", "doi", "pmcid", "year", "authors")}


def _rob_ratings(d: dict):
    return d.get("overall"), [(x.get("domain"), x.get("judgment")) for x in d.get("domains") or []]   # rob.rob_text


# The part of an input that an output of a type reads. A new version of the input makes the output out of date only
# when this part changed: a criterion edit does not reach the search, a confirmation does not change a risk-of-bias
# rating. A pair not listed reads the whole input.
READS = {
    ("search", "protocol"): _protocol_for_search,
    ("screen", "record"): _record_content,
    ("eligibility", "record"): _record_content,
    ("fulltext", "record"): _record_content,
    ("extraction", "record"): _record_content,
    ("rob", "record"): _record_content,
    ("pdf_request", "record"): _record_content,
    ("strategy", "protocol"): _protocol_for_search,
    ("rob", "extraction"): _extracted_values,
    ("synthesis", "extraction"): _extracted_with_quotes,
    ("synthesis", "rob"): _rob_ratings,
}


def data_at(node: dict, version: int) -> dict | None:
    """A node's data as it was at a version (None when that version is not in its history)."""
    if version == node["version"]:
        return node["data"]
    return next((h["data"] for h in node.get("history", []) if h["version"] == version), None)


def _read_part_changed(n: dict, d: dict, seen: int) -> bool:
    part = READS.get((n["type"], d["type"]))
    old = data_at(d, seen) if part else None
    if old is None:
        return True
    try:
        return part(old) != part(d["data"])
    except (AttributeError, TypeError):
        return True


def stale_reasons(store, node_id: str) -> list[dict]:
    """Why this node is out of date: each dependency that moved on since the node was computed, in the part this
    node reads (READS)."""
    n = store.get(node_id)
    if not n or n["status"] != "active":
        return []
    rec = n.get("dep_versions") or {}
    out = []
    for dep in n.get("depends_on", []):
        d = store.get(dep)
        if d is None:
            out.append({"dep": dep, "why": "missing"})
        elif d["status"] not in ("active",):
            out.append({"dep": dep, "why": d["status"]})
        elif dep in rec and d["version"] > rec[dep] and _read_part_changed(n, d, rec[dep]):
            out.append({"dep": dep, "why": f"v{rec[dep]} -> v{d['version']}"})
    return out


def stale(store, node_types: Iterable[str] | None = None) -> dict[str, list[dict]]:
    """{node id: reasons} for every active node with a dependency that changed after it was computed.
    Settings nodes and records are never stale (they are inputs)."""
    out = {}
    for n in store.nodes.values():
        if n["status"] != "active" or n["type"] in ("setting", "record"):
            continue
        if node_types and n["type"] not in node_types:
            continue
        r = stale_reasons(store, n["id"])
        if r:
            out[n["id"]] = r
    return out


def stale_summary(store) -> dict:
    s = stale(store)
    by_type: dict[str, int] = {}
    for nid in s:
        t = store.get(nid)["type"]
        by_type[t] = by_type.get(t, 0) + 1
    return {"n": len(s), "by_type": dict(sorted(by_type.items())),
            "pinned": sum(1 for nid in s if human_pinned(store.get(nid)))}
