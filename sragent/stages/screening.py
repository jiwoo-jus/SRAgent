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


def settle(protocol: dict, assessments: list[dict], stage: str) -> list[dict]:
    """At the full-text stage an inclusion criterion is "unclear" only when the paper does not report the
    information it needs. If the model said "unclear" and also that the paper reports it (reported: true),
    the paper's content did not satisfy the criterion: the assessment becomes "not_met". The model's own
    answer is kept as `model_assessment` so the change is visible. Title/abstract assessments are left alone."""
    if stage == "full_text":
        crits = {c["id"]: c for c in protocol.get("criteria", [])}
        for a in assessments:
            c = crits.get(a.get("id"))
            if c and c["type"] == "inclusion" and a.get("assessment") == "unclear" and a.get("reported") is True:
                a["model_assessment"] = "unclear"
                a["assessment"] = "not_met"
                a["settled"] = "the paper reports this and it does not satisfy the criterion"
            elif c and c["type"] == "exclusion" and a.get("assessment") == "unclear" and a.get("reported") is False:
                a["model_assessment"] = "unclear"
                a["assessment"] = "not_met"
                a["settled"] = "the paper gives no sign of this, so the exclusion does not apply"
        # alternatives (any_of): the whole paper was read and it reports none of them: the group is not met
        groups: dict[str, list[dict]] = {}
        for a in assessments:
            c = crits.get(a.get("id"))
            if c and c["type"] == "inclusion" and c.get("any_of"):
                groups.setdefault(str(c["any_of"]), []).append(a)
        for members in groups.values():
            if members and all(a.get("assessment") == "unclear" and a.get("reported") is False for a in members):
                for a in members:
                    a["model_assessment"] = "unclear"
                    a["assessment"] = "not_met"
                    a["settled"] = "the paper reports none of the alternatives of this group"
    return assessments


def criterion_reasons(protocol: dict, d: dict, ids: list[str] | None = None) -> str:
    """'I2 (Study provides data on humans): <the model's reason>' for the given criteria (default: `because`),
    falling back to the model's overall reason when it gave none per criterion."""
    crits = {c["id"]: c for c in protocol.get("criteria", [])}
    ids = list(ids if ids is not None else d.get("because") or [])
    parts = []
    for a in d.get("criteria", []):
        if a.get("id") in ids:
            why = (a.get("reason") or "").strip()
            parts.append(f"{a['id']} ({crits.get(a['id'], {}).get('text', '')})" + (f": {why}" if why else ""))
    out = "; ".join(parts)
    if not any(a.get("reason") for a in d.get("criteria", []) if a.get("id") in ids) and d.get("reason"):
        out += f". Agent: {d['reason']}"
    return out


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
    crits = settle(protocol, [dict(a) for a in d.get("criteria", [])], d["stage"])
    d["criteria"] = crits
    dec, because, by_rule = decide(protocol, crits, d["stage"], policy)
    return dec, because, d["stage"], by_rule


