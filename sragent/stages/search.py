"""Stage 2: literature search (PubMed), strategy recording, de-duplication.

Two record sources:
  * pubmed   : the agent writes a Boolean query (with DB-validated synonyms) and runs it
  * synergy  : benchmark mode; records + gold labels come from the SYNERGY dataset.
               The agent's own PubMed query is still generated and executed so that
               search recall against the gold included set can be measured.
  * file     : a JSONL of records you exported elsewhere (fields: rid,title,abstract,...)
"""
from __future__ import annotations

import json
import re

from .. import prompts
from ..context import Ctx
from ..sources import pubmed, synergy
from .planning import protocol_brief

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover
    fuzz = None


def _ntitle(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def dedup(records: list[dict]) -> tuple[list[dict], list[dict]]:
    keep, dups = [], []
    seen_pmid, seen_doi, titles = {}, {}, []
    for r in records:
        reason = None
        if r.get("pmid") and r["pmid"] in seen_pmid:
            reason = f"same PMID as {seen_pmid[r['pmid']]}"
        elif r.get("doi") and r["doi"] in seen_doi:
            reason = f"same DOI as {seen_doi[r['doi']]}"
        else:
            nt = _ntitle(r.get("title", ""))
            if len(nt) > 20:
                for rid, t in titles:
                    if nt == t or (fuzz and fuzz.ratio(nt, t) >= 95):
                        reason = f"near-identical title to {rid}"
                        break
        if reason:
            dups.append({**r, "duplicate_reason": reason})
            continue
        keep.append(r)
        if r.get("pmid"):
            seen_pmid[r["pmid"]] = r["rid"]
        if r.get("doi"):
            seen_doi[r["doi"]] = r["rid"]
        titles.append((r["rid"], _ntitle(r.get("title", ""))))
    return keep, dups


def build_query(ctx: Ctx, protocol: dict) -> dict:
    syn_lines = []
    for kc in protocol.get("key_concepts", []):
        v = kc.get("validation") or {}
        syn_lines.append(f"- {kc['term']} ({kc.get('kind')}): status={v.get('status', 'n/a')}; "
                         f"synonyms={', '.join((v.get('synonyms') or [])[:12]) or 'none'}")
    q = ctx.agent.chat(ctx.msgs(prompts.SEARCH_QUERY.format(protocol=protocol_brief(protocol),
                                                            synonyms="\n".join(syn_lines))), tag="search")
    return q


# ---------------------------------------------------------------- the search strategy chosen with the review team
# The agent's query is one proposal. In a real review the team can see alternatives with their PubMed hit counts, ask
# for changes in plain words, count any query, and confirm the one to run (store.meta["search_strategy"]).

def count(ctx: Ctx, query: str) -> dict:
    """PubMed hit count of a query (free; one esearch with retmax 0), with the date limit of the review."""
    src = ctx.topic.get("records", {})
    try:
        res = pubmed.esearch(query, retmax=0, maxdate=src.get("maxdate"))
        return {"count": res["count"], "translation": res.get("translation", "")}
    except Exception as e:  # network, bad query: shown, not raised
        return {"count": None, "error": str(e)[:200]}


def strategy_meta(st) -> dict:
    return st.meta.setdefault("search_strategy", {"variants": [], "chat": [], "confirmed": None})


def _syn_lines(protocol: dict) -> str:
    out = []
    for kc in protocol.get("key_concepts", []):
        v = kc.get("validation") or {}
        out.append(f"- {kc['term']} ({kc.get('kind')}): status={v.get('status', 'n/a')}; "
                   f"synonyms={', '.join((v.get('synonyms') or [])[:12]) or 'none'}")
    return "\n".join(out)


def suggest_queries(ctx: Ctx, protocol: dict) -> dict:
    """The agent's own query plus five alternatives, each with its hit count and the reason it differs."""
    import time
    st = ctx.store
    m = strategy_meta(st)
    base = next((v for v in m["variants"] if v.get("id") == "V0"), None)
    if base is None:
        q = build_query(ctx, protocol)
        base = {"id": "V0", "name": "The agent's query", "query": q.get("query", ""),
                "rationale": q.get("rationale", ""), "expect": "sensitive", "concept_blocks": q.get("concept_blocks", [])}
        base.update(count(ctx, base["query"]))
    out = ctx.agent.chat(ctx.msgs(prompts.SEARCH_VARIANTS.format(
        protocol=protocol_brief(protocol), synonyms=_syn_lines(protocol), current=base["query"],
        current_count=base.get("count", "?"))), tag="search_variants")
    variants = [base]
    for i, v in enumerate((out.get("variants") or [])[:5], 1):
        if not isinstance(v, dict) or not v.get("query"):
            continue
        var = {"id": f"V{i}", "name": str(v.get("name") or f"Variant {i}")[:80], "query": str(v["query"]),
               "rationale": str(v.get("rationale") or ""), "expect": v.get("expect") or ""}
        var.update(count(ctx, var["query"]))
        variants.append(var)
    m["variants"] = variants
    m["suggested_at"] = time.strftime("%Y-%m-%d %H:%M")
    ctx.log(f"[search] {len(variants) - 1} alternative queries proposed: " +
            ", ".join(f"{v['name']} ({v.get('count')})" for v in variants[1:]))
    return m


def refine_query(ctx: Ctx, protocol: dict, query: str, instruction: str) -> dict:
    """One change asked in plain words ("exclude reviews", "add a block for ..."): a revised query, its count and why."""
    st = ctx.store
    m = strategy_meta(st)
    cur = count(ctx, query)
    out = ctx.agent.chat(ctx.msgs(prompts.SEARCH_REFINE.format(
        protocol=protocol_brief(protocol), current=query, current_count=cur.get("count", "?"), instruction=instruction)),
        tag="search_refine")
    new_q = str(out.get("query") or query)
    res = {"query": new_q, "explanation": str(out.get("explanation") or ""), **count(ctx, new_q)}
    m["chat"].append({"role": "user", "text": instruction})
    m["chat"].append({"role": "agent", "text": res["explanation"], "query": new_q, "count": res.get("count")})
    m["draft"] = {"query": new_q, "count": res.get("count")}
    return res


def confirm_query(ctx: Ctx, query: str, note: str = "") -> dict:
    """The query the search stage will run (instead of the agent's), recorded with its count and who chose it."""
    import time
    st = ctx.store
    m = strategy_meta(st)
    c = count(ctx, query)
    var = next((v for v in m["variants"] if v.get("query") == query), None)
    m["confirmed"] = {"query": query, "count": c.get("count"), "translation": c.get("translation", ""),
                      "variant": (var or {}).get("name"), "rationale": (var or {}).get("rationale", ""),
                      "concept_blocks": (var or {}).get("concept_blocks", []), "note": note,
                      "by": "user", "ts": time.strftime("%Y-%m-%d %H:%M")}
    ctx.log(f"[search] query confirmed by the review team ({c.get('count')} hits)")
    return m["confirmed"]


def search(ctx: Ctx, protocol: dict) -> list[dict]:
    src = ctx.topic.get("records", {})
    kind = src.get("source", "pubmed")
    confirmed = (ctx.store.meta.get("search_strategy") or {}).get("confirmed")
    if confirmed and confirmed.get("query"):
        strategy = {"query": confirmed["query"], "concept_blocks": confirmed.get("concept_blocks") or [],
                    "rationale": confirmed.get("rationale") or "", "query_source": "confirmed by the review team",
                    "confirmed": {k: confirmed.get(k) for k in ("variant", "note", "by", "ts")}}
    else:
        strategy = build_query(ctx, protocol)
        strategy["query_source"] = "the agent"
    if kind == "file":
        strategy.update({"database": "Imported records", "retrieved_pmids": []})
    else:
        res = pubmed.esearch(strategy["query"], retmax=int(src.get("pubmed_retmax", 300)),
                             maxdate=src.get("maxdate"))
        strategy.update({"database": "PubMed", "hits": res["count"], "translation": res["translation"],
                         "retrieved_pmids": res["pmids"]})
        ctx.log(f"[search] PubMed query -> {res['count']} hits (retrieved {len(res['pmids'])})")

    if kind == "synergy":
        from pathlib import Path
        recs = synergy.load(src["synergy_key"], Path(src.get("data_dir", "data")) /
                            f"{src['synergy_key'].lower()}_records.jsonl", log=ctx.log)
        gold_pmids = {r["pmid"] for r in recs if r["label_included"] and r.get("pmid")}
        found = gold_pmids & set(res["pmids"])
        bench = {"gold_included_with_pmid": len(gold_pmids), "retrieved": len(found),
                 "recall": round(len(found) / len(gold_pmids), 3) if gold_pmids else None}
        ctx.log(f"[search] benchmark recall of agent query vs gold includes: {len(found)}/{len(gold_pmids)}")
    elif kind == "file":
        recs = [json.loads(l) for l in open(src["file"]) if l.strip()]
        for r in recs:
            r.setdefault("rid", synergy.rid_for(r))
    else:
        recs = pubmed.efetch(res["pmids"])
        for r in recs:
            r["rid"] = synergy.rid_for(r)

    recs, dups = dedup(recs)
    strategy["n_identified"] = len(recs) + len(dups)
    strategy["n_duplicates"] = len(dups)

    # optional cost cap: stratified sample keeps all gold includes in benchmark mode
    cap = ctx.cfg["pipeline"].get("max_records")
    if cap and len(recs) > cap:
        import random
        rnd = random.Random(ctx.cfg["pipeline"].get("screen_sample_seed", 13))
        if kind == "synergy":
            inc = [r for r in recs if r.get("label_included")]
            exc = [r for r in recs if not r.get("label_included")]
            recs = inc + rnd.sample(exc, max(0, cap - len(inc)))
        else:
            recs = rnd.sample(recs, cap)
        strategy["sampled_to"] = len(recs)
        ctx.log(f"[search] sampled {len(recs)} records for screening (max_records={cap})")

    st = ctx.store
    if any("label_included" in r for r in recs):   # gold labels go to the evaluator's private file only
        from .. import gold
        gold.strip_records(ctx.run_dir, recs, source=f"{kind}:{src.get('synergy_key') or src.get('file', '')}")
        if kind == "synergy":
            g = gold.load(ctx.run_dir)
            g["benchmark_search_recall"] = bench
            gold.save(ctx.run_dir, g)
    st.upsert("search", "search", strategy, depends_on=["protocol", "cfg:records"], reason="search executed")
    for r in recs:
        rid = r["rid"]
        if st.get(f"rec:{rid}") is None:
            st.upsert(f"rec:{rid}", "record", r, depends_on=["search"], reason="retrieved")
    ctx.log(f"[search] {len(recs)} unique records after removing {len(dups)} duplicates")
    return recs
