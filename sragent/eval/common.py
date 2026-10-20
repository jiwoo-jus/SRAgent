"""Shared evaluation utilities: judge wrapper with logging, metrics, result IO."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from ..context import Ctx
from .stats import _r

JUDGE_SYSTEM = ("You are a strict, careful evaluator of systematic-review outputs. Judge only what is asked, "
                "using the gold reference when one is given. Respond with one JSON object.")


class Judge:
    """Calls the judge model and logs every decision (for judge validation / human audit)."""

    def __init__(self, ctx: Ctx, out_dir: Path, role: str = "judge"):
        self.ctx, self.role = ctx, role
        self.log_path = out_dir / f"{role}_log.jsonl"
        self.out_dir = out_dir

    def __call__(self, kind: str, prompt: str, label_key: str, meta: dict | None = None, tag: str = "judge") -> dict:
        llm = self.ctx.hub[self.role]
        res = llm.chat([{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": prompt}],
                       tag=f"{tag}_{kind}")
        if not isinstance(res, dict):   # not one JSON object: logged without a label instead of failing the run
            res = {"_unparsed": res}
        rec = {"ts": time.time(), "kind": kind, "role": self.role, "model": llm.name, "label_key": label_key,
               "label": res.get(label_key), "result": res, "prompt": prompt, "meta": meta or {}}
        with open(self.log_path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        return res


def prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = 2 * p * r / (p + r) if p and r else None
    f2 = 5 * p * r / (4 * p + r) if p and r else None
    return {"precision": _r(p), "recall": _r(r), "f1": _r(f1), "f2": _r(f2), "tp": tp, "fp": fp, "fn": fn}


def cohen_kappa(a: list, b: list) -> float | None:
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not pairs:
        return None
    labels = sorted({str(x) for p in pairs for x in p})
    n = len(pairs)
    po = sum(1 for x, y in pairs if str(x) == str(y)) / n
    pe = sum((sum(1 for x, _ in pairs if str(x) == l) / n) * (sum(1 for _, y in pairs if str(y) == l) / n) for l in labels)
    return _r((po - pe) / (1 - pe)) if pe < 1 else 1.0


def wilson(k: int, n: int, z: float = 1.96) -> tuple | None:
    """95% Wilson interval for a proportion (small-n friendly)."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (_r((c - h) / d), _r((c + h) / d))


def save(out_dir: Path, name: str, obj):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.json").write_text(json.dumps(obj, indent=1, default=str))


def short(obj, n: int = 1500) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"