def rule_decision(protocol: dict, assessments: list[dict], stage: str) -> tuple[str, list[str]]:
    """Exclude if an inclusion criterion is not met or an exclusion criterion is met; uncertain if any is
    unclear; else include. Inclusion criteria that share an `any_of` label are alternatives: the group
    is met when any member is met, not met only when every member is not met, otherwise unclear."""
    crits = {c["id"]: c for c in protocol.get("criteria", [])}
    failed, unclear = [], []
    groups: dict[str, list[tuple[str, str]]] = {}
    for a in assessments:
        cid, val = a.get("id"), a.get("assessment")
        c = crits.get(cid)
        if c is None or c["type"] == "practical":
            continue
        if stage == "title_abstract" and c.get("stage", "title_abstract") == "full_text":
            continue
        if c["type"] == "inclusion" and c.get("any_of"):
            groups.setdefault(str(c["any_of"]), []).append((cid, val))
            continue
        if (c["type"] == "inclusion" and val == "not_met") or (c["type"] == "exclusion" and val == "met"):
            failed.append(cid)
        elif val == "unclear":
            unclear.append(cid)
    for members in groups.values():
        vals = [v for _, v in members]
        if "met" in vals:
            continue
        if all(v == "not_met" for v in vals):
            failed += [cid for cid, _ in members]
        else:
            unclear += [cid for cid, v in members if v != "not_met"]
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
                                                        record=packed(text, limit), stage=stage.replace("_", "/"),
                                                        stage_rule=prompts.SCREEN_STAGE_RULE[stage])),
                         tag=f"screen_{stage}{tag_suffix}")
    assessments = settle(protocol, complete_assessments(protocol, out.get("criteria"), stage), stage)
    evidence = []
    for a in assessments:
        cites = cites_of(a)
        a.pop("quote", None)
        if cites:
            evidence.append(attach(a, cites, text, f"{a.get('id')}={a.get('assessment')}", src_node))
    decision, because, by_rule = decide(protocol, assessments, stage, ctx.cfg["pipeline"].get("fulltext_unclear", "await"))
    verify = None
    if _verify_wanted(ctx, stage, decision):
        verify, decision, because, by_rule = verify_screen(ctx, protocol, rid, stage, text, limit, assessments, decision, because, by_rule)
    data = {"stage": stage, "decision": decision, "because": because, "criteria": assessments,
            "source_kind": "title_abstract" if stage == "title_abstract" else st.text_of(rid)[1],
            "llm_decision": out.get("decision"), "reason": out.get("reason", ""),
            "confidence": out.get("confidence")}
    if by_rule:
        data["unclear_by_rule"] = by_rule
    if verify:
        data["screen_verify"] = verify
        if verify["outcome"] == "decided_by_verifier":
            data["decided_by"] = "verifier"
    prev = st.get(f"screen:{rid}")
    reason = f"{stage} screening" + (f" (verifier {'agreed' if verify['agree'] else 'disagreed: ' + verify['outcome']})" if verify else "")
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


def reassess(ctx: Ctx, protocol: dict, rid: str, crit_ids: list[str], stage: str, tag_suffix: str = "") -> dict:
    """Assess one record on only the given criteria (added or changed since it was screened), merge them
    into the stored assessments and re-derive the decision. A person's decision is kept."""
    st = ctx.store
    n = st.get(f"screen:{rid}")
    if not n or not crit_ids:
        return n["data"] if n else {}
    sub = {**protocol, "criteria": [c for c in protocol.get("criteria", []) if c["id"] in crit_ids]}
    if not sub["criteria"]:
        return n["data"]
    text, src_node = screen_source(st, rid, stage)
    limit = None if stage == "title_abstract" else int(ctx.cfg["pipeline"].get("fulltext_screen_chars", 15000))
    out = ctx.agent.chat(ctx.msgs(prompts.SCREEN.format(criteria=criteria_text(sub, stage), synonyms=synonyms_text(protocol),
                                                        record=packed(text, limit), stage=stage.replace("_", "/"),
                                                        stage_rule=prompts.SCREEN_STAGE_RULE[stage])),
                         tag=f"screen_{stage}{tag_suffix}")
    fresh = settle(sub, complete_assessments(sub, out.get("criteria"), stage), stage)
    evidence = list(n.get("evidence") or [])
    for a in fresh:
        cites = cites_of(a)
        a.pop("quote", None)
        if cites:
            evidence.append(attach(a, cites, text, f"{a.get('id')}={a.get('assessment')}", src_node))
        a["reassessed"] = True
    kept = [a for a in n["data"].get("criteria", []) if a.get("id") not in crit_ids]
    d = dict(n["data"], criteria=kept + fresh)
    dec, because, by_rule = decide(protocol, d["criteria"], n["data"]["stage"] if n["data"]["stage"] != "retrieval" else "title_abstract",
                                   ctx.cfg["pipeline"].get("fulltext_unclear", "await"))
    if n["data"]["stage"] == "retrieval":   # still waiting for its full text: it stays waiting unless excluded now
        if dec != "exclude":
            dec, because = n["data"]["decision"], n["data"].get("because", [])
        else:
            d["stage"] = "title_abstract"
    hd = n["data"].get("human_decision")
    agent_dec = dec
    if hd:
        dec, because = hd["decision"], n["data"].get("because", [])
        d["agent_decision"] = agent_dec
    d.update({"decision": dec, "because": because})
    d.pop("unclear_by_rule", None)
    if by_rule and not hd:
        d["unclear_by_rule"] = by_rule
    deps = [x for x in n["depends_on"] if not x.startswith("crit:")] + [f"crit:{c['id']}" for c in protocol.get("criteria", [])]
    if "protocol" not in deps:
        deps.append("protocol")
    st.upsert(n["id"], "screen", d, depends_on=deps, evidence=evidence,
              reason=f"re-assessed on criteria {', '.join(crit_ids)} (protocol changed)")
    if hd and agent_dec != hd["decision"] and agent_dec in ("include", "exclude", "forward") and \
            not any(f["status"] == "open" and f["source"] == "reassess" for f in n["flags"]):
        st.flag(n["id"], f"The protocol changed (criteria {', '.join(crit_ids)}): the agent would now say "
                f"'{agent_dec}', your decision '{hd['decision']}' is kept. Please check.", actor="agent", source="reassess")
    return d


