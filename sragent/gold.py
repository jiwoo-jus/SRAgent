"""Benchmark gold labels are for the evaluator only.

The agent's store never holds them: when benchmark records are loaded, `label_included` is moved to
<run_dir>/eval_private/gold.json (with the benchmark search recall, which is also computed from gold).
Prompts, the UI and the reports read only the store, so the agent cannot see a gold label.
Evaluation code reads the labels with `labels()`.
"""
from __future__ import annotations

import json
from pathlib import Path


def _path(run_dir) -> Path:
    return Path(run_dir) / "eval_private" / "gold.json"


def load(run_dir) -> dict:
    p = _path(run_dir)
    return json.loads(p.read_text()) if p.exists() else {"labels": {}, "source": None}


def save(run_dir, g: dict):
    p = _path(run_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(g, indent=1))


def strip_records(run_dir, recs: list[dict], source: str | None = None) -> list[dict]:
    """Remove gold labels from records before they enter the store; keep them in the private file."""
    g = load(run_dir)
    for r in recs:
        if "label_included" in r:
            g["labels"][r["rid"]] = int(r.pop("label_included"))
    if source:
        g["source"] = source
    if g["labels"]:
        save(run_dir, g)
    return recs


def labels(ctx_or_run_dir) -> dict[str, int]:
    """rid -> 1/0 for every labelled record."""
    rd = getattr(ctx_or_run_dir, "run_dir", ctx_or_run_dir)
    return dict(load(rd).get("labels", {}))


def info(ctx_or_run_dir) -> dict:
    g = load(getattr(ctx_or_run_dir, "run_dir", ctx_or_run_dir))
    return {"source": g.get("source"), "benchmark_search_recall": g.get("benchmark_search_recall")}
