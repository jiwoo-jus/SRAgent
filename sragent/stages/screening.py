"""Stage 3: study selection (title/abstract, then full text).

The LLM assesses each atomic criterion separately (met / not_met / unclear + quote).
The decision is then derived by a transparent rule, so every exclusion can be traced to
a specific criterion and passage, and a reviewer can overturn a single criterion:

  exclude    if any inclusion criterion is not_met or any exclusion criterion is met
  uncertain  else if any criterion is unclear
  include    otherwise
Practical criteria (document access, language) are not judged by the LLM.

Final inclusion only after a full-text review (both human-involvement conditions):
  title/abstract   'exclude' or 'forward' (to full-text review). Nothing is included here.
  retrieval        every forwarded record needs its full text: user file > Europe PMC open access.
                   'fulltext_unavailable'  no full text could be obtained (requested as <PMID>.pdf)
                   'parse_failed'          a supplied file has no usable text (scanned PDF, empty)
  full text        screened on the full text only:
                   'include' | 'exclude' | 'awaiting' (undecided; pipeline.fulltext_unclear)
These statuses stay distinct in reports and evaluation: not retrieved, parse failure, undecided and
ineligible are never merged.

The LLM's own holistic decision is stored as `llm_decision` for analysis (hypothesis H1b:
how often does the holistic decision disagree with its own criterion-level assessments?).
"""
from __future__ import annotations

from .. import prompts
from ..context import Ctx
from ..evidence import attach, cites_of, packed
from .planning import criteria_text, synonyms_text


RETRIEVAL_FAILURES = ("fulltext_unavailable", "parse_failed")
OPEN_AT_TA = ("forward", "include")      # 'include' at title/abstract: a human decision before the full-text review


def finalize(decision: str, stage: str, policy: str = "await") -> str:
    if stage == "title_abstract":
        return "exclude" if decision == "exclude" else "forward"
    if decision == "uncertain":
        return {"await": "awaiting", "include": "include", "exclude": "exclude"}.get(policy, "awaiting")
    return decision


HUMAN_KEYS = ("human_decision", "human_decision_pending_fulltext")


def human_decided(d: dict) -> bool:
    """A person decided this record (or did, and the decision waits for its full text to come back)."""
    return any(d.get(k) for k in HUMAN_KEYS)


def complete_assessments(protocol: dict, out_criteria, stage: str) -> list[dict]:
    """The model's per-criterion assessments, with every applicable criterion present: one the model
    left out (or returned under a wrong id) counts as "unclear", never as passed. A dict keyed by
    criterion id is accepted too."""
    if isinstance(out_criteria, dict):
        out_criteria = [{"id": k, **(v if isinstance(v, dict) else {"assessment": v})} for k, v in out_criteria.items()]
    assessments = [a for a in (out_criteria or []) if isinstance(a, dict)]
    seen = {a.get("id") for a in assessments}
    for c in protocol.get("criteria", []):
        if c.get("type") == "practical" or c["id"] in seen:
            continue
        if stage == "title_abstract" and c.get("stage") == "full_text":
            continue
        assessments.append({"id": c["id"], "assessment": "unclear", "evidence": [], "missing": True})
    return assessments


def decide(protocol: dict, assessments: list[dict], stage: str, policy: str = "await") -> tuple[str, list[str], str | None]:
    """(decision, because, unclear_by_rule). unclear_by_rule is the pipeline.fulltext_unclear policy
    ("include" / "exclude") when it decided a record still unclear after its full text, else None."""
    dec, because = rule_decision(protocol, assessments, stage)
    final = finalize(dec, stage, policy)
    by_rule = policy if stage != "title_abstract" and dec == "uncertain" and final in ("include", "exclude") else None
    return final, because, by_rule


def rederive(protocol: dict, d: dict, policy: str = "await") -> tuple[str, list[str], str, str | None]:
    """(decision, because, stage, unclear_by_rule) for a stored screen node's assessments under a (revised)
    protocol. A record waiting at full-text retrieval was assessed on its title/abstract only: it keeps
    waiting unless those assessments now exclude it (it is never included without a full-text review)."""
    if d["stage"] == "retrieval":
        dec, because = rule_decision(protocol, d.get("criteria", []), "title_abstract")
        if dec == "exclude":
            return "exclude", because, "title_abstract", None
        return d["decision"], d.get("because", []), "retrieval", None
    dec, because, by_rule = decide(protocol, d.get("criteria", []), d["stage"], policy)
    return dec, because, d["stage"], by_rule


