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

from .. import prompts, schemas
from ..context import Ctx
from ..evidence import attach, cites_of, packed
from .planning import criteria_text, synonyms_text


from ..selection import OPEN_AT_TA, RETRIEVAL_FAILURES, decision_node, elig_id, human_decided, screen_id  # noqa: F401


def finalize(decision: str, stage: str, policy: str = "await") -> str:
    if stage == "title_abstract":
        return "exclude" if decision == "exclude" else "forward"
    if decision == "uncertain":
        return {"await": "awaiting", "include": "include", "exclude": "exclude"}.get(policy, "awaiting")
    return decision


def node_for(st, rid: str, stage: str):
    """The node of one stage: title/abstract -> screen:<rid>, full text -> elig:<rid> (selection.py)."""
    return st.get(screen_id(rid) if stage == "title_abstract" else elig_id(rid))


def person_decides(st, rid: str, decision: str, because: list[str], human: dict, reason: str) -> dict:
    """A person's eligibility decision: recorded on elig:<rid> (created when the study has none yet), over the
    study's record as it stands (selection.view). A rule exclusion it overrides is kept as `rule_restored`."""
    from ..selection import view
    el = st.get(elig_id(rid))
    d = dict(view(st, rid) or {"stage": "title_abstract", "criteria": []})
    d.update({"decision": decision, "because": because, "human_decision": human})
    if d.get("rule"):
        d["rule_restored"] = {"rule": d.pop("rule"), "note": "manual decision"}
    for k in ("retrieval_note", "human_decision_pending_fulltext"):
        if decision != "awaiting":
            d.pop(k, None)
    if el:
        return st.annotate(el["id"], d, actor="user", reason=reason)
    deps = [screen_id(rid), "protocol"] + ([f"ft:{rid}"] if st.get(f"ft:{rid}") else []) + \
        [n["id"] for n in st.by_type("criterion", active_only=True)]
    return st.annotate(elig_id(rid), d, ntype="eligibility", depends_on=deps, actor="user", reason=reason)


def stage_of(n: dict) -> str:
    return "full_text" if n["type"] == "eligibility" else "title_abstract"


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
    """(decision, because, stage, unclear_by_rule) for a stored decision node's assessments under a (revised)
    protocol (title/abstract: forward or exclude, never include; full text: include, exclude or awaiting)."""
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


def _screen_prompt(ctx: Ctx, protocol: dict, rid: str, stage: str, tag_suffix: str = "") -> tuple[list[dict], str]:
    """The (messages, tag) of one screening call."""
    text = screen_source(ctx.store, rid, stage)[0]
    limit = None if stage == "title_abstract" else int(ctx.cfg["pipeline"].get("fulltext_screen_chars", 15000))
    return ctx.msgs(prompts.SCREEN.format(criteria=criteria_text(protocol, stage), synonyms=synonyms_text(protocol),
                                          record=packed(text, limit), stage=stage.replace("_", "/"),
                                          stage_rule=prompts.SCREEN_STAGE_RULE[stage])), f"screen_{stage}{tag_suffix}"


def screen_one(ctx: Ctx, protocol: dict, rid: str, stage: str = "title_abstract", tag_suffix: str = "",
               keep_human: bool = True) -> dict:
    """Screen one record at one stage. A decision a person made is kept (the assessments are refreshed,
    the decision is not), unless keep_human=False: an explicit re-run a person asked for."""
    st = ctx.store
    text, src_node = screen_source(st, rid, stage)
    limit = None if stage == "title_abstract" else int(ctx.cfg["pipeline"].get("fulltext_screen_chars", 15000))
    messages, tag = _screen_prompt(ctx, protocol, rid, stage, tag_suffix)
    out = ctx.writer.chat(messages, tag=tag, schema=schemas.SCREEN)
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
    prev = node_for(st, rid, stage)
    reason = f"{stage} screening" + (f" (verifier {'agreed' if verify['agree'] else 'disagreed: ' + verify['outcome']})" if verify else "")
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
    crit_deps = ["protocol"] + [f"crit:{c['id']}" for c in protocol.get("criteria", [])]
    if stage == "title_abstract":
        st.write(screen_id(rid), "screen", data, depends_on=[f"rec:{rid}"] + crit_deps, evidence=evidence, reason=reason)
    else:   # full text: depends on the title/abstract decision that forwarded it, the text, and the unclear policy
        deps = [screen_id(rid), src_node] + crit_deps + (["cfg:fulltext_unclear"] if st.get("cfg:fulltext_unclear") else [])
        st.write(elig_id(rid), "eligibility", data, depends_on=deps, evidence=evidence, reason=reason)
    return data


