"""Stage-wise evaluation inputs: test later stages independently of earlier failures.

build_gold_input_review()
    A SEPARATE copy of the review whose included set is the benchmark's gold includes (supplied by the
    evaluator, marked `gold_input`). The agent then extracts data, drafts risk of bias and writes the
    synthesis for exactly these studies, with fresh model calls. The real screening records and the
    end-to-end scores are untouched (the main store is never written).
    The agent never sees a label: it only receives the studies. Gold inclusion says nothing about
    the correct extracted values or RoB judgments; those are scored against the source text.

verified_extraction_review()
    A further copy for the synthesis test: only extracted values that the judge rated "supported" by
    their cited source sentences are kept (others become "NR (not verified)"), and the agent writes the
    synthesis from that verified table. Marked `verified_extraction_input` (judge-verified, not human-verified).
"""
from __future__ import annotations

import copy
import dataclasses
from collections import Counter
from pathlib import Path

from .. import gold
from ..context import Ctx
from ..stages import extraction, fulltext, rob, synthesis

NOT_VERIFIED = "NR (not verified: the judge did not find this value supported by its cited source sentences)"


def _ctx_for(ectx: Ctx, store, d: Path) -> Ctx:
    d.mkdir(parents=True, exist_ok=True)
    store.run_dir = d
    return dataclasses.replace(ectx, store=store, run_dir=d)


def build_gold_input_review(ctx: Ctx, ectx: Ctx, out_dir: Path) -> tuple[Ctx, dict]:
    labels = gold.labels(ctx)
    st = ctx.store.clone()
    sctx = _ctx_for(ectx, st, out_dir)
    gold_inc = sorted(r for r, l in labels.items() if l == 1 and st.get(f"rec:{r}"))
    # start from a clean slate for the later stages
    for nid in [k for k in st.nodes if k.startswith(("ext:", "rob:", "syn:")) or k == "report"]:
        del st.nodes[nid]
    for n in st.by_type("screen"):
        rid = n["id"].split(":", 1)[1]
        d = copy.deepcopy(n["data"])
        d["gold_input"] = True
        if rid in gold_inc:
            d.update({"decision": "include", "stage": "full_text", "because": []})
        else:
            d.update({"decision": "exclude", "because": ["not in the supplied study set"]})
        n["data"] = d
    sources = {}
    for rid in gold_inc:
        ft = (st.get(f"ft:{rid}") or {}).get("data")
        if ft is None or ft.get("kind") != "fulltext":
            ft = fulltext.fetch_one(sctx, rid)       # user file from the main fulltext folder, else Europe PMC
        sources[rid] = ft.get("kind", "abstract")
    sctx.log(f"[stagewise] gold-input review: {len(gold_inc)} studies; sources {dict(Counter(sources.values()))}")
    extraction.extract_all(sctx, only=gold_inc)
    rob.rob_all(sctx, only=[r for r in gold_inc if st.get(f"ext:{r}")])
    synthesis.synthesize(sctx)
    st.meta["gold_input"] = {
        "supplied": True,
        "what": "included set = benchmark gold includes (screening bypassed); extraction, risk of bias and synthesis by the agent",
        "n_studies": len(gold_inc), "source_kinds": dict(Counter(sources.values())), "sources": sources,
        "agent_saw_labels": False}
    sctx.save()
    return sctx, st.meta["gold_input"]


def verified_extraction_review(sctx: Ctx, field_labels: dict, out_dir: Path) -> tuple[Ctx, dict]:
    st = sctx.store.clone()
    vctx = _ctx_for(sctx, st, out_dir)
    for nid in [k for k in st.nodes if k.startswith("syn:")]:
        del st.nodes[nid]
    kept = dropped = unjudged = 0
    for rid in st.included_ids():
        n = st.get(f"ext:{rid}")
        if not n:
            continue
        labs = field_labels.get(rid)
        for name, item in n["data"]["fields"].items():
            if item["value"].strip().upper().startswith("NR"):
                continue
            lab = (labs or {}).get(name)
            if lab == "supported":
                kept += 1
            else:
                unjudged += lab is None
                dropped += 1
                item["value"] = NOT_VERIFIED
    synthesis.synthesize(vctx)
    info = {"supplied": True, "what": "synthesis input = extracted values the judge rated 'supported' (judge-verified, "
                                      "not human-verified); other values replaced by NR",
            "values_kept": kept, "values_dropped": dropped, "values_not_judged": unjudged}
    st.meta["verified_extraction_input"] = info
    vctx.save()
    return vctx, info
