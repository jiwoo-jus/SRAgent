"""Stage-wise synthesis test: the agent writes the synthesis from a verified extraction table
(see stagewise.verified_extraction_review); the judge rates every statement against the data it cites.

Measures: statements supported / partially / unsupported, overgeneralization rate, citation validity
(every cited study is in the supplied set), agreement between the agent's own self-check and the judge.
"""
from __future__ import annotations

from collections import Counter

from .. import prompts
from ..context import Ctx
from ..stages import synthesis
from .common import Judge, short, wilson


def run(vctx: Ctx, judge: Judge, input_info: dict) -> dict:
    st = vctx.store
    inc = set(st.included_ids())
    rows = []
    for n in st.by_type("synthesis", active_only=True):
        d = n["data"]
        ev = synthesis.statement_evidence(vctx, d["cites"], d["text"])
        j = judge("synthesis_support", prompts.SYNTH_VERIFY.format(sid=d["id"], text=d["text"], evidence=ev),
                  "support", {"statement": d["id"]})
        rows.append({"id": d["id"], "text": short(d["text"], 400), "cites": d["cites"],
                     "cites_valid": all(c in inc for c in d["cites"]) and bool(d["cites"]),
                     "judge_support": j.get("support"), "judge_overgeneralization": bool(j.get("overgeneralization")),
                     "judge_issues": [short(x, 200) for x in (j.get("issues") or [])][:3],
                     "self_check_support": (d.get("verification") or {}).get("support"),
                     "self_check_overgeneralization": (d.get("verification") or {}).get("overgeneralization")})
    n = len(rows)
    c = Counter(r["judge_support"] for r in rows)
    og = sum(r["judge_overgeneralization"] for r in rows)
    agree = [r for r in rows if r["self_check_support"]]
    return {"input": input_info, "n_statements": n, "judge_support": dict(c),
            "supported_rate": round(c["supported"] / n, 3) if n else None,
            "supported_rate_95ci": wilson(c["supported"], n),
            "overgeneralization_rate": round(og / n, 3) if n else None,
            "citation_validity_rate": round(sum(r["cites_valid"] for r in rows) / n, 3) if n else None,
            "self_check_vs_judge_agreement": round(sum(r["self_check_support"] == r["judge_support"] for r in agree) / len(agree), 3)
            if agree else None,
            "statements": rows}