def reassess(ctx: Ctx, protocol: dict, rid: str, crit_ids: list[str], stage: str, tag_suffix: str = "") -> dict:
    """Assess one record on only the given criteria (added or changed since it was screened), merge them
    into the stored assessments and re-derive the decision. A person's decision is kept."""
    st = ctx.store
    n = node_for(st, rid, stage)
    if not n or not crit_ids:
        return n["data"] if n else {}
    sub = {**protocol, "criteria": [c for c in protocol.get("criteria", []) if c["id"] in crit_ids]}
    if not sub["criteria"]:
        return n["data"]
    text, src_node = screen_source(st, rid, stage)
    limit = None if stage == "title_abstract" else int(ctx.cfg["pipeline"].get("fulltext_screen_chars", 15000))
    out = ctx.writer.chat(ctx.msgs(prompts.SCREEN.format(criteria=criteria_text(sub, stage), synonyms=synonyms_text(protocol),
                                                        record=packed(text, limit), stage=stage.replace("_", "/"),
                                                        stage_rule=prompts.SCREEN_STAGE_RULE[stage])),
                         tag=f"screen_{stage}{tag_suffix}", schema=schemas.SCREEN)
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
    dec, because, by_rule = decide(protocol, d["criteria"], stage_of(n), ctx.cfg["pipeline"].get("fulltext_unclear", "await"))
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
    st.write(n["id"], n["type"], d, depends_on=deps, evidence=evidence,
             reason=f"re-assessed on criteria {', '.join(crit_ids)} (protocol changed)")
    return d   # a reading that differs from a person's decision is a derived note (attention.py)


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
    """A second agent (the stage's verifier) reads the record and decides independently.
    Agreement: recorded. Disagreement: a title/abstract exclusion is forwarded to the full text (the full text is
    the better judge); a full-text decision becomes undecided for a person (agent-only: the fulltext_unclear
    policy applies, flagged). Both readings are kept on the screening node (`screen_verify`)."""
    import time
    from ..evidence import resolve, joined
    checker = ctx.hub.model("eligibility" if stage == "full_text" else "screen", "verifier")
    common = dict(criteria=criteria_text(protocol, stage), synonyms=synonyms_text(protocol), record=packed(text, limit),
                  stage=stage.replace("_", "/"), stage_rule=prompts.SCREEN_STAGE_RULE[stage])
    blind = bool(ctx.cfg["pipeline"].get("verify_screening_blind"))
    if blind:   # the verifier does not see the screener's answer; the readings are compared here
        out = checker.chat(ctx.msgs(prompts.SCREEN_VERIFY_BLIND.format(**common)), tag=f"screen_verify_{stage}", schema=schemas.SCREEN_VERIFY_BLIND)
        first = {a.get("id"): a.get("assessment") for a in assessments if isinstance(a, dict)}
        out["differs_on"] = [a for a in out.get("assessments") or []
                             if isinstance(a, dict) and a.get("id") in first and a.get("assessment") != first[a["id"]]]
        out.pop("agree", None)
    else:
        brief = "; ".join(f"{a.get('id')}={a.get('assessment')}" + (f" ({a['reason'][:80]})" if a.get("reason") else "")
                          for a in assessments if isinstance(a, dict))
        out = checker.chat(ctx.msgs(prompts.SCREEN_VERIFY.format(**common, assessments=brief, decision=decision,
                                                                 because=", ".join(because) or "-")), tag=f"screen_verify_{stage}", schema=schemas.SCREEN_VERIFY)
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
    rec = {"by": checker.name, "blind": blind, "decision": own, "agree": agree, "explanation": str(out.get("explanation") or "")[:400],
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
    for n in [decision_node(st, r) for r in sorted({x["id"].split(":", 1)[1] for x in st.by_type("screen")})]:
        if n is None or n["status"] != "active":
            continue
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
        n = decision_node(st, rid)
        d = dict(n["data"])
        stage = stage_of(n)
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
        st.annotate(n["id"], d, reason=f"second reading by the verifier: {rec['outcome']}")
        if rec["outcome"] not in ("agreed", "still_undecided") and rid not in st.included_ids():
            for kind in ("ext", "rob"):
                if st.get(f"{kind}:{rid}") and st.get(f"{kind}:{rid}")["status"] == "active":
                    st.set_status(f"{kind}:{rid}", "excluded", reason="study no longer included after the second reading")
        return rec["outcome"]
    with ctx.at("eligibility"):
        outcomes = ctx.map(one, todo)
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
    todo = [n["id"].split(":", 1)[1] for n in st.by_type("record")      # not screened, or cleared (inactive)
            if (st.get(f"screen:{n['id'].split(':', 1)[1]}") or {}).get("status") != "active"]
    ctx.log(f"[screen] title/abstract screening {len(todo)} records")
    ctx.map_batched("screen", lambda rid: _screen_prompt(ctx, protocol, rid, "title_abstract"),
                    lambda rid: screen_one(ctx, protocol, rid, "title_abstract"), todo, schema=schemas.SCREEN)
    ctx.save()
    counts = {}
    for n in st.by_type("screen"):
        counts[n["data"]["decision"]] = counts.get(n["data"]["decision"], 0) + 1
    ctx.log(f"[screen] title/abstract decisions: {counts}")
    return counts


def needs_fulltext_review(st, rid: str) -> bool:
    """A study forwarded at title/abstract that has no full-text decision made on its full text yet (no elig node,
    or one made without the full text). A person's decision is never screened again by the agent."""
    sc = st.get(screen_id(rid))
    if not sc or sc["data"].get("rule") or sc["data"].get("human_decision") or sc["data"]["decision"] not in OPEN_AT_TA:
        return False
    el = st.get(elig_id(rid))
    if not el or el["status"] != "active":
        return True
    d = el["data"]
    if d.get("human_decision"):
        return False     # a person decided; the agent does not screen it again
    if d.get("human_decision_pending_fulltext"):
        return True      # the person's decision waits for the full text
    if d["decision"] in ("include", "awaiting"):
        ft = (st.get(f"ft:{rid}") or {}).get("data", {})
        return ft.get("kind") != "fulltext" or d.get("source_kind") != "fulltext"
    return False


def _mark_retrieval(ctx: Ctx, rid: str, status: str, reason: str):
    """A forwarded study without a usable full text. Its status is derived from ft:<rid> (selection.view); only a
    person's inclusion is set aside here, to apply again once the full text is back."""
    st = ctx.store
    el = st.get(elig_id(rid))
    if el and (el["data"].get("human_decision") or {}).get("decision") == "include":
        d = dict(el["data"])
        d["human_decision_pending_fulltext"] = d.pop("human_decision")
        st.annotate(el["id"], d, reason=f"full-text retrieval: {status} ({reason}); your inclusion waits for the full text")


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


def _needs_full_text_screen(ctx: Ctx, rid: str) -> bool:
    el = (ctx.store.get(elig_id(rid)) or {}).get("data") or {}
    return el.get("source_kind") != "fulltext" or bool(el.get("human_decision_pending_fulltext"))


def eligibility_one(ctx: Ctx, protocol: dict, rid: str, fetched: bool = False) -> str:
    """Stage 5 for one record with a full text: screened on it (again only when the text is new to the decision)."""
    if fetched or _needs_full_text_screen(ctx, rid):
        screen_one(ctx, protocol, rid, "full_text")
    return ctx.store.get(elig_id(rid))["data"]["decision"]


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
    out = ctx.map_batched("eligibility", lambda rid: _screen_prompt(ctx, protocol, rid, "full_text") if _needs_full_text_screen(ctx, rid) else None,
                          lambda rid: eligibility_one(ctx, protocol, rid), todo, schema=schemas.SCREEN)
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


SCREEN_FLAG_SOURCES = ("screening", "screening_unclear")   # derived notes now (attention.py); kept for older stores


def rescreen_rule_only(ctx: Ctx, protocol: dict) -> dict:
    """Re-derive every decision from the STORED criterion assessments after a protocol edit
    (e.g. a criterion re-typed as practical). No LLM calls. Records newly forwarded from the
    title/abstract stage are screened at full text by the next `sragent run`."""
    st = ctx.store
    policy = ctx.cfg["pipeline"].get("fulltext_unclear", "await")
    changed = 0
    from .. import rules
    for n in st.by_type("screen") + st.by_type("eligibility", active_only=True):
        if n["data"].get("rule") or human_decided(n["data"]):
            continue
        if n["type"] == "screen" and st.get(elig_id(n["id"].split(":", 1)[1])):
            continue   # screened on its full text: the full-text assessments decide
        d = dict(n["data"])
        dec, because, stage, by_rule = rederive(protocol, d, policy)
        if (dec, because, stage, by_rule) != (d["decision"], d.get("because"), d["stage"], d.get("unclear_by_rule")):
            d.pop("note", None)
            d["decision"], d["because"], d["stage"] = dec, because, stage
            if by_rule:
                d["unclear_by_rule"] = by_rule
            else:
                d.pop("unclear_by_rule", None)
            st.annotate(n["id"], d, actor="user", reason="protocol revised: decision re-derived from stored assessments")
            changed += 1
    changed += len(rules.apply(ctx, protocol))
    inc = set(st.included_ids())
    for kind in ("ext", "rob"):
        for n in st.by_type("extraction" if kind == "ext" else "rob"):
            rid = n["id"].split(":", 1)[1]
            want = "active" if rid in inc else "excluded"
            if n["status"] != want:
                st.set_status(n["id"], want, actor="user", reason="eligibility re-derived")
    ctx.save()
    ctx.log(f"[screen] rule-only re-screen: {changed} decisions changed; included now {len(inc)}")
    return {"changed": changed, "included": len(inc)}