def rule_decision(protocol: dict, assessments: list[dict], stage: str) -> tuple[str, list[str]]:
    types = {c["id"]: c["type"] for c in protocol.get("criteria", [])}
    stages = {c["id"]: c.get("stage", "title_abstract") for c in protocol.get("criteria", [])}
    failed, unclear = [], []
    for a in assessments:
        cid, val = a.get("id"), a.get("assessment")
        if cid not in types or types[cid] == "practical":
            continue
        if stage == "title_abstract" and stages[cid] == "full_text":
            continue
        if (types[cid] == "inclusion" and val == "not_met") or (types[cid] == "exclusion" and val == "met"):
            failed.append(cid)
        elif val == "unclear":
            unclear.append(cid)
    if failed:
        return "exclude", failed
    if unclear:
        return "uncertain", unclear
    return "include", []


def record_text(rec: dict) -> str:
    # NOTE: never include benchmark labels here
    parts = [f"Title: {rec.get('title', '')}"]
    if rec.get("year"):
        parts.append(f"Year: {rec['year']}  Journal: {rec.get('journal', '')}")
    if rec.get("pub_types"):
        parts.append("Publication type: " + "; ".join(rec["pub_types"][:4]))
    parts.append("Abstract: " + (rec.get("abstract") or "(no abstract available)"))
    return "\n".join(parts)


def screen_source(st, rid: str, stage: str) -> tuple[str, str]:
    """The exact text the screener saw (evidence ids are resolved against it)."""
    if stage == "title_abstract":
        return record_text(st.get(f"rec:{rid}")["data"]), f"rec:{rid}"
    return st.text_of(rid)[0], f"ft:{rid}"


def screen_one(ctx: Ctx, protocol: dict, rid: str, stage: str = "title_abstract", tag_suffix: str = "",
               keep_human: bool = True) -> dict:
    """Screen one record at one stage. A decision a person made is kept (the assessments are refreshed,
    the decision is not), unless keep_human=False: an explicit re-run a person asked for."""
    st = ctx.store
    text, src_node = screen_source(st, rid, stage)
    limit = None if stage == "title_abstract" else int(ctx.cfg["pipeline"].get("fulltext_screen_chars", 15000))
    out = ctx.agent.chat(ctx.msgs(prompts.SCREEN.format(criteria=criteria_text(protocol, stage),
                                                        synonyms=synonyms_text(protocol),
                                                        record=packed(text, limit), stage=stage.replace("_", "/"))),
                         tag=f"screen_{stage}{tag_suffix}")
    assessments = complete_assessments(protocol, out.get("criteria"), stage)
    evidence = []
    for a in assessments:
        cites = cites_of(a)
        a.pop("quote", None)
        if cites:
            evidence.append(attach(a, cites, text, f"{a.get('id')}={a.get('assessment')}", src_node))
    decision, because, by_rule = decide(protocol, assessments, stage, ctx.cfg["pipeline"].get("fulltext_unclear", "await"))
    data = {"stage": stage, "decision": decision, "because": because, "criteria": assessments,
            "source_kind": "title_abstract" if stage == "title_abstract" else st.text_of(rid)[1],
            "llm_decision": out.get("decision"), "reason": out.get("reason", ""),
            "confidence": out.get("confidence")}
    if by_rule:
        data["unclear_by_rule"] = by_rule
    prev = st.get(f"screen:{rid}")
    reason = f"{stage} screening"
    if stage == "full_text" and prev:
        pd = prev["data"]
        data["title_abstract"] = pd.get("title_abstract") if pd.get("stage") != "title_abstract" else \
            {k: pd.get(k) for k in ("decision", "because")}
    if prev and keep_human:
        pd = prev["data"]
        hd = pd.get("human_decision")
        if not hd and pd.get("human_decision_pending_fulltext") and data["source_kind"] == "fulltext":
            hd = pd["human_decision_pending_fulltext"]      # the full text is here: the person's decision applies again
        if hd:
            data.update({"decision": hd["decision"], "because": pd.get("because") or [], "human_decision": hd,
                         "agent_decision": decision})
            data.pop("unclear_by_rule", None)
            reason = f"{stage} screening (assessments refreshed; the person's decision '{hd['decision']}' is kept)"
        elif pd.get("human_decision_pending_fulltext"):
            data["human_decision_pending_fulltext"] = pd["human_decision_pending_fulltext"]
    deps = [f"rec:{rid}", "protocol"] + [f"crit:{c['id']}" for c in protocol.get("criteria", [])]
    if stage == "full_text":
        deps.append(src_node)
    st.upsert(f"screen:{rid}", "screen", data, depends_on=deps, evidence=evidence, reason=reason)
    return data


