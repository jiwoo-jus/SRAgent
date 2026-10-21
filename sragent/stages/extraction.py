"""Stage 4: data extraction with supporting passages + term normalisation.

Every field = {"value", "evidence": sentence ids, "spans"/"quote": the located sentences,
"check": location status, "terms": DB validation (if any)}.
Fields whose evidence cannot be located in the source are flagged for the reviewer.
"""
from __future__ import annotations

import json
import re

from .. import prompts, terms
from ..context import Ctx
from ..evidence import attach, chunks, cites_of, data_chars, packed, segment


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
            cites = cover_numbers(item, cites_of(raw), text)
            evidence.append(attach(item, cites, text, f"{name}={item['value'][:80]}", src_node))
            if f.get("kind") in ("drug", "disease") and ctx.cfg["pipeline"].get("validate_terms", True):
                item["terms"] = validate_field_terms(item["value"], f["kind"], ents, known_terms(ctx))
                if ents:
                    item["entities"] = ents
        clean[name] = item
    return clean, evidence


_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w.])")


MAX_COVER = 8   # citations a value may end up with after coverage (the extractor is asked for up to MAX_IDS)
_STOP = set("""the and for with from that this were was are not but than then into over under per each both all any
some most more less also only after before during between within without study studies patients patient subjects
subject data analysis analyses results result values value mean median range years year weeks week days day dose doses
group groups cohort cohorts phase trial trials reported report using used based total number level levels
""".split())


def _norm_numbers(text: str) -> str:
    """Decimal forms seen in papers made comparable to the value's: 31·8 and 31,8 become 31.8; .222 becomes 0.222."""
    t = re.sub(r"(?<=\d)[·,](?=\d)", ".", text)
    return re.sub(r"(?<![\w.])\.(?=\d)", "0.", t)


def _terms(value: str) -> list[str]:
    """Distinctive words of a value that a cited sentence should carry: capitalised names, acronyms, hyphenated or
    long technical words. Common words and anything with digits (numbers are covered separately) are skipped."""
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z\-/]{3,}", value):
        lw = w.lower().strip("-/")
        if lw in _STOP or len(lw) < 4:
            continue
        if w[0].isupper() or w.isupper() or "-" in w or len(lw) >= 9:
            out.append(w)
    return list(dict.fromkeys(out))


def cover_numbers(item: dict, cites: list, text: str) -> list:
    """Every number and every distinctive term in a value should be in a cited sentence. One that is not, but occurs
    in exactly one sentence of the source, gets that sentence cited (recorded in item["auto_evidence"], up to
    MAX_COVER citations); one that occurs in several sentences is left as it is (it is in the paper; which sentence
    is not guessed). A number found nowhere in the source, in any decimal form (31·8, 31,8, .222), is listed in
    item["uncovered"] for the reviewer: it may be a sum or a rounding the model made."""
    # single-digit integers (phase 1, 3 cohorts, week 5) carry little and match everywhere: not checked
    nums = [n for n in _NUM.findall(item["value"]) if len(n) <= 12 and not (n.isdigit() and int(n) < 10)]
    terms = _terms(item["value"])
    if not nums and not terms:
        return cites
    segs = segment(text)
    by_id = {s["sid"]: s for s in segs}
    norm = {s["sid"]: _norm_numbers(s["text"]) for s in segs}
    ids = [c for c in cites if isinstance(c, str) and c in by_id]
    cited_text = " ".join(norm[i] for i in ids)
    added, uncovered = [], []

    def place(rx, texts):
        nonlocal cited_text
        if rx.search(cited_text):
            return True
        where = [s for s in segs if rx.search(texts[s["sid"]])]
        if len(where) == 1 and where[0]["sid"] not in ids and len(ids) + len(added) < MAX_COVER:
            added.append(where[0]["sid"])
            cited_text += " " + norm[where[0]["sid"]]
            return True
        return bool(where)

    for n in dict.fromkeys(nums):
        if not place(re.compile(r"(?<![\w.])" + re.escape(n) + r"(?![\w.])"), norm):
            uncovered.append(n)
    for t in terms:   # terms are never flagged: wording may differ; only a missing citation is repaired
        place(re.compile(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])", re.I), {s["sid"]: s["text"] for s in segs})
    if added:
        item["auto_evidence"] = added
    if uncovered:
        item["uncovered"] = uncovered
    return list(cites) + added


def context_budget(ctx: Ctx) -> int:
    return int(ctx.cfg["pipeline"]["context_chars"])


