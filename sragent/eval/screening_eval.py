"""Search and screening against the benchmark gold standard (no LLM calls; reads stored decisions).

Two separate views, never mixed:

END-TO-END (`end_to_end`)  the agent's own search results are the universe. A gold study missed by the
    search counts as missed, whatever screening would have done with it. Final inclusion requires the
    full-text review, so retrieval failures also count as misses. Status categories are kept apart:
    not found by search · excluded at title/abstract · excluded by rule · full text not retrieved ·
    full text unreadable · undecided after full text · excluded at full text · included.

STAGE-WISE SCREENING (`stagewise_screening`)  the benchmark's candidate pool is SUPPLIED as input
    instead of the agent's search (marked as such). Measures title/abstract forwarding and the
    full-text review independent of search recall, and the full-text decision conditional on the full
    text being available (so retrieval failures and judgment errors are separated).

Gold labels are read from the evaluator's private file (sragent/gold.py); the agent never sees them.
"""
from __future__ import annotations

from collections import Counter

from .. import gold
from ..context import Ctx
from ..stages.screening import RETRIEVAL_FAILURES
from .common import prf, wilson


def category(st, rid: str) -> str:
    n = st.get(f"screen:{rid}")
    if not n:
        return "not_screened"
    d = n["data"]
    dec = d["decision"]
    if dec == "exclude":
        if d.get("rule"):
            return "excluded_by_rule"
        return "excluded_full_text" if d["stage"] == "full_text" else "excluded_title_abstract"
    if dec == "include":
        return "included" if d["stage"] == "full_text" else "forwarded_pending_full_text"
    if dec in RETRIEVAL_FAILURES:
        return {"fulltext_unavailable": "full_text_not_retrieved", "parse_failed": "full_text_unreadable"}[dec]
    if dec == "awaiting":
        return "undecided_after_full_text"
    return "forwarded_pending_full_text"


def ta_forwarded(st, rid: str) -> bool | None:
    n = st.get(f"screen:{rid}")
    if not n:
        return None
    d = n["data"]
    if d["stage"] == "title_abstract":
        if d.get("rule"):   # excluded by a publication-type rule; forwarded only if the LLM had forwarded it
            return d.get("decision_before_rule") not in (None, "exclude")
        return d["decision"] != "exclude"
    ta = d.get("title_abstract") or {}
    return ta.get("decision", "forward") != "exclude"


def _conf(rows, pred, total):
    tp = sum(1 for r in rows if r["gold"] and pred(r))
    fp = sum(1 for r in rows if not r["gold"] and pred(r))
    fn = sum(1 for r in rows if r["gold"] and not pred(r))
    res = prf(tp, fp, fn)
    res["recall_95ci"] = wilson(tp, tp + fn)
    res["work_saved"] = round(sum(1 for r in rows if not pred(r)) / total, 3) if total else None
    return res


