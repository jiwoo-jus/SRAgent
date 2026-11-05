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
        if isinstance(raw.get("note"), str) and raw["note"].strip():
            item["note"] = raw["note"].strip()[:300]
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


_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                      "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUMWORD = re.compile(r"\b(" + "|".join(_TENS) + r")(?:[- ](" + "|".join(w for w in _UNITS if w != "zero") + r"))?\b|\b("
                      + "|".join(_UNITS) + r")\b", re.I)


def _words_to_digits(m: re.Match) -> str:
    tens, unit, single = m.group(1), m.group(2), m.group(3)
    if single:
        return str(_UNITS[single.lower()])
    return str(_TENS[tens.lower()] + (_UNITS[unit.lower()] if unit else 0))


def _norm_numbers(text: str) -> str:
    """Forms seen in papers made comparable to the value's digits: 31·8 and 31,8 become 31.8; .222 becomes 0.222;
    "twenty-four" becomes 24."""
    t = re.sub(r"(?<=\d)[·,](?=\d)", ".", text)
    t = re.sub(r"(?<![\w.])\.(?=\d)", "0.", t)
    return _NUMWORD.sub(_words_to_digits, t)


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
    """Extraction read the whole paper and claims the study is not eligible (the upward check). A second agent, the
    verifier role (else the full-text screening model), decides on the paper. Agreement: the study is excluded, by
    "extraction + verifier", its outputs set aside. Disagreement: the study becomes undecided (pending), for a person
    (in agent-only mode the fulltext_unclear policy applies, flagged). Both claims, the verdict, the evidence and the
    outcome are recorded on the screening node (`eligibility_review`) and shown on the study page.
    Returns 'exclude', 'pending' or 'kept'."""
    import time
    from ..evidence import joined, packed, resolve
    from .screening import SCREEN_FLAG_SOURCES, human_decided
    st = ctx.store
    from ..selection import decision_node
    sc = decision_node(st, rid)          # the full-text decision that included the study
    if not sc:
        return "kept"
    cid, why = doubt["criterion"], doubt["why"]
    crit = next((c for c in _protocol(st).get("criteria", []) if c["id"] == cid), {"id": cid, "type": "inclusion", "text": doubt.get("text", "")})
    first = next((a for a in sc["data"].get("criteria", []) if a.get("id") == cid), {})
    text, kind = st.text_of(rid)
    limit = int(ctx.cfg["pipeline"].get("fulltext_screen_chars", 15000))
    checker = ctx.hub["verifier"] if (ctx.cfg.get("roles") or {}).get("verifier") else ctx.hub.agent_for("eligibility")
    out = checker.chat(ctx.msgs(prompts.ELIGIBILITY_VERIFY.format(
        cid=cid, ctype=crit.get("type", "inclusion"), ctext=crit.get("text", ""),
        screen_assessment=first.get("assessment", "not assessed"),
        screen_reason=f" ({first['reason']})" if first.get("reason") else "",
        why=why, kind=kind, text=packed(text, limit))), tag="eligibility_verify")
    verdict = "exclude" if str(out.get("verdict", "")).lower().startswith("exclude") else "include"
    spans = resolve(out.get("evidence") or [], text).get("spans", [])
    review = {"claim": {"by": "extraction", "criterion": cid, "why": why, "ts": time.strftime("%Y-%m-%d %H:%M")},
              "verifier": {"by": checker.name, "verdict": verdict, "explanation": str(out.get("explanation") or ""),
                           "evidence": [sp.get("sid") for sp in spans if sp.get("sid")], "quote": joined(spans)}}
    d = dict(sc["data"])
    if human_decided(d):     # a person decided this inclusion: it stands; the review is recorded for them
        review["outcome"] = "kept_human_decision"
        d["eligibility_review"] = review
        st.annotate(sc["id"], d, reason=f"eligibility review after extraction ({cid}): your decision stands")
        if not any(f["status"] == "open" and f.get("source") == DOUBT_SOURCE for f in st.get(sc["id"])["flags"]):
            st.flag(sc["id"], f"After extraction, two agents reviewed this inclusion you decided ({cid}): extraction says exclude "
                             f"({why[:160]}); the verifier says {verdict}. Your decision is kept; please check.", actor="agent", source=DOUBT_SOURCE)
        return "kept"
    crits = []
    for a in d.get("criteria", []):
        a = dict(a)
        if a.get("id") == cid and verdict == "exclude":
            a.update({"assessment": "met" if crit.get("type") == "exclusion" else "not_met", "reported": True,
                      "reason": f"after extraction, confirmed by the verifier: {review['verifier']['explanation'][:200]}",
                      "evidence": review["verifier"]["evidence"], "reviewed_after_extraction": True})
        crits.append(a)
    agent_only = ctx.cfg["pipeline"].get("human_mode") == "agent_only"
    if verdict == "exclude":
        review["outcome"] = "excluded"
        d.update({"criteria": crits, "decision": "exclude", "because": [cid], "decided_by": "extraction+verifier",
                  "eligibility_review": review})
        d.pop("unclear_by_rule", None)
        reason = f"excluded after extraction: the verifier agreed with the extraction's doubt on {cid}"
        outcome = "exclude"
    else:
        policy = ctx.cfg["pipeline"].get("fulltext_unclear", "await") if agent_only else "await"
        decision = {"include": "include", "exclude": "exclude"}.get(policy, "awaiting")
        review["outcome"] = "pending_disagreement" if decision == "awaiting" else f"{decision}_by_policy_disagreement"
        d.update({"decision": decision, "because": [cid], "decided_by": "extraction+verifier", "eligibility_review": review})
        if decision != "awaiting":
            d["unclear_by_rule"] = policy
        reason = f"two agents disagree after extraction on {cid}: extraction says exclude, the verifier says include; " + \
                 ("a person decides" if decision == "awaiting" else f"pipeline.fulltext_unclear: {policy}")
        outcome = "pending" if decision == "awaiting" else ("exclude" if decision == "exclude" else "kept")
    st.annotate(sc["id"], d, reason=reason)
    for f in st.get(sc["id"])["flags"]:
        if f["status"] == "open" and f.get("source") in SCREEN_FLAG_SOURCES + (DOUBT_SOURCE,):
            st.resolve_flag(sc["id"], f["id"], f"eligibility reviewed after extraction: {review['outcome']}", actor="agent")
    if outcome in ("exclude", "pending"):
        for kind_ in ("ext", "rob"):
            nid = f"{kind_}:{rid}"
            if st.get(nid) and st.get(nid)["status"] == "active":
                st.set_status(nid, "excluded", reason=f"study {'excluded' if outcome == 'exclude' else 'undecided'} after the eligibility review")
    if outcome == "kept" and not any(f["status"] == "open" and f.get("source") == DOUBT_SOURCE for f in st.get(sc["id"])["flags"]):
        st.flag(sc["id"], f"Two agents disagree after extraction ({cid}): extraction says exclude ({why[:160]}); the verifier says "
                         f"include ({review['verifier']['explanation'][:160]}). Included by pipeline.fulltext_unclear; check this inclusion.",
                actor="agent", source=DOUBT_SOURCE)
    ctx.log(f"[extract] {rid}: eligibility doubt on {cid}; verifier says {verdict}; outcome {review['outcome']}")
    return outcome


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
        outcome = recheck_eligibility(ctx, rid, doubt)
        if outcome in ("exclude", "pending"):   # not included any more: the extraction is kept aside, not stored as active
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
    from ..selection import node_id
    st.write(f"ext:{rid}", "extraction", data, depends_on=[node_id(st, rid), src_node] + field_deps(ctx.cfg),
              evidence=evidence, reason="extracted")
    n = st.get(f"ext:{rid}")
    for name, item in fields.items():
        # a value with no sentence behind it (check "none") and a number not found as written (item["uncovered"]) are
        # markers shown beside the value with the agent's note, not flags: a reader checks them when it matters
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
    rids = only or [rid for rid in st.included_ids() if (st.get(f"ext:{rid}") or {}).get("status") != "active"
                    or source_changed(st, f"ext:{rid}")]                 # not extracted, cleared, or the text changed
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