def _merge(ctx: Ctx, rid: str, outs: list[dict], text: str) -> tuple[dict, list]:
    """Per field: one candidate -> keep; several different candidates -> one merge call."""
    spec = {f["name"]: f for f in ctx.topic["extraction_fields"]}
    by_id = {s["sid"]: s for s in segment(text)}
    final, need = {}, {}
    for name in spec:
        cands = []
        for o in outs:
            v = (o.get("fields") or {}).get(name)
            if isinstance(v, dict) and str(v.get("value", "NR")).strip().upper() != "NR":
                if all(str(v.get("value")).strip() != str(c.get("value")).strip() for c in cands):
                    cands.append(v)
        if not cands:
            final[name] = {"value": "NR", "evidence": []}
        elif len(cands) == 1:
            final[name] = cands[0]
        else:
            need[name] = cands
    merge_info = []
    if need:
        lines = []
        for name, cands in need.items():
            f = spec[name]
            lines.append(f"- {name}{' [' + f['kind'] + ']' if f.get('kind') in ('drug', 'disease') else ''}: {f['description']}")
            for i, c in enumerate(cands, 1):
                ev = " ".join(f"[{x}] {by_id[x]['text'][:240]}" for x in cites_of(c) if x in by_id)
                lines.append(f"    candidate {i}: {c.get('value')}\n      evidence: {ev or '(none)'}")
        out = ctx.agent.chat(ctx.msgs(prompts.EXTRACT_MERGE.format(rid=rid, candidates="\n".join(lines))), tag="extract_merge")
        for name in need:
            v = (out.get("fields") or {}).get(name)
            final[name] = v if isinstance(v, dict) else need[name][0]
        merge_info = sorted(need)
    return final, merge_info


def _protocol(st) -> dict:
    return (st.get("protocol") or {}).get("data") or {}


def _doubt(st, raw) -> dict | None:
    """A valid eligibility doubt from the extraction reply: a known, non-practical criterion id and a reason."""
    crits = {c["id"]: c for c in _protocol(st).get("criteria", [])}
    if not isinstance(raw, dict) or not raw.get("why"):
        return None
    c = crits.get(str(raw.get("criterion", "")).strip())
    if not c or c.get("type") == "practical":
        return None
    return {"criterion": c["id"], "text": c["text"], "why": str(raw["why"])[:400]}


DOUBT_SOURCE = "eligibility_doubt"


def recheck_eligibility(ctx: Ctx, rid: str, doubt: dict) -> str:
    """Extraction read the whole paper and doubts the inclusion (the upward check). In agent-only mode the record
    is screened again on its full text with that doubt as a note; an exclusion stands. Otherwise, or when the
    re-screen keeps it, the screening node is flagged for a person. Returns 'exclude' or 'flagged'."""
    import dataclasses
    from .screening import SCREEN_FLAG_SOURCES, human_decided, screen_one
    st = ctx.store
    what = f"{doubt['criterion']} ({doubt['text']}): {doubt['why']}"
    sc = st.get(f"screen:{rid}")
    agent_only = ctx.cfg["pipeline"].get("human_mode") == "agent_only"
    if agent_only and sc and not human_decided(sc["data"]) and st.get(f"ft:{rid}"):
        c = dataclasses.replace(ctx, stage="fulltext",
                                note=f"The data-extraction step read the whole paper and reports a doubt about eligibility: "
                                     f"{what}. Check this criterion against the whole paper and decide.")
        d = screen_one(c, _protocol(st), rid, "full_text", tag_suffix="_recheck")
        if d["decision"] == "exclude":
            for f in st.get(sc["id"])["flags"]:   # the screening flags of the old decision ('included by rule' ...) are settled
                if f["status"] == "open" and f.get("source") in SCREEN_FLAG_SOURCES:
                    st.resolve_flag(sc["id"], f["id"], f"decision is now 'exclude' ({what[:160]})", actor="agent")
            ctx.log(f"[extract] {rid}: excluded after the eligibility doubt raised at extraction ({what[:120]})")
            return "exclude"
    if sc and not any(f["status"] == "open" and f.get("source") == DOUBT_SOURCE for f in sc["flags"]):
        st.flag(sc["id"], f"Data extraction read the whole paper and doubts this inclusion: {what}. Check this inclusion.",
                actor="agent", source=DOUBT_SOURCE)
    return "flagged"