def end_to_end(ctx: Ctx) -> dict:
    st = ctx.store
    labels = gold.labels(ctx)
    s = (st.get("search") or {}).get("data", {})
    retrieved = set(s.get("retrieved_pmids") or [])
    recs = {n["id"].split(":", 1)[1]: n["data"] for n in st.by_type("record")}
    gold_inc = sorted(r for r, l in labels.items() if l == 1)
    fv = st.meta.get("found_via", {})
    found_by = lambda rid: ("search" if (recs.get(rid) or {}).get("pmid") in retrieved else
                            "citation chasing" if "citation" in fv.get(rid, []) else None)
    per = []
    for rid in gold_inc:
        pmid = (recs.get(rid) or {}).get("pmid", "")
        how = found_by(rid)
        per.append({"rid": rid, "pmid": pmid, "found_by_search": how == "search", "found_by": how,
                    "outcome": category(st, rid) if how else "not_found",
                    "status_if_screened": category(st, rid)})
    included_ret = [rid for rid in st.included_ids() if found_by(rid)]
    tp = sum(1 for p in per if p["outcome"] == "included")
    labelled_inc_ret = [r for r in included_ret if r in labels]
    unlabelled_inc = [r for r in included_ret if r not in labels]   # found by citation chasing etc.: no gold label
    pool_pmids = {d.get("pmid") for d in recs.values() if d.get("pmid")}
    ret_rids = [rid for rid in recs if found_by(rid)]
    return {
        "universe": "records found by the agent's own PubMed search or by its citation chasing",
        "search": {"query": s.get("query"), "hits": s.get("hits"), "retrieved": len(retrieved),
                   "gold_includes": len(gold_inc), "gold_found_by_search": sum(p["found_by_search"] for p in per),
                   "search_recall": round(sum(p["found_by_search"] for p in per) / len(gold_inc), 3) if gold_inc else None,
                   "retrieved_outside_benchmark_pool": len(retrieved - pool_pmids),
                   "gold_found_by_citation_chasing_only": sum(p["found_by"] == "citation chasing" for p in per),
                   "records_added_by_citation_chasing": len((st.meta.get("snowball") or {}).get("added", []))},
        "gold_outcomes": dict(Counter(p["outcome"] for p in per)),
        "gold_found_any_route": sum(1 for p in per if p["found_by"]),
        "end_to_end_recall": round(tp / len(gold_inc), 3) if gold_inc else None,
        "end_to_end_recall_95ci": wilson(tp, len(gold_inc)),
        "end_to_end_precision": round(sum(1 for r in labelled_inc_ret if labels[r]) / len(labelled_inc_ret), 3) if labelled_inc_ret else None,
        "included_from_search": len(included_ret),
        "included_unlabelled": len(unlabelled_inc), "included_unlabelled_rids": unlabelled_inc,
        "status_of_retrieved_records": dict(Counter(category(st, r) for r in ret_rids)),
        "per_gold_study": per,
        "note": ("Retrieved records outside the benchmark pool have no gold label and were not screened in benchmark "
                 "mode; they are counted but not scored. Screening decisions are per record, so the outcome for a "
                 "retrieved record is the same as if only the search results had been screened."),
    }


def stagewise_screening(ctx: Ctx) -> dict:
    st = ctx.store
    labels = gold.labels(ctx)
    rows = []
    for n in st.by_type("screen"):
        rid = n["id"].split(":", 1)[1]
        if rid not in labels:
            continue
        ft = (st.get(f"ft:{rid}") or {}).get("data", {})
        rows.append({"rid": rid, "gold": labels[rid], "category": category(st, rid), "forwarded": bool(ta_forwarded(st, rid)),
                     "full_text_available": ft.get("kind") == "fulltext",
                     "llm_decision": n["data"].get("llm_decision"), "final": n["data"]["decision"],
                     "has_abstract": bool((st.get(f"rec:{rid}") or {}).get("data", {}).get("abstract"))})
    if not rows:
        return {"note": "no benchmark labels for this run"}
    out = {"input_supplied": "benchmark candidate pool (gold-labelled records) instead of the agent's search results",
           "n_records": len(rows), "n_gold_included": sum(r["gold"] for r in rows)}
    N = len(rows)
    out["title_abstract_forwarding"] = _conf(rows, lambda r: r["forwarded"], N)
    out["final_included"] = _conf(rows, lambda r: r["category"] == "included", N)
    fwd = [r for r in rows if r["forwarded"]]
    with_ft = [r for r in fwd if r["full_text_available"]]
    out["full_text_review_given_full_text"] = {
        "n_forwarded": len(fwd), "n_with_full_text": len(with_ft),
        "decisions": _conf(with_ft, lambda r: r["category"] == "included", len(with_ft)) if with_ft else None,
        "undecided": dict(Counter(f"gold={r['gold']}" for r in with_ft if r["category"] == "undecided_after_full_text"))}
    out["status_by_gold"] = {f"gold={g}": dict(Counter(r["category"] for r in rows if r["gold"] == g)) for g in (1, 0)}
    out["gold_includes_not_included"] = [{k: r[k] for k in ("rid", "category", "full_text_available", "has_abstract")}
                                         for r in rows if r["gold"] and r["category"] != "included"]
    dis = [r for r in rows if r["llm_decision"] in ("include", "exclude", "uncertain")
           and (r["llm_decision"] == "exclude") != (r["final"] == "exclude")]
    out["holistic_vs_rule_disagreement"] = {"n": len(dis), "rate": round(len(dis) / N, 3)}
    return out
