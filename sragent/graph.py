"""The dependency graph as an explicit model: node types, what each is derived from, and staleness.

Every output is a node whose `depends_on` names the nodes it was derived from. Since schema 2 a node also
records `dep_versions`, the version of each dependency at the time the node was computed, so "is this node
out of date?" is a comparison, not a guess: a node is stale when a dependency has a newer version, was
retired, or is gone.

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

import copy
import json
from typing import Iterable

SCHEMA = 2

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
    "search":     {"depends": ["protocol", "cfg:records"], "update": "derive"},
    "record":     {"depends": ["search"], "update": "derive"},
    "fulltext":   {"depends": ["rec:<rid>"], "update": "recompute"},
    "screen":     {"depends": ["rec:<rid>", "protocol", "crit:*", "ft:<rid> (full text)", "cfg:screening_rules", "cfg:human_mode"],
                   "update": "assess"},
    "extraction": {"depends": ["screen:<rid>", "ft:<rid> or rec:<rid>", "field:*", "cfg:question"], "update": "recompute"},
    "rob":        {"depends": ["ext:<rid>", "ft:<rid> or rec:<rid>", "dom:*", "cfg:rob_tool"], "update": "recompute"},
    "synthesis":  {"depends": ["ext:<rid>, rob:<rid> of the cited studies", "cfg:question"], "update": "revise"},
    "report":     {"depends": ["syn:*"], "update": "render"},
}

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
            store.upsert(nid, "setting", data, actor=actor, reason="review setting" if old is None else "review setting changed")
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


def stale_reasons(store, node_id: str) -> list[dict]:
    """Why this node is out of date: each dependency that moved on since the node was computed."""
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
        elif dep in rec and d["version"] > rec[dep]:
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


# ---------------------------------------------------------------- migration

def migrate(store, cfg: dict | None = None) -> list[str]:
    """Bring a store from an older schema to SCHEMA. Idempotent. Returns what was done."""
    done = []
    schema = int(store.meta.get("schema") or 1)
    if schema < 2:
        # dep_versions: nodes made before the field existed are taken as up to date with their inputs
        for n in store.nodes.values():
            if "dep_versions" not in n:
                n["dep_versions"] = {d: store.nodes[d]["version"] for d in n.get("depends_on", []) if d in store.nodes}
        done.append("dep_versions recorded for every node (taken as current)")
        if cfg is not None:
            sync_settings(store, cfg)
            done.append("settings nodes written from the config")
            # extraction and risk-of-bias outputs depend on the field / domain definitions, not the whole protocol
            fd, dd = field_deps(cfg), domain_deps(cfg)
            for n in store.by_type("extraction"):
                deps = [d for d in n["depends_on"] if d != "protocol"] + fd
                _set_deps(store, n, deps)
            for n in store.by_type("rob"):
                _set_deps(store, n, list(n["depends_on"]) + dd)
            for n in store.by_type("synthesis"):
                _set_deps(store, n, list(n["depends_on"]) + ["cfg:question"])
            for n in store.by_type("protocol"):
                _set_deps(store, n, list(n["depends_on"]) + ["cfg:question", "cfg:seed_eligibility"])
            for n in store.by_type("search"):
                _set_deps(store, n, list(n["depends_on"]) + ["cfg:records"])
            done.append("edges refined: extraction -> field:*, rob -> dom:*, synthesis/protocol/search -> cfg:*")
        store.meta["schema"] = 2
        store.reindex()
    return done


def _set_deps(store, n: dict, deps: list[str]) -> None:
    """Change a node's edges without a new version (a migration, not a revision); the new edges are
    recorded as current so the migration itself makes nothing stale."""
    new = sorted(set(deps))
    if new == n.get("depends_on"):
        return
    n["depends_on"] = new
    n.setdefault("dep_versions", {})
    for d in new:
        if d in store.nodes and d not in n["dep_versions"]:
            n["dep_versions"][d] = store.nodes[d]["version"]
    store._log("migrate", n["id"], "system", "edges refined (schema 2)", None, new)
