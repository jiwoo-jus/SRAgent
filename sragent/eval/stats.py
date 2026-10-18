"""Paired comparisons that respect the unit of analysis.

The unit is the unique planted error (Claim 1) or the unique case (Claim 2). Repeated runs of the same
error are NOT independent observations: they are averaged within the unit first, and confidence
intervals come from a bootstrap over units (cluster bootstrap). Both conditions always see the same
units, so differences are paired.
"""
from __future__ import annotations

import math
import random


def _r(x, k=3):
    return None if x is None else round(x, k)


def sign_test(diffs: list[float]) -> dict:
    """Exact two-sided sign test on the non-zero paired differences."""
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    n = pos + neg
    if n == 0:
        return {"positive": 0, "negative": 0, "ties": len(diffs), "p_two_sided": None}
    k = min(pos, neg)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return {"positive": pos, "negative": neg, "ties": len(diffs) - n, "p_two_sided": _r(min(1.0, 2 * p), 4)}


def bootstrap_mean_ci(values: list[float], n_boot: int = 4000, seed: int = 11) -> tuple | None:
    if not values:
        return None
    rnd = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rnd.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return (_r(means[int(0.025 * n_boot)]), _r(means[int(0.975 * n_boot) - 1]))


def paired(unit_a: dict[str, float], unit_b: dict[str, float], label_a: str, label_b: str, seed: int = 11) -> dict:
    """unit_* : unit id -> score in [0, 1] (already averaged over repeats). Returns b - a."""
    units = sorted(set(unit_a) & set(unit_b))
    diffs = [unit_b[u] - unit_a[u] for u in units]
    return {"units": len(units), f"mean_{label_a}": _r(sum(unit_a[u] for u in units) / len(units)) if units else None,
            f"mean_{label_b}": _r(sum(unit_b[u] for u in units) / len(units)) if units else None,
            "mean_difference": _r(sum(diffs) / len(diffs)) if diffs else None,
            "difference_95ci_cluster_bootstrap": bootstrap_mean_ci(diffs, seed=seed),
            "sign_test": sign_test(diffs), "direction": f"{label_b} minus {label_a}"}


def mcnemar_exact(pairs: list[tuple[bool, bool]]) -> dict:
    """Observation-level McNemar (reported for reference only when repeats are not independent)."""
    b = sum(1 for x, y in pairs if not x and y)
    c = sum(1 for x, y in pairs if x and not y)
    n = b + c
    p = None
    if n:
        k = min(b, c)
        p = _r(min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n), 4)
    return {"only_second": b, "only_first": c, "p_two_sided": p}
