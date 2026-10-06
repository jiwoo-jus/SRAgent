"""Stage 1: review planning -> protocol (PICO, atomic eligibility criteria, key concepts).

A human checkpoint: the protocol is written to <run_dir>/protocol.yaml. Edit it and the next
`sragent run` uses your edited version (it is never silently overwritten).
"""
from __future__ import annotations

import json

import yaml

from .. import prompts, terms
from ..context import Ctx


def plan(ctx: Ctx) -> dict:
    pfile = ctx.run_dir / "protocol.yaml"
    given = ctx.topic.get("protocol_file")
    if given:
        protocol = yaml.safe_load(open(given))
        ctx.log(f"[plan] using protocol from {given}")
    elif pfile.exists():
        protocol = yaml.safe_load(open(pfile))
        ctx.log(f"[plan] using existing (possibly human-edited) {pfile}")
    else:
        seed = ctx.topic.get("seed_eligibility", "")
        seed_txt = f"SEED ELIGIBILITY TEXT (from the review team; stay faithful to it):\n{seed}" if seed else ""
        protocol = ctx.agent.chat(ctx.msgs(prompts.PLAN.format(question=ctx.topic["question"], seed=seed_txt)),
                                  tag="plan")
        ctx.log(f"[plan] drafted protocol with {len(protocol.get('criteria', []))} criteria")

    # validate key concepts in external databases (synonyms feed the search strategy)
    if ctx.cfg["pipeline"].get("validate_terms", True):
        for kc in protocol.get("key_concepts", []):
            if kc.get("kind") in ("drug", "disease"):
                v = terms.validate(kc["term"], kc["kind"])
                kc["validation"] = {k: v[k] for k in ("status", "preferred", "ids", "synonyms")}
                ctx.log(f"[plan] concept '{kc['term']}' -> {v['status']} {v['ids']}")
    with open(pfile, "w") as f:
        yaml.safe_dump(protocol, f, sort_keys=False, allow_unicode=True)

    st = ctx.store
    st.upsert("protocol", "protocol", protocol, reason="planned")
    for c in protocol.get("criteria", []):
        cid = f"crit:{c['id']}"
        old = st.get(cid)
        if old is None or old["data"] != c:
            st.upsert(cid, "criterion", c, depends_on=["protocol"], reason="protocol criterion")
    return protocol


def criteria_text(protocol: dict, stage: str | None = None) -> str:
    lines = []
    for c in protocol.get("criteria", []):
        if c.get("type") == "practical" and stage is not None:
            continue  # access/language requirements are checked by humans from metadata
        if stage == "title_abstract" and c.get("stage") == "full_text":
            continue
        lines.append(f"- {c['id']} ({c['type']}): {c['text']}")
    return "\n".join(lines)


def synonyms_text(protocol: dict) -> str:
    """Database-validated synonyms of key concepts (e.g. ACE910 = emicizumab) for the screener."""
    out = []
    for kc in protocol.get("key_concepts", []):
        v = kc.get("validation") or {}
        if v.get("synonyms"):
            out.append(f"- {kc['term']} ({', '.join(f'{k}:{x}' for k, x in (v.get('ids') or {}).items())}) "
                       f"also known as: {', '.join(v['synonyms'][:15])}")
    if not out:
        return ""
    return "KNOWN SYNONYMS (validated in RxNorm/ChEMBL/PubChem/MONDO):\n" + "\n".join(out) + "\n\n"


def protocol_brief(protocol: dict) -> str:
    return json.dumps({k: protocol.get(k) for k in ("question", "pico")}, indent=1)
