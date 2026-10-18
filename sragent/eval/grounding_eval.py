"""H2 Evidence grounding and H6 term validation.

H2a  Extracted values come with resolvable source locations (sentence ids; deterministic lookup).
H2b  An independent LLM judge rates the value as supported by the cited sentences; citing
     several locations (multi-location evidence) raises this rate compared with one short quote.
H2c  Grounding is lower when only the abstract is available than with full text.
H6   External databases (RxNorm, PubChem, ChEMBL, MONDO) validate correctly extracted entity
     names and flag corrupted ones (misspelt / fabricated / wrong-class), with no LLM involved.
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict

from .. import terms
from ..context import Ctx
from ..evidence import grounding_summary
from .common import Judge, short

FIELD_JUDGE = """For each extracted item, decide whether the VALUE is fully supported by its EVIDENCE
(the source sentences the extractor cited, with their location in the paper).

{items}

Return JSON: {{"items": [{{"name": str, "label": "supported"|"partially_supported"|"unsupported"|"no_evidence",
              "issue": str}}]}}
"partially_supported": the evidence supports the core value, but a number, unit, qualifier or
population in the value is missing from / different from the evidence."""


def _perturb(term: str, rnd: random.Random) -> list[tuple[str, str]]:
    t = term.strip()
    out = []
    if len(t) > 5:
        i = rnd.randrange(1, len(t) - 2)
        out.append(("misspelling", t[:i] + t[i + 1] + t[i] + t[i + 2:]))
        out.append(("fabricated_suffix", t[: max(4, len(t) - 3)] + "orazine"))
    return out


def run(ctx: Ctx, judge: Judge, max_studies: int = 15) -> dict:
    st = ctx.store
    inc = st.included_ids()
    out = {}
    by_src = defaultdict(list)
    for kind, ntype in (("extraction", "extraction"), ("rob", "rob"), ("screening", "screen")):
        ev = []
        for n in st.by_type(ntype):
            if ntype != "screen" and n["id"].split(":", 1)[1] not in inc:
                continue
            for e in n["evidence"]:
                ev.append(e)
                if ntype == "extraction":
                    by_src[n["data"].get("source_kind", "?")].append(e)
        out[f"evidence_{kind}"] = grounding_summary(ev)
    out["evidence_extraction_by_source"] = {k: grounding_summary(v) for k, v in by_src.items()}

    # NR rate per field
    nr = Counter()
    tot = 0
    for rid in inc:
        n = st.get(f"ext:{rid}")
        if not n:
            continue
        tot += 1
        for k, v in n["data"]["fields"].items():
            if v["value"].strip().upper().startswith("NR"):
                nr[k] += 1
    out["not_reported_rate_by_field"] = {k: round(nr[k] / tot, 2) for k in nr} if tot else {}

    # H2b judge support on value-quote pairs (one call per study)
    labels = Counter()
    by_kind_lab = defaultdict(Counter)
    issues = []
    field_labels: dict[str, dict] = {}
    for rid in (inc if not max_studies else inc[:max_studies]):
        n = st.get(f"ext:{rid}")
        if not n:
            continue
        items = [f"- name: {k}\n  description: {next(f['description'] for f in ctx.topic['extraction_fields'] if f['name'] == k)}"
                 f"\n  VALUE: {v['value']}\n  EVIDENCE: {v.get('quote', '') or '(none cited)'}"
                 for k, v in n["data"]["fields"].items() if not v["value"].strip().upper().startswith("NR")]
        if not items:
            continue
        res = judge("field_support", FIELD_JUDGE.format(items="\n".join(items)), "items", {"rid": rid})
        for it in res.get("items", []):
            field_labels.setdefault(rid, {})[it.get("name")] = it.get("label")
            labels[it.get("label")] += 1
            by_kind_lab[n["data"].get("source_kind")][it.get("label")] += 1
            if it.get("label") in ("partially_supported", "unsupported"):
                issues.append({"rid": rid, "field": it.get("name"), "label": it.get("label"), "issue": short(it.get("issue", ""), 300)})
    total = sum(labels.values())
    out["judge_value_support"] = {"counts": dict(labels), "n": total,
                                  "supported_rate": round(labels["supported"] / total, 3) if total else None,
                                  "by_source_kind": {k: dict(v) for k, v in by_kind_lab.items()},
                                  "issue_examples": issues[:12], "studies_judged": len(field_labels)}
    out["_field_labels"] = field_labels   # used to build the verified-extraction input for the synthesis test

    # H6 term validation: real extracted terms + deterministic corruptions
    real = Counter()
    real_terms = []
    for rid in inc:
        n = st.get(f"ext:{rid}")
        if not n:
            continue
        for fname, v in n["data"]["fields"].items():
            for t in v.get("terms", []):
                real[t["status"]] += 1
                real_terms.append((t["term"], next((f.get("kind") for f in ctx.topic["extraction_fields"] if f["name"] == fname), None), t["status"]))
    out["term_validation_extracted"] = dict(real)
    rnd = random.Random(7)
    validated = sorted({(t, k) for t, k, s in real_terms if s == "validated" and k})
    probes = []
    for t, k in validated[:8]:
        for kind_p, bad in _perturb(t, rnd):
            v = terms.validate(bad, k)
            probes.append({"original": t, "kind": k, "corruption": kind_p, "corrupted": bad, "status": v["status"],
                           "flagged": v["status"] != "validated", "suggested": v.get("preferred")})
    # wrong-class probes: disease name in a drug field and vice versa
    for t, k in validated[:4]:
        other = "disease" if k == "drug" else "drug"
        v = terms.validate(t, other)
        probes.append({"original": t, "kind": other, "corruption": "wrong_class", "corrupted": t, "status": v["status"],
                       "flagged": v["status"] != "validated", "suggested": v.get("preferred")})
    out["term_corruption_probes"] = {"n": len(probes), "flagged": sum(p["flagged"] for p in probes),
                                     "detection_rate": round(sum(p["flagged"] for p in probes) / len(probes), 3) if probes else None,
                                     "approximate_with_correct_suggestion": sum(
                                         1 for p in probes if p["status"] == "approximate" and p["suggested"] and
                                         p["suggested"].lower() == p["original"].lower()),
                                     "probes": probes}
    return out
