"""Stage 4: data extraction with supporting passages + term normalisation.

Every field = {"value", "quote", "check": quote verification, "terms": DB validation (if any)}.
Fields whose quote cannot be found in the source are flagged for the reviewer.
"""
from __future__ import annotations

import json

from .. import prompts, terms
from ..context import Ctx
from ..evidence import attach, cites_of, numbered


def fields_text(fields: list[dict]) -> str:
    return "\n".join(f"- {f['name']}{' [' + f['kind'] + ']' if f.get('kind') in ('drug', 'disease') else ''}: "
                     f"{f['description']}" for f in fields)


def validate_field_terms(value: str, kind: str, entities: list[str] | None = None,
                         known: list[str] | None = None) -> list[dict]:
    cands = [e for e in (entities or []) if isinstance(e, str) and e.strip()]
    if not cands:
        cands = terms.candidate_terms(value, kind, known or [])
    out = []
    for t in list(dict.fromkeys(cands))[:4]:
        v = terms.validate(t, kind)
        if v["status"] != "skipped":
            out.append({k: v[k] for k in ("term", "status", "preferred", "ids")})
    return out


def known_terms(ctx: Ctx) -> list[str]:
    p = (ctx.store.get("protocol") or {}).get("data", {})
    out = []
    for kc in p.get("key_concepts", []):
        out.append(kc["term"])
        out += (kc.get("validation") or {}).get("synonyms", []) or []
    return out


def postprocess(ctx: Ctx, rid: str, fields_out: dict, text: str, src_node: str) -> tuple[dict, list]:
    spec = {f["name"]: f for f in ctx.topic["extraction_fields"]}
    evidence = []
    clean = {}
    for name, f in spec.items():
        raw = fields_out.get(name) or {"value": "NR"}
        if not isinstance(raw, dict):
            raw = {"value": str(raw)}
        ents = raw.get("entities") if isinstance(raw.get("entities"), list) else None
        item = {"value": str(raw.get("value", "NR"))}
        if item["value"].strip().upper() != "NR":
            evidence.append(attach(item, cites_of(raw), text, f"{name}={item['value'][:80]}", src_node))
            if f.get("kind") in ("drug", "disease") and ctx.cfg["pipeline"].get("validate_terms", True):
                item["terms"] = validate_field_terms(item["value"], f["kind"], ents, known_terms(ctx))
                if ents:
                    item["entities"] = ents
        clean[name] = item
    return clean, evidence


def extract_one(ctx: Ctx, rid: str) -> dict:
    st = ctx.store
    text, kind = st.text_of(rid)
    src_node = f"ft:{rid}" if st.get(f"ft:{rid}") else f"rec:{rid}"
    out = ctx.agent.chat(ctx.msgs(prompts.EXTRACT.format(
        question=ctx.topic["question"], fields=fields_text(ctx.topic["extraction_fields"]),
        kind=kind, rid=rid, text=numbered(text))), tag="extract")
    fields, evidence = postprocess(ctx, rid, out.get("fields", {}), text, src_node)
    data = {"rid": rid, "source_kind": kind, "fields": fields}
    st.upsert(f"ext:{rid}", "extraction", data, depends_on=[f"screen:{rid}", src_node, "protocol"],
              evidence=evidence, reason="extracted")
    n = st.get(f"ext:{rid}")
    for name, item in fields.items():
        if item.get("check", {}).get("status") == "none":
            st.flag(n["id"], f"No source location cited for '{name}'; please check the value.",
                    actor="agent", source="evidence_check")
        for t in item.get("terms", []):
            if t["status"] == "not_found":
                st.flag(n["id"], f"Term '{t['term']}' in '{name}' not found in RxNorm/PubChem/ChEMBL/MONDO.",
                        actor="agent", source="term_validation")
    return data


def extract_all(ctx: Ctx, only: list[str] | None = None) -> int:
    st = ctx.store
    rids = only or [rid for rid in st.included_ids() if st.get(f"ext:{rid}") is None]
    ctx.log(f"[extract] extracting {len(rids)} studies")
    ctx.map(lambda r: extract_one(ctx, r), rids)
    ctx.save()
    return len(rids)


def table_text(ctx: Ctx, rids: list[str] | None = None, with_quotes: bool = False) -> str:
    st = ctx.store
    rids = rids if rids is not None else st.included_ids()
    rows = []
    for rid in rids:
        n = st.get(f"ext:{rid}")
        if not n or n["status"] != "active":
            continue
        f = n["data"]["fields"]
        if with_quotes:
            d = {k: {"value": v["value"], "evidence": v.get("quote", "")} for k, v in f.items()}
        else:
            d = {k: v["value"] for k, v in f.items()}
        rows.append(f"[{rid}] " + json.dumps(d, ensure_ascii=False))
    return "\n".join(rows)


def revalidate_terms(ctx: Ctx) -> dict:
    """Re-run term validation on stored extractions (no LLM calls)."""
    from collections import Counter
    c = Counter()
    kinds = {f["name"]: f.get("kind") for f in ctx.topic["extraction_fields"]}
    for n in ctx.store.by_type("extraction"):
        for name, item in n["data"]["fields"].items():
            if kinds.get(name) in ("drug", "disease") and not item["value"].upper().startswith("NR"):
                item["terms"] = validate_field_terms(item["value"], kinds[name], item.get("entities"), known_terms(ctx))
                c.update(t["status"] for t in item["terms"])
        n["flags"] = [f for f in n["flags"] if f.get("source") != "term_validation"]
        for name, item in n["data"]["fields"].items():
            for t in item.get("terms", []):
                if t["status"] == "not_found":
                    ctx.store.flag(n["id"], f"Term '{t['term']}' in '{name}' not found in RxNorm/PubChem/ChEMBL/MONDO.",
                                   actor="agent", source="term_validation")
    ctx.save()
    return dict(c)