def screen_all(ctx: Ctx, protocol: dict) -> dict:
    from .. import rules
    st = ctx.store
    ruled = rules.apply(ctx, protocol)   # obvious exclusions first: no LLM call, no human time
    if ruled:
        ctx.log(f"[screen] {len(ruled)} records excluded by screening rules (publication type / off-topic)")
    todo = [n["id"].split(":", 1)[1] for n in st.by_type("record")
            if st.get(f"screen:{n['id'].split(':', 1)[1]}") is None]
    ctx.log(f"[screen] title/abstract screening {len(todo)} records")
    ctx.map(lambda rid: screen_one(ctx, protocol, rid, "title_abstract"), todo)
    ctx.save()
    counts = {}
    for n in st.by_type("screen"):
        counts[n["data"]["decision"]] = counts.get(n["data"]["decision"], 0) + 1
    ctx.log(f"[screen] title/abstract decisions: {counts}")
    return counts


def needs_fulltext_review(st, rid: str) -> bool:
    n = st.get(f"screen:{rid}")
    if not n:
        return False
    d = n["data"]
    if d.get("rule") or d.get("human_decision"):
        return False     # a person decided; the agent does not screen it again
    dec, stage = d["decision"], d["stage"]
    if dec in RETRIEVAL_FAILURES or dec == "forward" or (stage == "title_abstract" and dec in OPEN_AT_TA):
        return True
    if stage == "full_text" and dec in ("include", "awaiting"):
        ft = (st.get(f"ft:{rid}") or {}).get("data", {})
        return ft.get("kind") != "fulltext" or d.get("source_kind") != "fulltext"
    return False


def _mark_retrieval(ctx: Ctx, rid: str, status: str, reason: str):
    st = ctx.store
    n = st.get(f"screen:{rid}")
    d = dict(n["data"])
    if d["decision"] == status and d["stage"] == "retrieval":
        return
    d.setdefault("title_abstract", {k: n["data"].get(k) for k in ("decision", "because")} if n["data"]["stage"] == "title_abstract" else d.get("title_abstract"))
    if d.get("human_decision", {}).get("decision") == "include":
        d["human_decision_pending_fulltext"] = d.pop("human_decision")
    d.update({"decision": status, "stage": "retrieval", "because": [], "retrieval_note": reason})
    st.upsert(n["id"], "screen", d, depends_on=n["depends_on"], reason=f"full-text retrieval: {status} ({reason})")


def review_one(ctx: Ctx, protocol: dict, rid: str, refetch: bool = False) -> str:
    """Full-text review of one forwarded record: retrieve, then screen on the full text only."""
    from . import fulltext
    st = ctx.store
    ft = (st.get(f"ft:{rid}") or {}).get("data")
    fetched = False
    if ft is None or refetch or (ft.get("kind") != "fulltext" and fulltext.local_file(ctx, rid) is not None):
        ft = fulltext.fetch_one(ctx, rid)
        fetched = True
    if ft.get("kind") == "fulltext":
        sc = st.get(f"screen:{rid}")["data"]
        if fetched or not (sc["stage"] == "full_text" and sc.get("source_kind") == "fulltext"):
            screen_one(ctx, protocol, rid, "full_text")
        return st.get(f"screen:{rid}")["data"]["decision"]
    if ft.get("parse_failed"):
        _mark_retrieval(ctx, rid, "parse_failed", f"the supplied file {ft.get('local_file')} has no usable text")
        return "parse_failed"
    _mark_retrieval(ctx, rid, "fulltext_unavailable", "no open-access full text found; PDF requested")
    return "fulltext_unavailable"


