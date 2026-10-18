"""Who did what: the two human-involvement conditions, and a separate log of human input.

Conditions (pipeline.human_mode)
  agent_only          every judgment and revision is made by the agent. A person may only supply the
                      full texts the agent requested (PDF / text files in <run_dir>/fulltext/), or say
                      that a requested full text cannot be obtained.
  human_in_the_loop   in addition, people decide eligibility, edit content, confirm risk of bias,
                      approve or reject proposals and send feedback.

Every human input is appended to <run_dir>/provenance.jsonl with a category:
  pdf_provision    a requested full text was supplied (file name, size, sha256) or declared not obtainable
  human_judgment   a decision, edit, approval/rejection, feedback or re-run request by a person
  annotation       a comment saved without asking the agent to act (changes no output)
PDF provision and human judgment are therefore always counted separately. In agent_only mode,
judgment actions are refused (and would be recorded as violations if they happened anyway).
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from pathlib import Path

MODES = ("agent_only", "human_in_the_loop")
CATEGORIES = ("pdf_provision", "human_judgment", "annotation")


class NotAllowed(Exception):
    pass


def mode(ctx) -> str:
    m = (ctx.cfg.get("pipeline") or {}).get("human_mode", "human_in_the_loop")
    return m if m in MODES else "human_in_the_loop"


def _path(run_dir) -> Path:
    return Path(run_dir) / "provenance.jsonl"


def record(ctx, category: str, action: str, **data) -> dict:
    assert category in CATEGORIES, category
    e = {"ts": time.time(), "mode": mode(ctx), "category": category, "action": action, **data}
    if category == "human_judgment" and mode(ctx) == "agent_only":
        e["violation"] = True
    with open(_path(ctx.run_dir), "a") as f:
        f.write(json.dumps(e, default=str) + "\n")
    return e


def require_judgment_allowed(ctx, what: str):
    """Raise NotAllowed in agent_only mode (the person may only provide full texts)."""
    if mode(ctx) == "agent_only":
        raise NotAllowed(f"This review runs in agent-only mode: people may only provide requested full texts. "
                         f"'{what}' is a human judgment and is not allowed here.")


def file_sha(p: Path) -> str:
    """First 16 hex digits of the file's SHA-256."""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def file_digest(p: Path) -> dict:
    return {"file": p.name, "bytes": p.stat().st_size, "sha256": file_sha(p)}


def events(run_dir) -> list[dict]:
    p = _path(run_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def summary(ctx_or_run_dir, cfg_mode: str | None = None) -> dict:
    """Counts per category, kept apart: PDFs supplied vs human judgments."""
    rd = getattr(ctx_or_run_dir, "run_dir", ctx_or_run_dir)
    m = cfg_mode or (mode(ctx_or_run_dir) if hasattr(ctx_or_run_dir, "cfg") else None)
    ev = events(rd)
    pdf = [e for e in ev if e["category"] == "pdf_provision"]
    jud = [e for e in ev if e["category"] == "human_judgment"]
    ann = [e for e in ev if e["category"] == "annotation"]
    return {
        "mode": m,
        "pdf_provision": {"n": len(pdf), "files_supplied": sum(1 for e in pdf if e["action"] == "supplied"),
                          "declared_not_obtainable": sum(1 for e in pdf if e["action"] == "not_obtainable"),
                          "studies": sorted({e.get("rid") for e in pdf if e.get("rid")})},
        "human_judgment": {"n": len(jud), "by_action": dict(Counter(e["action"] for e in jud)),
                           "studies": sorted({e.get("rid") for e in jud if e.get("rid")})},
        "annotation": {"n": len(ann)},
        "violations": sum(1 for e in jud if e.get("violation")),
        "condition_respected": not (m == "agent_only" and jud),
    }
