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


def search(ctx: Ctx, protocol: dict) -> list[dict]:
    src = ctx.topic.get("records", {})
    kind = src.get("source", "pubmed")
    strategy = build_query(ctx, protocol)
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
        strategy["benchmark_search_recall"] = {"gold_included_with_pmid": len(gold_pmids),
                                               "retrieved": len(found),
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
    st.upsert("search", "search", strategy, depends_on=["protocol"], reason="search executed")
    for r in recs:
        rid = r["rid"]
        if st.get(f"rec:{rid}") is None:
            st.upsert(f"rec:{rid}", "record", r, depends_on=["search"], reason="retrieved")
    ctx.log(f"[search] {len(recs)} unique records after removing {len(dups)} duplicates")
    return recs