def _verify_wanted(ctx: Ctx, stage: str, decision: str) -> bool:
    """pipeline.verify_screening: full_text checks every full-text decision; all also checks title/abstract
    exclusions (the recall-critical decision; forwarded records are checked by the full text anyway)."""
    mode = ctx.cfg["pipeline"].get("verify_screening") or "off"
    if mode == "off":
        return False
    if stage == "full_text":
        if decision == "awaiting":
            return mode in ("pending", "full_text", "all")     # the screener could not decide: a second reading
        return mode in ("full_text", "all")
    return mode == "all" and decision == "exclude"


def verify_screen(ctx: Ctx, protocol: dict, rid: str, stage: str, text: str, limit, assessments: list[dict],
                  decision: str, because: list[str], by_rule) -> tuple[dict, str, list[str], str | None]:
    """A second agent (roles.verifier, else the stage's model) reads the record and decides independently.
    Agreement: recorded. Disagreement: a title/abstract exclusion is forwarded to the full text (the full text is
    the better judge); a full-text decision becomes undecided for a person (agent-only: the fulltext_unclear
    policy applies, flagged). Both readings are kept on the screening node (`screen_verify`)."""
    import time
    from ..evidence import resolve, joined
    checker = ctx.hub["verifier"] if (ctx.cfg.get("roles") or {}).get("verifier") else ctx.hub.agent_for(
        "eligibility" if stage == "full_text" else "screen")
    brief = "; ".join(f"{a.get('id')}={a.get('assessment')}" + (f" ({a['reason'][:80]})" if a.get("reason") else "")
                      for a in assessments if isinstance(a, dict))
    out = checker.chat(ctx.msgs(prompts.SCREEN_VERIFY.format(
        criteria=criteria_text(protocol, stage), synonyms=synonyms_text(protocol), record=packed(text, limit),
        stage=stage.replace("_", "/"), assessments=brief, decision=decision, because=", ".join(because) or "-",
        stage_rule=prompts.SCREEN_STAGE_RULE[stage])), tag=f"screen_verify_{stage}")
    own = str(out.get("decision") or "").lower()
    own = {"include": "include", "exclude": "exclude"}.get(own, "uncertain")
    agree = bool(out.get("agree")) if "agree" in out else own == decision
    # the screener's decision in the verifier's terms: at title/abstract "forward" means include or uncertain
    screener = decision if stage == "full_text" else ("exclude" if decision == "exclude" else "uncertain")
    if stage != "full_text" and own in ("include", "uncertain") and screener == "uncertain":
        agree = True
    if own == screener:
        agree = True
    differs = [d for d in (out.get("differs_on") or []) if isinstance(d, dict) and d.get("id")]
    for d in differs:
        d["evidence"] = [sp.get("sid") for sp in resolve(d.get("evidence") or [], text).get("spans", []) if sp.get("sid")]
    rec = {"by": checker.name, "decision": own, "agree": agree, "explanation": str(out.get("explanation") or "")[:400],
           "differs_on": differs, "ts": time.strftime("%Y-%m-%d %H:%M")}
    if decision == "awaiting":   # the screener could not decide: the verifier's reading decides, or it stays with a person
        if own in ("include", "exclude"):
            rec["outcome"] = "decided_by_verifier"
            ids = [d["id"] for d in differs] or list(because)
            for d in differs:    # the verifier's assessments replace the unclear ones it resolved
                for a in assessments:
                    if isinstance(a, dict) and a.get("id") == d["id"] and d.get("assessment"):
                        a.update({"assessment": d["assessment"], "reason": f"verifier: {d.get('reason', '')}"[:300],
                                  "evidence": d.get("evidence") or [], "reported": True, "model_assessment": a.get("assessment")})
            return rec, own, ids if own == "exclude" else [], None
        rec["outcome"] = "still_undecided"
        return rec, decision, because, by_rule
    if agree:
        rec["outcome"] = "agreed"
        return rec, decision, because, by_rule
    ids = [d["id"] for d in differs] or list(because) or [a.get("id") for a in assessments[:1] if isinstance(a, dict)]
    if stage != "full_text":
        rec["outcome"] = "forwarded"            # the full text decides
        return rec, "forward", ids, None
    agent_only = ctx.cfg["pipeline"].get("human_mode") == "agent_only"
    policy = ctx.cfg["pipeline"].get("fulltext_unclear", "await") if agent_only else "await"
    new = {"include": "include", "exclude": "exclude"}.get(policy, "awaiting")
    rec["outcome"] = "pending_disagreement" if new == "awaiting" else f"{new}_by_policy_disagreement"
    return rec, new, ids, (policy if new != "awaiting" else None)