def fulltext_review(ctx: Ctx, protocol: dict) -> dict:
    """Every record forwarded from title/abstract gets a full-text review (retrieve + screen)."""
    from collections import Counter
    from . import fulltext
    st = ctx.store
    todo = [n["id"].split(":", 1)[1] for n in st.by_type("screen") if needs_fulltext_review(st, n["id"].split(":", 1)[1])]
    ctx.log(f"[screen] full-text review of {len(todo)} forwarded records")
    out = ctx.map(lambda rid: review_one(ctx, protocol, rid), todo)
    sync_awaiting_flags(ctx)
    from .. import rules
    rules.apply(ctx, protocol)
    fulltext.write_needed_pdfs(ctx)
    ctx.save()
    c = Counter(out)
    ctx.log(f"[screen] full-text review outcomes: {dict(c)}; included now {len(st.included_ids())}")
    return {"outcomes": dict(c), "included": len(st.included_ids())}


SCREEN_FLAG_SOURCES = ("screening", "screening_unclear")


def sync_awaiting_flags(ctx: Ctx):
    """Screening flags follow the decision: one 'awaiting classification' flag while a person must decide,
    one 'included although unclear' flag when pipeline.fulltext_unclear: include decided it, none otherwise."""
    st = ctx.store
    for n in st.by_type("screen"):
        d, rid = n["data"], n["id"].split(":", 1)[1]
        dec = d["decision"]
        want = None
        if dec == "awaiting":
            want = ("screening", f"Awaiting classification: criteria {d.get('because')} unclear after "
                                 f"full-text screening (source: {st.text_of(rid)[1]}). Please decide.")
        elif dec == "include" and d.get("unclear_by_rule") == "include":
            want = ("screening_unclear", f"Included by rule although criteria {d.get('because')} were still unclear "
                                         f"after full-text screening (pipeline.fulltext_unclear: include). Check this inclusion.")
        for f in n["flags"]:
            if f["status"] == "open" and f["source"] in SCREEN_FLAG_SOURCES and (not want or f["source"] != want[0]):
                st.resolve_flag(n["id"], f["id"], f"decision is now '{dec}'")
        if want and not any(f["status"] == "open" and f["source"] == want[0] for f in n["flags"]):
            st.flag(n["id"], want[1], actor="agent", source=want[0])


def rescreen_rule_only(ctx: Ctx, protocol: dict) -> dict:
    """Re-derive every decision from the STORED criterion assessments after a protocol edit
    (e.g. a criterion re-typed as practical). No LLM calls. Records newly forwarded from the
    title/abstract stage are screened at full text by the next `sragent run`."""
    st = ctx.store
    policy = ctx.cfg["pipeline"].get("fulltext_unclear", "await")
    changed = 0
    from .. import rules
    for n in st.by_type("screen"):
        if n["data"].get("rule") or human_decided(n["data"]):
            continue
        d = dict(n["data"])
        dec, because, stage, by_rule = rederive(protocol, d, policy)
        if (dec, because, stage, by_rule) != (d["decision"], d.get("because"), d["stage"], d.get("unclear_by_rule")):
            d.pop("note", None)
            d["decision"], d["because"], d["stage"] = dec, because, stage
            if by_rule:
                d["unclear_by_rule"] = by_rule
            else:
                d.pop("unclear_by_rule", None)
            st.upsert(n["id"], "screen", d, depends_on=n["depends_on"], actor="user",
                      reason="protocol revised: decision re-derived from stored assessments")
            for f in n["flags"]:
                if f["status"] == "open" and f["source"] in SCREEN_FLAG_SOURCES:
                    st.resolve_flag(n["id"], f["id"], "protocol revised")
            changed += 1
    changed += len(rules.apply(ctx, protocol))
    inc = set(st.included_ids())
    for kind in ("ext", "rob"):
        for n in st.by_type("extraction" if kind == "ext" else "rob"):
            rid = n["id"].split(":", 1)[1]
            want = "active" if rid in inc else "excluded"
            if n["status"] != want:
                st.set_status(n["id"], want, actor="user", reason="eligibility re-derived")
    sync_awaiting_flags(ctx)
    ctx.save()
    ctx.log(f"[screen] rule-only re-screen: {changed} decisions changed; included now {len(inc)}")
    return {"changed": changed, "included": len(inc)}
