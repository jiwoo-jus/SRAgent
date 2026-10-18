"""Cost estimates for the work that is still to do, per model.

Calls per stage are counted from the current review state (or projected from typical ratios before
the stage's inputs exist). Tokens per call come from your own past runs when available
(usage.jsonl in any run directory, matched by stage and model), otherwise from the defaults below,
which were measured in the v1/v2 runs. Reasoning effort mostly changes output (thinking) tokens.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

# stage -> (input tokens, output tokens at low effort, output tokens at default effort)
DEFAULTS = {
    "plan": (500, 900, 1700),
    "search": (550, 450, 2200),
    "screen_title_abstract": (1050, 280, 1700),
    "screen_full_text": (2500, 300, 1200),
    "extract": (6000, 750, 1500),
    "rob": (6000, 600, 2000),
    "synthesis": (10000, 1800, 6000),
    "synthesis_verify": (10000, 60, 1500),
    "feedback": (12000, 1200, 4000),
}
FORWARD_RATE = 0.56     # share of records forwarded past title/abstract (v2)
INCLUDE_RATE = 0.09     # share of records finally included (v2)
N_STATEMENTS = 7


def history(root: Path = Path("runs")) -> dict:
    """Mean tokens per (stage, model) over all past uncached calls."""
    acc = defaultdict(lambda: {"in": [], "out": []})
    for f in root.glob("*/usage.jsonl"):
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("cached"):
                continue
            tag = r.get("tag", "")
            stage = tag if tag in DEFAULTS else next(
                (s for s in sorted(DEFAULTS, key=len, reverse=True) if tag.startswith(s + "_")), None)
            if stage:
                acc[(stage, r["model"])]["in"].append(r["prompt_tokens"])
                acc[(stage, r["model"])]["out"].append(r["completion_tokens"])
                acc[(stage, "*")]["in"].append(r["prompt_tokens"])
    return {k: {"in": statistics.mean(v["in"]), "out": statistics.mean(v["out"]) if v["out"] else None, "n": len(v["in"])}
            for k, v in acc.items()}


def remaining_calls(ctx, max_records: int | None, until: str = "report") -> dict:
    """Number of LLM calls per stage still needed to finish the pipeline up to `until`."""
    from .pipeline import STAGES
    st = ctx.store
    order = STAGES[: STAGES.index(until) + 1]
    calls = defaultdict(int)
    if "plan" in order and not st.get("protocol"):
        calls["plan"] = 1
    n_records = len(st.by_type("record"))
    if not st.get("search"):
        if "search" in order:
            calls["search"] = 1
        src = (ctx.topic.get("records") or {})
        guess = src.get("pubmed_retmax", 300)
        if src.get("source") == "synergy":
            p = Path(src.get("data_dir", "data")) / f"{src['synergy_key'].lower()}_records.jsonl"
            if p.exists():
                guess = sum(1 for _ in open(p))
        n_records = min(guess, max_records) if max_records else guess
    screened = len(st.by_type("screen"))
    if "screen" in order:
        calls["screen_title_abstract"] = max(0, n_records - screened)
    forwarded_known = [n for n in st.by_type("screen") if n["data"]["stage"] == "title_abstract"
                       and n["data"]["decision"] in ("include", "uncertain", "forward")]
    if "fulltext" in order:
        calls["screen_full_text"] = len(forwarded_known) + int(calls["screen_title_abstract"] * FORWARD_RATE)
    inc = st.included_ids()
    n_inc_future = int((calls["screen_title_abstract"]) * INCLUDE_RATE)
    if "extract" in order:
        calls["extract"] = sum(1 for r in inc if not st.get(f"ext:{r}")) + n_inc_future
    if "rob" in order:
        calls["rob"] = sum(1 for r in inc if not st.get(f"rob:{r}")) + n_inc_future
    if "synthesize" in order and not st.by_type("synthesis"):
        calls["synthesis"] = 1
        calls["synthesis_verify"] = N_STATEMENTS
    return dict(calls)


def estimate(calls: dict, model: str, pricing: dict, effort: str | None, hist: dict | None = None) -> dict:
    hist = hist or {}
    rows, total = [], 0.0
    for stage, n in calls.items():
        if not n:
            continue
        d_in, d_low, d_def = DEFAULTS[stage]
        h = hist.get((stage, model)) or {}
        h_any = hist.get((stage, "*")) or {}
        tin = h.get("in") or h_any.get("in") or d_in
        tout = d_low if effort == "low" else d_def
        src = "your past runs" if h else ("past runs (other model)" if h_any else "defaults")
        usd = n * (tin * pricing.get("input_per_m", 0) + tout * pricing.get("output_per_m", 0)) / 1e6
        total += usd
        rows.append({"stage": stage, "calls": n, "in_per_call": int(tin), "out_per_call": int(tout),
                     "usd": round(usd, 3), "source": src})
    return {"model": model, "rows": rows, "total_usd": round(total, 2),
            "range_usd": [round(total * 0.6, 2), round(total * 1.6, 2)]}