def verify_existing(ctx: Ctx, protocol: dict, mode: str = "full_text", only: list[str] | None = None) -> dict:
    """The second reading for records already screened (pipeline.verify_screening was off then): the records undecided
    after their full text (every mode), with "full_text" every full-text include/exclude too, with "all" every
    title/abstract exclusion by the agent as well; decisions a person made and records already read twice are skipped.
    Outcomes are applied as at screening time."""
    from collections import Counter
    st = ctx.store
    todo = []
    for n in st.by_type("screen", active_only=True):
        d = n["data"]
        rid = n["id"].split(":", 1)[1]
        if only and rid not in only:
            continue
        if d.get("human_decision") or d.get("rule") or d.get("screen_verify") or d.get("eligibility_review"):
            continue                                           # decided by a person, by rule, or already read twice
        if d["stage"] == "full_text" and d["decision"] == "awaiting":
            todo.append(rid)                                   # every mode: the records the screener could not decide
        elif mode in ("full_text", "all") and d["stage"] == "full_text" and d["decision"] in ("include", "exclude"):
            todo.append(rid)
        elif mode == "all" and d["stage"] == "title_abstract" and d["decision"] == "exclude":
            todo.append(rid)
    ctx.log(f"[screen] second reading of {len(todo)} decisions ({mode})")

    def one(rid):
        n = st.get(f"screen:{rid}")
        d = dict(n["data"])
        stage = d["stage"]
        text, _ = screen_source(st, rid, stage)
        limit = None if stage == "title_abstract" else int(ctx.cfg["pipeline"].get("fulltext_screen_chars", 15000))
        rec, dec, because, by_rule = verify_screen(ctx, protocol, rid, stage, text, limit, d.get("criteria", []),
                                                   d["decision"], d.get("because", []), d.get("unclear_by_rule"))
        d["screen_verify"] = rec
        if rec["outcome"] not in ("agreed", "still_undecided"):
            d.update({"decision": dec, "because": because})
            d.pop("unclear_by_rule", None)
            if by_rule:
                d["unclear_by_rule"] = by_rule
            if rec["outcome"] == "decided_by_verifier":
                d["decided_by"] = "verifier"
        st.upsert(n["id"], "screen", d, depends_on=n["depends_on"], current=False,
                  reason=f"second reading by the verifier: {rec['outcome']}")
        if rec["outcome"] not in ("agreed", "still_undecided") and rid not in st.included_ids():
            for kind in ("ext", "rob"):
                if st.get(f"{kind}:{rid}") and st.get(f"{kind}:{rid}")["status"] == "active":
                    st.set_status(f"{kind}:{rid}", "excluded", reason="study no longer included after the second reading")
        return rec["outcome"]
    with ctx.at("eligibility"):
        outcomes = ctx.map(one, todo)
    sync_awaiting_flags(ctx)
    ctx.save()
    c = dict(Counter(outcomes))
    ctx.log(f"[screen] second reading done: {c}")
    return {"checked": len(todo), "outcomes": c}


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
    st.upsert(n["id"], "screen", d, depends_on=n["depends_on"], reason=f"full-text retrieval: {status} ({reason})",
              current=False)


