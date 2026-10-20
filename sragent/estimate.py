"""Cost estimates for the work that is still to do, per model.

Calls per stage are counted from the current review state (or projected from typical ratios before
the stage's inputs exist). Tokens per call come from your own past runs when available
(usage.jsonl in any run directory, matched by stage and model), otherwise from the defaults below,
which were measured in the v1/v2 runs. Reasoning effort mostly changes output (thinking) tokens.
"""
from __future__ import annotations

import json
import math
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
    "synthesis_repair": (10000, 500, 2500),
    "feedback": (12000, 1200, 4000),
}
# estimate rows -> pipeline stage whose model does that work (stage_models)
ROW_STAGE = {"plan": "plan", "search": "search", "screen_title_abstract": "screen", "screen_full_text": "fulltext",
             "extract": "extract", "rob": "rob", "synthesis": "synthesize", "synthesis_verify": "synthesize",
             "synthesis_repair": "synthesize", "feedback": "feedback"}
FORWARD_RATE = 0.56     # share of records forwarded past title/abstract (v2)
INCLUDE_RATE = 0.09     # share of records finally included (v2)
RETRIEVED_RATE = 0.6    # share of forwarded records whose full text can be retrieved (benchmark runs: 25/42, 36/55)
SNOWBALL_PER_SEED = 6   # candidates citation chasing adds per included study (64/13 and 115/15 in the benchmark runs)
N_STATEMENTS = 8        # statements per synthesis (the prompt asks for 5 to 10)
RECHECK_RATE = 0.15     # share of extractions that raise an eligibility doubt and re-screen the record (3/19 in the v2 run)
REPAIR_RATE = 0.6       # share of statements the self-check faults and the repair round rewrites (8/10 in the v2 run)


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
    unscreened = max(0, n_records - screened)
    if "screen" in order:
        # records the screening rules will exclude cost nothing: count them out when the records are here
        from . import rules
        p = (st.get("protocol") or {}).get("data", {})
        ruled = sum(1 for n in st.by_type("record") if not st.get("screen:" + n["id"].split(":", 1)[1])
                    and rules.classify(n["data"], rules.key_terms(p), rules.enabled_rules(ctx)))
        calls["screen_title_abstract"] = max(0, unscreened - ruled)
    forwarded_known = [n for n in st.by_type("screen") if n["data"]["stage"] == "title_abstract"
                       and n["data"]["decision"] in ("include", "uncertain", "forward")]
    waiting = sum(1 for n in st.by_type("screen") if n["data"]["stage"] == "retrieval")   # no full text yet: no call
    if "fulltext" in order:
        calls["screen_full_text"] = int((len(forwarded_known) + calls["screen_title_abstract"] * FORWARD_RATE) * RETRIEVED_RATE)
    inc = st.included_ids()
    pending_ft = sum(1 for n in forwarded_known if not st.get(f"ext:{n['id'].split(':', 1)[1]}"))
    n_inc_future = int(calls["screen_title_abstract"] * INCLUDE_RATE) + math.ceil(pending_ft * INCLUDE_RATE / FORWARD_RATE)
    if "snowball" in order and ctx.cfg["pipeline"].get("citation_chasing", True) and not st.meta.get("snowball"):
        cand = int((len(inc) + n_inc_future) * SNOWBALL_PER_SEED)     # found papers go through both screening steps
        calls["screen_title_abstract"] += cand
        calls["screen_full_text"] += int(cand * FORWARD_RATE * RETRIEVED_RATE)
        n_inc_future += int(cand * INCLUDE_RATE)
    if "extract" in order:
        budget = int(ctx.cfg["pipeline"].get("context_chars", 60000))
        n_new = 0
        for r in inc:
            if st.get(f"ext:{r}"):
                continue
            parts = -(-len(st.text_of(r)[0]) // budget) if st.get(f"ft:{r}") else 1
            calls["extract"] += parts + (1 if parts > 1 else 0)                     # one call per part, plus the merge
            n_new += 1
        # studies not yet known: as many parts as the full texts already retrieved need on average
        lens = [n["data"].get("chars") or 0 for n in st.by_type("fulltext") if n["data"].get("kind") == "fulltext"]
        avg_parts = max(1, -(-int(statistics.mean(lens)) // budget)) if lens else 1
        calls["extract"] += n_inc_future * (avg_parts + (1 if avg_parts > 1 else 0))
        if "fulltext" in order or n_new:   # eligibility doubts raised at extraction re-screen the record on its full text
            calls["screen_full_text"] += math.ceil((n_new + n_inc_future) * RECHECK_RATE)
    if "rob" in order:
        calls["rob"] = sum(1 for r in inc if not st.get(f"rob:{r}")) + n_inc_future
    if "synthesize" in order:
        if not st.by_type("synthesis", active_only=True):
            calls["synthesis"] = 1
            calls["synthesis_verify"] = N_STATEMENTS
            if ctx.cfg["pipeline"].get("verify_synthesis", True) and ctx.cfg["pipeline"].get("repair_synthesis", True):
                n_rep = math.ceil(N_STATEMENTS * REPAIR_RATE)         # the repair round: one rewrite and one re-check each
                calls["synthesis_repair"] = n_rep
                calls["synthesis_verify"] += n_rep
        elif set(st.meta.get("synthesis_considered") or []) != set(inc) or n_inc_future:
            n_new = len(set(inc) - set(st.meta.get("synthesis_considered") or [])) + n_inc_future
            calls["feedback"] = n_new                 # one integrate call per new study
            calls["synthesis_verify"] = min(N_STATEMENTS, max(1, n_new))
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
        tout = out_tokens(stage, effort)
        src = "your past runs" if h else ("past runs (other model)" if h_any else "defaults")
        usd = n * (tin * pricing.get("input_per_m", 0) + tout * pricing.get("output_per_m", 0)) / 1e6
        total += usd
        rows.append({"stage": stage, "calls": n, "in_per_call": int(tin), "out_per_call": int(tout),
                     "usd": round(usd, 3), "source": src})
    return {"model": model, "rows": rows, "total_usd": round(total, 2),
            "range_usd": [round(total * 0.6, 2), round(total * 1.6, 2)]}


def out_tokens(stage: str, effort: str | None) -> float:
    """Output tokens per call (thinking included) for a reasoning effort."""
    _, low, dflt = DEFAULTS[stage]
    return {"low": low, "medium": (low + dflt) / 2, "high": dflt, "xhigh": dflt * 1.5, "max": dflt * 2}.get(effort or "", dflt)


def estimate_stages(calls: dict, cfg: dict, hist: dict | None = None) -> dict:
    """Like estimate(), with each stage's own model, price and effort (stage_models)."""
    from .config import role_cfg, stage_cfg
    rows, total = [], 0.0
    for stage, n in calls.items():
        if not n:
            continue
        if stage == "synthesis_verify":   # the check is made by the verifier role (default: the agent role)
            c = role_cfg(cfg, "verifier") if (cfg.get("roles") or {}).get("verifier") else role_cfg(cfg, "agent")
        else:
            c = stage_cfg(cfg, ROW_STAGE.get(stage, "feedback"))
        e = estimate({stage: n}, c.get("model"), c.get("pricing") or {}, c.get("reasoning_effort"), hist)
        for r in e["rows"]:
            r["model"], r["effort"] = c.get("model"), c.get("reasoning_effort") or "default"
            rows.append(r)
            total += r["usd"]
    models = sorted({r["model"] for r in rows})
    return {"model": models[0] if len(models) == 1 else "per stage", "rows": rows, "total_usd": round(total, 2),
            "range_usd": [round(total * 0.6, 2), round(total * 1.6, 2)]}
