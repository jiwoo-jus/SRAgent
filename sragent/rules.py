"""Deterministic screening rules: obvious exclusions never reach a human (or the LLM).

Each rule looks only at bibliographic metadata (title, publication type, DOI, abstract presence),
gives a plain reason, and can be switched on/off per topic (`screening_rules` in the topic file).
Human decisions are never overridden. Every rule exclusion stays visible ("Excluded by rules" in
the UI) and can be restored with one click.

  empty_record           no title and no abstract (e.g. a journal-issue record)
  conference_abstract    abstract-only publication: meeting abstract books, congress supplements,
                         poster/oral abstract collections, coded abstracts (e.g. "PSY57 - ...")
  erratum                corrections / errata / retractions
  editorial              editorials and news (comments that are letters or case reports are kept)
  review                 narrative or systematic reviews (no original data)  [topic decides]
  off_topic_no_abstract  no abstract AND the title mentions none of the review's key concepts
                         (or their database synonyms), e.g. "PRISMA statement."
"""
from __future__ import annotations

import re

RULES_VERSION = 2   # bump when patterns change, so existing reviews are re-checked automatically
DEFAULT_RULES = ["empty_record", "conference_abstract", "erratum", "editorial", "off_topic_no_abstract"]

_ABSTRACT_TITLE = re.compile(
    r"\babstracts?\b|\babstract book\b|^(oral|poster|posters?|speaker|invited)\s+(presentations?|abstracts?)\b|"
    r"\bproceedings\b|\bcongress(o)?\b|\bannual meeting\b|\bscientific meeting\b|\bsymposium\b|\bsummit\b|"
    r"\bjahrestagung\b|\bconvegno\b|\bkongress\b|\bworld congress\b|\bpeptalk\b", re.I)
_ABSTRACT_CODE = re.compile(r"^\s*(?:[A-Z]{1,5}-?\d{1,4}[A-Z]?)(?:\s*[-:  ]|\s+[A-Z])")  # PSY57 - , PO292 , SAT0048
_SUPPL_DOI = re.compile(
    r"\.s\d+$|\.v\d+\.s\d+$|suppl|10\.1182/blood\.v\d+\.\d+\.\d+\.\d+$|10\.1182/blood-\d{4}-(?:99-)?\d{5,7}$|"
    r"-eular\.|-\d{4}-[a-z]{3,}\.\d+$|10\.1016/j\.jval\.|10\.1016/j\.htct\.|10\.1016/j\.gheart\.|annrheumdis-\d{4}-", re.I)
_ERRATUM = re.compile(r"^\s*(correction|erratum|corrigendum|retraction|retracted)\b", re.I)


def classify(rec: dict, key_terms: list[str], enabled: list[str]) -> tuple[str, str] | None:
    title = (rec.get("title") or "").strip()
    abstract = (rec.get("abstract") or "").strip()
    ptypes = {p.lower() for p in rec.get("pub_types") or []}
    doi = (rec.get("doi") or "").lower()

    if "empty_record" in enabled and not title and not abstract:
        return "empty_record", "Record has no title and no abstract (not an article)."
    if "erratum" in enabled and (_ERRATUM.search(title) or ptypes & {"published erratum", "retraction of publication"}):
        return "erratum", "Correction / erratum / retraction notice."
    if "conference_abstract" in enabled:
        if ptypes & {"congress", "conference proceedings", "proceedings-article", "journal-issue"}:
            return "conference_abstract", f"Conference / meeting material ({', '.join(sorted(ptypes))}); abstract-only publication."
        if _ABSTRACT_TITLE.search(title) and not abstract:
            return "conference_abstract", "Meeting abstract collection or proceedings (title); no full article."
        if _SUPPL_DOI.search(doi):  # meeting-abstract DOI formats are abstract-only even when an abstract exists
            return "conference_abstract", f"Meeting-abstract supplement (DOI {doi}); abstract-only publication."
        if _ABSTRACT_CODE.match(title) and not abstract and rec.get("source") == "crossref":
            return "conference_abstract", "Coded meeting abstract (e.g. ISPOR / EULAR poster code); abstract-only publication."
    if "editorial" in enabled and ("editorial" in ptypes or "news" in ptypes) and not (ptypes & {"letter", "case reports"}):
        return "editorial", "Editorial / news item (no original data)."
    if "review" in enabled and ptypes & {"review", "systematic review", "meta-analysis"} and not (
            ptypes & {"clinical trial", "randomized controlled trial", "clinical trial, phase i", "clinical trial, phase iii"}):
        return "review", "Review article (no original data)."
    if "off_topic_no_abstract" in enabled and not abstract and title and key_terms:
        t = title.lower()
        if not any(k.lower() in t for k in key_terms if len(k) > 3):
            return "off_topic_no_abstract", ("No abstract, and the title mentions none of the review's key concepts "
                                             f"({', '.join(sorted(set(key_terms))[:6])}...).")
    return None