def retrieve_one(ctx: Ctx, rid: str, refetch: bool = False) -> tuple[str, bool]:
    """Stage 4 for one forwarded record: its full text (a person's file, else an open-access copy). No model calls.
    Returns (kind of text now held, fetched now); a record without a usable full text is marked for retrieval."""
    from . import fulltext
    st = ctx.store
    ft = (st.get(f"ft:{rid}") or {}).get("data")
    fetched = False
    if ft is None or refetch or (ft.get("kind") != "fulltext" and fulltext.local_file(ctx, rid) is not None):
        ft = fulltext.fetch_one(ctx, rid)
        fetched = True
    if ft.get("kind") == "fulltext":
        return "fulltext", fetched
    if ft.get("parse_failed"):
        _mark_retrieval(ctx, rid, "parse_failed", f"the supplied file {ft.get('local_file')} has no usable text")
        return "parse_failed", fetched
    _mark_retrieval(ctx, rid, "fulltext_unavailable", "no open-access full text found; PDF requested")
    return "fulltext_unavailable", fetched


def eligibility_one(ctx: Ctx, protocol: dict, rid: str, fetched: bool = False) -> str:
    """Stage 5 for one record with a full text: screened on it (again only when the text is new to the decision)."""
    sc = ctx.store.get(f"screen:{rid}")["data"]
    if fetched or not (sc["stage"] == "full_text" and sc.get("source_kind") == "fulltext"):
        screen_one(ctx, protocol, rid, "full_text")
    return ctx.store.get(f"screen:{rid}")["data"]["decision"]


def review_one(ctx: Ctx, protocol: dict, rid: str, refetch: bool = False) -> str:
    """Full-text review of one forwarded record: retrieve, then screen on the full text only (one study: an added
    PDF, a re-run)."""
    kind, fetched = retrieve_one(ctx, rid, refetch)
    return eligibility_one(ctx, protocol, rid, fetched) if kind == "fulltext" else kind


def _forwarded(st) -> list[str]:
    return [n["id"].split(":", 1)[1] for n in st.by_type("screen") if needs_fulltext_review(st, n["id"].split(":", 1)[1])]