def extract_one(ctx: Ctx, rid: str) -> dict:
    st = ctx.store
    text, kind = st.text_of(rid)
    src_node = f"ft:{rid}" if st.get(f"ft:{rid}") else f"rec:{rid}"
    budget = context_budget(ctx)
    fields_spec = fields_text(ctx.topic["extraction_fields"])
    from .planning import criteria_text
    crit_spec = criteria_text(_protocol(st), "full_text") or "(none recorded)"
    mode, merged = "single", []
    if data_chars(text) <= budget:
        out = ctx.agent.chat(ctx.msgs(prompts.EXTRACT.format(question=ctx.topic["question"], fields=fields_spec, criteria=crit_spec,
                                                              kind=kind, rid=rid, text=packed(text, budget))), tag="extract")
        raw_fields = out.get("fields", {})
        doubt = _doubt(st, out.get("eligibility_doubt"))
    else:   # long paper: read it in consecutive parts (same sentence ids), then merge differing values
        parts = chunks(text, budget)
        outs = [ctx.agent.chat(ctx.msgs(prompts.EXTRACT.format(
            question=ctx.topic["question"], fields=fields_spec, criteria=crit_spec, kind=f"{kind}, part {i} of {len(parts)}",
            rid=rid, text=part)), tag="extract") for i, part in enumerate(parts, 1)]
        raw_fields, merged = _merge(ctx, rid, outs, text)
        mode = f"{len(parts)} parts"
        doubt = next((d for d in (_doubt(st, o.get("eligibility_doubt")) for o in outs) if d), None)
    if doubt:
        if recheck_eligibility(ctx, rid, doubt) == "exclude":
            old = st.get(f"ext:{rid}")
            if old and old["status"] == "active":
                st.set_status(old["id"], "retired", reason="the study was excluded after an eligibility doubt raised at extraction")
            return {}
    fields, evidence = postprocess(ctx, rid, raw_fields, text, src_node)
    kept = keep_human_edits(st.get(f"ext:{rid}"), fields)
    ftn = st.get(f"ft:{rid}")
    data = {"rid": rid, "source_kind": kind, "fields": fields, "read_mode": mode, "merged_fields": merged,
            "source_version": ftn["version"] if ftn else None}
    if doubt:
        data["eligibility_doubt"] = doubt
    if kept:
        data["kept_human_edits"] = kept
    from ..graph import field_deps
    st.upsert(f"ext:{rid}", "extraction", data, depends_on=[f"screen:{rid}", src_node] + field_deps(ctx.cfg),
              evidence=evidence, reason="extracted")
    n = st.get(f"ext:{rid}")
    uncovered = []
    for name, item in fields.items():
        if item.get("check", {}).get("status") == "none":
            st.flag(n["id"], f"No source location cited for '{name}'; please check the value.",
                    actor="agent", source="evidence_check")
        elif item.get("uncovered"):
            uncovered.append(f"{name}: {', '.join(item['uncovered'][:5])}")
    if uncovered:   # one flag per study, not one per field
        st.flag(n["id"], "Numbers not found in the paper's text as written (a sum, a rounding or a misread? check them against the paper): "
                + "; ".join(uncovered[:8]), actor="agent", source="evidence_check")
        for t in item.get("terms", []):
            if t["status"] == "not_found":
                st.flag(n["id"], f"Term '{t['term']}' in '{name}' not found in RxNorm/PubChem/ChEMBL/MONDO.",
                        actor="agent", source="term_validation")
    return data


def keep_human_edits(old: dict | None, fields: dict) -> list[str]:
    """A value a person edited survives a re-extraction: the person's value, note and evidence are kept
    and the fresh one is stored beside it as `agent_value`. Returns the field names kept."""
    kept = []
    for name, item in ((old or {}).get("data", {}).get("fields") or {}).items():
        if isinstance(item, dict) and item.get("edited") and name in fields:
            fresh = fields[name]
            fields[name] = {**item, "agent_value": fresh.get("value"), "agent_evidence": fresh.get("evidence", [])}
            kept.append(name)
    return kept


def source_changed(st, node_id: str) -> bool:
    """The full text was retrieved again (e.g. without truncation) after this output was made."""
    n, rid = st.get(node_id), node_id.split(":", 1)[1]
    ft = st.get(f"ft:{rid}")
    return bool(n and ft and n["data"].get("source_version") != ft["version"])


def extract_all(ctx: Ctx, only: list[str] | None = None) -> int:
    st = ctx.store
    rids = only or [rid for rid in st.included_ids() if st.get(f"ext:{rid}") is None or source_changed(st, f"ext:{rid}")]
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
