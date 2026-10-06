"""ReviewStore: every review output is a versioned node in a dependency graph.

Node ids follow a simple convention so that humans can reference them in feedback:

  protocol                 review question, PICO, eligibility criteria
  rec:<rid>                a bibliographic record (title/abstract/ids)
  screen:<rid>             title/abstract (and optionally full-text) decision
  ft:<rid>                 full-text document (open-access) or abstract fallback
  ext:<rid>                extracted study characteristics and findings
  rob:<rid>                risk-of-bias draft assessment
  syn:<k>                  one narrative synthesis statement
  report                   the rendered report (deterministic from the nodes above)

Edges point from a node to the nodes it was derived from (`depends_on`).
`descendants()` gives everything downstream of a change; `related()` adds the
other outputs derived from the same study, which is what a dependency-aware
revision must revisit (e.g. a corrected population can change the eligibility
decision as well as the synthesis).
"""
from __future__ import annotations

import copy
import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable


def study_of(node_id: str) -> str | None:
    if ":" in node_id:
        kind, rest = node_id.split(":", 1)
        if kind in {"rec", "screen", "ft", "ext", "rob"}:
            return rest
    return None


class ReviewStore:
    def __init__(self, run_dir: str | Path | None = None):
        self.run_dir = Path(run_dir) if run_dir else None
        self.nodes: dict[str, dict] = {}
        self.changelog: list[dict] = []
        self.meta: dict[str, Any] = {}

    # ------------------------------------------------------------ basic ops
    def upsert(self, node_id: str, ntype: str, data: dict, *, depends_on: Iterable[str] = (),
               evidence: list[dict] | None = None, status: str = "active",
               actor: str = "agent", reason: str = "created") -> dict:
        now = time.time()
        old = self.nodes.get(node_id)
        if old is None:
            node = {"id": node_id, "type": ntype, "data": data, "depends_on": sorted(set(depends_on)),
                    "evidence": evidence or [], "status": status, "version": 1, "history": [],
                    "flags": [], "updated": now}
            self.nodes[node_id] = node
            self._log("create", node_id, actor, reason, None, data)
            return node
        old["history"].append({"version": old["version"], "data": copy.deepcopy(old["data"]),
                               "evidence": copy.deepcopy(old["evidence"]), "status": old["status"],
                               "ts": old["updated"]})
        prev = copy.deepcopy(old["data"])
        old["data"] = data
        old["depends_on"] = sorted(set(depends_on)) if depends_on else old["depends_on"]
        if evidence is not None:
            old["evidence"] = evidence
        old["status"] = status
        old["version"] += 1
        old["updated"] = now
        self._log("update", node_id, actor, reason, prev, data)
        return old

    def set_status(self, node_id: str, status: str, actor="agent", reason=""):
        n = self.nodes[node_id]
        if n["status"] != status:
            self._log("status", node_id, actor, reason, n["status"], status)
            n["status"] = status

    def flag(self, node_id: str, text: str, actor="user", source="feedback") -> dict:
        f = {"id": f"F{sum(len(n['flags']) for n in self.nodes.values()) + 1}", "text": text,
             "status": "open", "actor": actor, "source": source, "ts": time.time()}
        self.nodes[node_id]["flags"].append(f)
        self._log("flag", node_id, actor, text, None, f)
        return f

    def resolve_flag(self, node_id: str, flag_id: str, resolution: str, actor="agent"):
        for f in self.nodes[node_id]["flags"]:
            if f["id"] == flag_id:
                f["status"] = "resolved"
                f["resolution"] = resolution
                self._log("resolve_flag", node_id, actor, resolution, flag_id, None)

    def _log(self, op, node_id, actor, reason, before, after):
        self.changelog.append({"ts": time.time(), "op": op, "node": node_id, "actor": actor,
                               "reason": reason, "before": before, "after": after})

    def get(self, node_id: str) -> dict | None:
        return self.nodes.get(node_id)

    def by_type(self, ntype: str, active_only: bool = False) -> list[dict]:
        out = [n for n in self.nodes.values() if n["type"] == ntype]
        if active_only:
            out = [n for n in out if n["status"] == "active"]
        return sorted(out, key=lambda n: n["id"])

    # ------------------------------------------------------------ graph
    def children(self, node_id: str) -> list[str]:
        return sorted(n["id"] for n in self.nodes.values() if node_id in n["depends_on"])

    def descendants(self, node_id: str) -> list[str]:
        seen, q = set(), deque([node_id])
        while q:
            cur = q.popleft()
            for ch in self.children(cur):
                if ch not in seen:
                    seen.add(ch)
                    q.append(ch)
        return sorted(seen)

    def related(self, node_id: str) -> list[str]:
        """Outputs that a dependency-aware revision should revisit for this node.

        = descendants of the node  ∪  other nodes derived from the same study
          ∪ their descendants. The rendered report is excluded (it is regenerated).
        """
        out = set(self.descendants(node_id))
        sid = study_of(node_id)
        if sid:
            for kind in ("screen", "ext", "rob"):
                nid = f"{kind}:{sid}"
                if nid in self.nodes and nid != node_id:
                    out.add(nid)
                    out.update(self.descendants(nid))
        out.discard(node_id)
        out.discard("report")
        return sorted(out)

    # ------------------------------------------------------------ persistence
    def save(self, path: str | Path | None = None):
        path = Path(path) if path else self.run_dir / "store.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"meta": self.meta, "nodes": self.nodes}, f, indent=1)
        with open(path.with_name(path.stem + "_changelog.jsonl"), "w") as f:
            for e in self.changelog:
                f.write(json.dumps(e) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> "ReviewStore":
        path = Path(path)
        s = cls(path.parent)
        d = json.loads(path.read_text())
        s.nodes, s.meta = d["nodes"], d.get("meta", {})
        cl = path.with_name(path.stem + "_changelog.jsonl")
        if cl.exists():
            s.changelog = [json.loads(l) for l in cl.read_text().splitlines() if l.strip()]
        return s

    def clone(self) -> "ReviewStore":
        s = ReviewStore(self.run_dir)
        s.nodes = copy.deepcopy(self.nodes)
        s.meta = copy.deepcopy(self.meta)
        return s

    # ------------------------------------------------------------ helpers
    def included_ids(self) -> list[str]:
        """Record ids whose final eligibility decision is include."""
        out = []
        for n in self.by_type("screen"):
            if n["status"] == "active" and n["data"].get("decision") == "include":
                out.append(n["id"].split(":", 1)[1])
        return sorted(out)

    def text_of(self, rid: str) -> tuple[str, str]:
        """Best available source text for a study: (text, kind)."""
        ft = self.get(f"ft:{rid}")
        if ft and ft["data"].get("text"):
            return ft["data"]["text"], ft["data"].get("kind", "fulltext")
        rec = self.get(f"rec:{rid}")
        if rec:
            d = rec["data"]
            return f"{d.get('title', '')}\n\n{d.get('abstract', '')}", "abstract"
        return "", "none"