def retrieve_all(ctx: Ctx) -> dict:
    """Stage 4: the full text of every record forwarded from title/abstract (no model calls); the PDFs still
    needed are listed in needed_pdfs.csv."""
    from collections import Counter
    from . import fulltext
    st = ctx.store
    todo = _forwarded(st)
    ctx.log(f"[retrieve] full texts of {len(todo)} forwarded records")
    out = ctx.map(lambda rid: retrieve_one(ctx, rid)[0], todo)
    fulltext.write_needed_pdfs(ctx)
    ctx.save()
    c = Counter(out)
    ctx.log(f"[retrieve] {dict(c)}")
    return {"outcomes": dict(c)}


def eligibility_all(ctx: Ctx, protocol: dict) -> dict:
    """Stage 5: every forwarded record that has its full text is screened on it; then the screening flags, the
    rules and the list of PDFs still needed follow the decisions."""
    from collections import Counter
    from . import fulltext
    st = ctx.store
    todo = [rid for rid in _forwarded(st) if ((st.get(f"ft:{rid}") or {}).get("data") or {}).get("kind") == "fulltext"]
    ctx.log(f"[eligibility] full-text screening of {len(todo)} records")
    out = ctx.map(lambda rid: eligibility_one(ctx, protocol, rid), todo)
    sync_awaiting_flags(ctx)
    from .. import rules
    rules.apply(ctx, protocol)
    fulltext.write_needed_pdfs(ctx)
    ctx.save()
    c = Counter(out)
    ctx.log(f"[eligibility] outcomes: {dict(c)}; included now {len(st.included_ids())}")
    return {"outcomes": dict(c), "included": len(st.included_ids())}


def fulltext_review(ctx: Ctx, protocol: dict) -> dict:
    """Stages 4 and 5 together: every record forwarded from title/abstract retrieved, then screened on its full text."""
    r = retrieve_all(ctx)
    e = eligibility_all(ctx, protocol)
    return {"outcomes": _merge_outcomes(r, e), "included": e["included"]}


def _merge_outcomes(r: dict, e: dict) -> dict:
    """One outcome per record: its eligibility decision, else its retrieval status."""
    out = dict(e["outcomes"])
    for k, n in r["outcomes"].items():
        if k != "fulltext":
            out[k] = out.get(k, 0) + n
    return out


SCREEN_FLAG_SOURCES = ("screening", "screening_unclear")


def sync_awaiting_flags(ctx: Ctx):
    """Screening flags follow the decision: one 'awaiting classification' flag while a person must decide,
    one 'included although unclear' flag when pipeline.fulltext_unclear: include decided it, none otherwise."""
    st = ctx.store
    protocol = (st.get("protocol") or {}).get("data") or {}
    for n in st.by_type("screen"):
        d, rid = n["data"], n["id"].split(":", 1)[1]
        dec = d["decision"]
        want = None
        if dec == "awaiting" and (d.get("screen_verify") or {}).get("outcome") == "pending_disagreement":
            v = d["screen_verify"]
            want = ("screening", f"Awaiting classification: the screener and the verifier disagree after full-text screening "
                                 f"(verifier: {v.get('decision')}; {v.get('explanation', '')[:200]}). Please decide.")
        elif dec == "awaiting":
            want = ("screening", f"Awaiting classification: criteria {d.get('because')} unclear after "
                                 f"full-text screening (source: {st.text_of(rid)[1]}). Please decide. "
                                 f"{criterion_reasons(protocol, d)}".rstrip())
        elif dec == "include" and d.get("unclear_by_rule") == "include":
            want = ("screening_unclear", f"Included by rule although criteria {d.get('because')} were still unclear "
                                         f"after full-text screening (pipeline.fulltext_unclear: include). Check this inclusion. "
                                         f"{criterion_reasons(protocol, d)}".rstrip())
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
            st.upsert(n["id"], "screen", d, depends_on=n["depends_on"], actor="user", current=False,
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