def key_terms(protocol: dict) -> list[str]:
    out = []
    for kc in protocol.get("key_concepts", []):
        if kc.get("kind") in ("drug", "disease"):
            out.append(kc["term"])
            out += (kc.get("validation") or {}).get("synonyms", []) or []
    return out


def enabled_rules(ctx) -> list[str]:
    r = ctx.topic.get("screening_rules")
    return DEFAULT_RULES if r is None else list(r)


def apply(ctx, protocol: dict | None = None) -> list[dict]:
    """Apply rules to every record that has no human decision. Returns the changes made."""
    st = ctx.store
    protocol = protocol or (st.get("protocol") or {}).get("data", {})
    keys, rules = key_terms(protocol), enabled_rules(ctx)
    changes = []
    for n in st.by_type("record"):
        rid = n["id"].split(":", 1)[1]
        sc = st.get(f"screen:{rid}")
        if sc and (sc["data"].get("human_decision") or sc["data"].get("rule_restored")):
            continue
        hit = classify(n["data"], keys, rules)
        if not hit:
            continue
        if sc and sc["data"].get("rule") == hit[0] and sc["data"]["decision"] == "exclude":
            continue
        before = sc["data"]["decision"] if sc else None
        data = dict(sc["data"]) if sc else {"stage": "title_abstract", "criteria": []}
        data.update({"decision": "exclude", "because": [f"rule:{hit[0]}"], "rule": hit[0], "rule_reason": hit[1],
                     "decision_before_rule": before})
        deps = sc["depends_on"] if sc else [f"rec:{rid}", "protocol"]
        st.upsert(f"screen:{rid}", "screen", data, depends_on=deps, actor="rule",
                  reason=f"screening rule {hit[0]}: {hit[1]}")
        for f in st.get(f"screen:{rid}")["flags"]:
            if f["status"] == "open":
                st.resolve_flag(f"screen:{rid}", f["id"], f"excluded by rule {hit[0]}", actor="rule")
        changes.append({"rid": rid, "rule": hit[0], "before": before})
    st.meta["rules_applied"] = {"rules": rules, "version": RULES_VERSION, "n": len(changes)}
    return changes


def restore(ctx, rid: str, note: str = "") -> None:
    """Undo a rule exclusion: the record goes back to 'awaiting' for a human decision."""
    st = ctx.store
    n = st.get(f"screen:{rid}")
    d = dict(n["data"])
    d["decision"] = d.get("decision_before_rule") or "awaiting"
    if d["decision"] in ("exclude", None):
        d["decision"] = "awaiting"
    d["because"] = []
    d["rule_restored"] = {"rule": d.pop("rule", None), "note": note}
    st.upsert(n["id"], "screen", d, depends_on=n["depends_on"], actor="user", reason=f"rule exclusion undone. {note}".strip())
