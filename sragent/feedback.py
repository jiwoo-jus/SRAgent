"""Human feedback -> verified, dependency-aware revision (Claim 2).

handle_feedback(ctx, text, target=..., verify=..., propagate=...)

  1. locate     which output the feedback is about (given, or LLM-located)
  2. verify     check the feedback against source evidence (can reject it)        [verify=True]
  3. revise     produce a corrected version of the target
  4. propagate  find related outputs via the dependency graph, ask which ones are
                affected, revise them, cascade eligibility changes (exclusion removes the
                study from synthesis; inclusion runs extraction + RoB + synthesis update) [propagate=True]
  5. propose    everything happens in a sandbox copy; the result is a change set that a human
                accepts (or auto_accept for experiments). Rejected / unresolved feedback stays
                visibly flagged on the node.

  6. check     every revised output is checked again by an independent verifier call (extracted values
                against the source, statements against the cited data); problems get one repair round
                (feedback.max_repair_rounds)                                         [both conditions]
  7. audit      a review-wide consistency audit catches statements outside the citation graph that now
                contradict the revised data (e.g. "all studies showed ..."), and revises them    [propagate=True]
Vague or multi-target feedback is split into one request per output first; feedback the agent cannot
interpret becomes a clarification question for the user (status needs_clarification).
Proposals remember the version of every output they change; accepting one that is out of date is refused.
The verifier role (roles.verifier) defaults to the agent model; set it to another model for an
independent check.

`propagate=False` is the LOCAL-EDIT BASELINE: only the directly referenced output is revised.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import re
import time

from . import prompts
from .context import Ctx
from .evidence import attach, cites_of, joined, packed, resolve
from .stages import extraction, fulltext, rob, screening, synthesis
from .stages.planning import criteria_text
from .store import study_of

TYPE_HINTS = {
    "screen": "The eligibility decision is derived by rule from the criterion assessments "
              "(exclude if an inclusion criterion is not_met or an exclusion criterion is met). "
              "To change the decision, change the relevant criterion assessment(s) and evidence ids.",
    "extraction": "Each field has a value and the ids of the numbered source sentences that support it.",
    "rob": "Each domain judgment needs a rationale and the ids of supporting source sentences.",
    "synthesis": "A synthesis statement: text, cited study ids, and scope.",
}
SKIP_TYPES = {"record", "fulltext", "search", "protocol", "criterion", "report"}


class StaleProposal(Exception):
    pass


def verifier(ctx: Ctx):
    """The model that checks feedback and revisions: roles.verifier if configured, else the agent."""
    return ctx.hub["verifier"] if (ctx.cfg.get("roles") or {}).get("verifier") else ctx.agent


def record_bases(live_store, cs: dict) -> None:
    ids = {c["node"] for c in cs["changes"]} | set(cs.get("sandbox_nodes") or {})
    cs["base_versions"] = {nid: (live_store.get(nid) or {}).get("version") for nid in sorted(ids) if live_store.get(nid)}


def sandbox_delta(live_store, sandbox_store, cs: dict) -> None:
    """Everything the sandbox did goes into the change set: the listed changes, and also the nodes it
    created or changed on the way (a retrieved full text, a retired statement, an excluded extraction)
    and the meta keys it touched. Without this, accepting a proposal applied only the listed nodes."""
    d = sandbox_store.diff(live_store)
    ids = {c["node"] for c in cs["changes"]} | set(d["new"]) | set(d["changed"])
    cs["sandbox_nodes"] = {nid: sandbox_store.get(nid) for nid in sorted(ids) if sandbox_store.get(nid)}
    cs["sandbox_meta"] = {k: copy.deepcopy(sandbox_store.meta.get(k)) for k in d["meta"]}


# ------------------------------------------------------------------ views
def editable(n: dict) -> dict:
    d, t = n["data"], n["type"]
    if t == "extraction":
        return {"fields": {k: {"value": v.get("value"), "evidence": _ev(v)} for k, v in d["fields"].items()}}
    if t == "screen":
        return {"criteria": [{"id": a.get("id"), "assessment": a.get("assessment"), "evidence": _ev(a)}
                             for a in d.get("criteria", [])],
                "decision": d.get("decision")}
    if t == "rob":
        return {"domains": [{"domain": x.get("domain"), "judgment": x.get("judgment"),
                             "rationale": x.get("rationale"), "evidence": _ev(x)} for x in d["domains"]],
                "overall": d.get("overall")}
    if t == "synthesis":
        return {k: d.get(k) for k in ("text", "cites", "scope")}
    return d


def _ev(x: dict) -> list:
    """Evidence ids of a stored claim."""
    return list(x.get("evidence") or [])


def summary_line(n: dict) -> str:
    d, t = n["data"], n["type"]
    if t == "synthesis":
        return d["text"][:160]
    if t == "screen":
        return f"eligibility decision={d['decision']} ({d['stage']})"
    if t == "extraction":
        return "extracted data: " + "; ".join(f"{k}={v['value'][:40]}" for k, v in list(d["fields"].items())[:4])
    if t == "rob":
        return f"risk of bias overall={d['overall']}"
    return t


def evidence_for(ctx: Ctx, n: dict, limit: int = 40000) -> str:
    sid = study_of(n["id"])
    if n["type"] == "synthesis":
        return synthesis.statement_evidence(ctx, n["data"]["cites"])
    if sid:
        text, kind = source_for(ctx, n)
        extra = ""
        if n["type"] == "screen":
            p = ctx.store.get("protocol")["data"]
            extra = "ELIGIBILITY CRITERIA:\n" + criteria_text(p) + "\n\n"
        return f"{extra}SOURCE ({kind}) for study {sid}, numbered sentences:\n{packed(text, limit)}"
    return ""


def source_for(ctx: Ctx, n: dict) -> tuple[str, str]:
    """The text whose sentence ids this node's evidence refers to."""
    sid = study_of(n["id"])
    if n["type"] == "screen":
        return screening.screen_source(ctx.store, sid, n["data"].get("stage", "full_text"))[0], n["data"].get("stage")
    return ctx.store.text_of(sid)


# ------------------------------------------------------------------ apply revised data
def apply_revised(ctx: Ctx, node_id: str, revised: dict, reason: str, actor: str = "agent") -> tuple[dict, dict]:
    """Write a revised editable view back to the store (with re-verification). Returns (before, after) data."""
    st = ctx.store
    n = st.get(node_id)
    before = copy.deepcopy(n["data"])
    t = n["type"]
    if t == "extraction":
        sid = study_of(node_id)
        text, _ = st.text_of(sid)
        src_node = f"ft:{sid}" if st.get(f"ft:{sid}") else f"rec:{sid}"
        fields, evidence = extraction.postprocess(ctx, sid, revised.get("fields", {}), text, src_node)
        data = {**n["data"], "fields": fields}
        st.upsert(node_id, t, data, depends_on=n["depends_on"], evidence=evidence, reason=reason, actor=actor)
    elif t == "screen":
        sid = study_of(node_id)
        p = st.get("protocol")["data"]
        crits = [a for a in (revised.get("criteria") or n["data"]["criteria"]) if isinstance(a, dict)]
        text = source_for(ctx, n)[0]
        for a in crits:
            cites = cites_of(a)
            a.pop("quote", None)
            if cites:
                attach(a, cites, text, "", "")
        crits = screening.settle(p, crits, n["data"].get("stage"))   # the full-text rule applies to revised assessments too
        decision, because, stage, by_rule = screening.rederive(p, {**n["data"], "criteria": crits},
                                                               ctx.cfg["pipeline"].get("fulltext_unclear", "await"))
        if decision == "include" and st.text_of(sid)[1] != "fulltext":
            decision, because = "fulltext_unavailable", []   # final inclusion only after a full-text review
        data = {**n["data"], "criteria": crits, "decision": decision, "because": because, "stage": stage}
        data.pop("unclear_by_rule", None)
        if by_rule and decision in ("include", "exclude"):
            data["unclear_by_rule"] = by_rule
        st.upsert(node_id, t, data, depends_on=n["depends_on"], reason=reason, actor=actor)
    elif t == "rob":
        sid = study_of(node_id)
        text, _ = st.text_of(sid)
        doms = [d for d in (revised.get("domains") or n["data"]["domains"]) if isinstance(d, dict)]
        for d in doms:
            cites = cites_of(d)
            d.pop("quote", None)
            attach(d, cites, text, "", "")
        data = {**n["data"], "domains": doms, "overall": revised.get("overall", n["data"]["overall"])}
        st.upsert(node_id, t, data, depends_on=n["depends_on"], reason=reason, actor=actor)
    elif t == "synthesis":
        had_cites = bool(n["data"].get("cites"))
        cites = [str(c).strip("[] ") for c in revised.get("cites", n["data"]["cites"])]
        cites, _ = synthesis.reconcile_citations(st, revised.get("text", n["data"]["text"]), cites)
        data = {**n["data"], "text": revised.get("text", n["data"]["text"]), "cites": cites,
                "scope": revised.get("scope", n["data"].get("scope", ""))}
        data.pop("verification", None)
        st.upsert(node_id, t, data, depends_on=synthesis.statement_deps(st, cites), reason=reason, actor=actor)
        synthesis.retire_if_uncited(st, node_id, had_cites)
    else:
        raise ValueError(f"cannot revise node type {t}")
    return before, st.get(node_id)["data"]


def _changed(before: dict, after: dict, t: str) -> bool:
    return json.dumps(editable({"type": t, "data": before}), sort_keys=True) != \
        json.dumps(editable({"type": t, "data": after}), sort_keys=True)


# ------------------------------------------------------------------ main entry
def handle_feedback(ctx: Ctx, text: str, target: str | None = None, pmid: str | None = None,
                    verify: bool | None = None, propagate: bool | None = None,
                    auto_accept: bool | None = None, case_id: str | None = None,
                    tag_prefix: str = "feedback") -> dict:
    fcfg = ctx.cfg["feedback"]
    verify = fcfg["verify_feedback"] if verify is None else verify
    propagate = fcfg["propagate"] if propagate is None else propagate
    auto_accept = fcfg["auto_accept"] if auto_accept is None else auto_accept
    t0 = time.time()
    usd0 = ctx.hub.tracker.spent
    eq0 = ctx.hub.tracker.spent_equiv
    cs = {"id": case_id or f"FB{int(t0 * 1000) % 10**9}", "feedback": text, "target": target, "pmid": pmid,
          "mode": {"verify": verify, "propagate": propagate}, "changes": [], "verdict": None,
          "explanation": "", "status": "proposed", "created": t0}

    # sandbox: all edits happen on a copy until accepted
    sb = dataclasses.replace(ctx, store=ctx.store.clone())

    requests_ = [{"target": target, "pmid": pmid, "feedback": text}]
    if not target and not pmid:
        requests_ = decompose(sb, text, tag=f"{tag_prefix}_locate")
        cs["located"] = requests_
        target = requests_[0].get("target") if len(requests_) == 1 else None
        cs["target"] = target
    for rq in requests_:
        if rq.get("pmid") or rq.get("target") == "NEW_PAPER":
            _add_paper(sb, cs, rq.get("pmid") or None, rq["feedback"], propagate, tag_prefix)
        elif rq.get("target"):
            sub = dict(cs, changes=[]) if len(requests_) > 1 else cs
            _revise_target(sb, sub, rq["target"], rq["feedback"], verify, propagate, tag_prefix)
            if sub is not cs:
                cs["changes"] += sub["changes"]
                cs.setdefault("sub_results", []).append({k: sub.get(k) for k in ("verdict", "explanation", "clarification_question")}
                                                        | {"target": rq["target"], "feedback": rq["feedback"]})
                cs["verdict"] = cs.get("verdict") or sub.get("verdict")
                if sub.get("clarification_question"):
                    cs["clarification_question"] = sub["clarification_question"]
                cs["explanation"] = " | ".join(x for x in (cs.get("explanation"), f"{rq['target']}: {sub.get('explanation', '')}") if x)
    if cs["changes"] and fcfg.get("post_check", True):        # both conditions: revision quality, not propagation
        _check_and_repair(sb, cs, tag_prefix, int(fcfg.get("max_repair_rounds", 1)))
    if cs["changes"] and propagate and fcfg.get("global_audit", True):
        _global_audit(sb, cs, tag_prefix)

    cs["cost_usd"] = round(ctx.hub.tracker.spent - usd0, 5)
    cs["cost_usd_uncached"] = round(ctx.hub.tracker.spent_equiv - eq0, 5)
    cs["seconds"] = round(time.time() - t0, 1)
    if cs["changes"]:
        sandbox_delta(ctx.store, sb.store, cs)
    if cs["changes"] and auto_accept:
        commit(ctx, cs["sandbox_nodes"], cs)
        cs.pop("sandbox_nodes", None)
        cs.pop("sandbox_meta", None)
    elif cs["changes"]:
        record_bases(ctx.store, cs)
        if target and ctx.store.get(target):
            ctx.store.flag(target, f"Feedback {cs['id']} pending review: {text[:200]}", actor="user")
    elif cs.get("clarification_question"):
        cs["status"] = "needs_clarification"
    elif _confirm_evidence(ctx, cs, target, text):
        pass   # a pointer to the sentences behind a flagged value: cited and closed, nothing to argue about
    else:
        cs["status"] = "rejected_by_agent" if cs["verdict"] in ("unsupported", "insufficient_evidence") else "no_change"
        if target and ctx.store.get(target):
            f = ctx.store.flag(target, f"Feedback {cs['id']}: {text[:200]} -> agent: {cs['verdict']}: "
                               f"{cs['explanation'][:200]}", actor="user")
            cs["flag_id"] = f["id"]  # stays open (visible) until a human resolves it
    _save_changeset(ctx, cs)
    ctx.save()
    return cs


EVIDENCE_FLAG_MARKS = ("Numbers not found", "found nowhere", "Numbers that are not in the cited sentences", "No source location cited")


def _confirm_evidence(ctx: Ctx, cs: dict, target: str | None, text: str) -> bool:
    """A reply to an evidence flag ("numbers not found as written", "no source location") that points to where the
    value is in the paper is not a change request: the sentences the check located are cited for the flagged items,
    the value stands, and the flag counts as answered. Returns True when that applied."""
    if not target or not target.startswith("ext:") or "(context:" not in text:
        return False
    ctxpart = text.split("(context:", 1)[1]
    if not any(m in ctxpart for m in EVIDENCE_FLAG_MARKS):
        return False
    ids = [sp.get("sid") for sp in (cs.get("evidence") or []) if sp.get("sid")]
    if not ids:
        return False
    st = ctx.store
    n = st.get(target)
    if not n:
        return False
    fields = n["data"].get("fields") or {}
    names = [x for x in re.findall(r"([a-z_]+): \d", ctxpart) + re.findall(r"cited for '([a-z_]+)'", ctxpart) if x in fields]
    names = list(dict.fromkeys(names))
    if not names:
        return False
    from .evidence import MAX_IDS, attach
    rid = study_of(target)
    full_text = st.text_of(rid)[0]
    src = f"ft:{rid}" if st.get(f"ft:{rid}") else f"rec:{rid}"
    data = copy.deepcopy(n["data"])
    evidence = [e for e in (n.get("evidence") or []) if not any(str(e.get("claim", "")).startswith(nm + "=") for nm in names)]
    for nm in names:
        item = data["fields"][nm]
        new_ids = list(dict.fromkeys(list(item.get("evidence") or []) + ids))[:MAX_IDS]
        item.pop("uncovered", None)
        item["confirmed_by"] = f"the review team (feedback {cs['id']})"
        evidence.append(attach(item, new_ids, full_text, f"{nm}={str(item.get('value', ''))[:80]}", src))
    st.upsert(target, "extraction", data, depends_on=n["depends_on"], evidence=evidence, actor="user",
              reason=f"feedback {cs['id']}: the review team pointed to the sentences ({', '.join(ids)}); cited for {', '.join(names)}")
    cs["status"] = "confirmed"
    cs["explanation"] = (f"The value stands. Sentences {', '.join(ids)} are now cited for {', '.join(names)}, as you pointed out. "
                         + (cs.get("explanation") or ""))
    return True


def _index(ctx: Ctx) -> str:
    return "\n".join(f"{n['id']}: {summary_line(n)}" for n in ctx.store.nodes.values()
                     if n["type"] not in SKIP_TYPES and n["status"] == "active")


def locate(ctx: Ctx, text: str, tag: str) -> dict:
    return ctx.agent.chat(ctx.msgs(prompts.LOCATE.format(feedback=text, index=_index(ctx))), tag=tag)


def decompose(ctx: Ctx, text: str, tag: str, max_n: int = 5) -> list[dict]:
    """One request per affected output (vague feedback like 're-check every population' -> several)."""
    out = ctx.agent.chat(ctx.msgs(prompts.DECOMPOSE.format(feedback=text, index=_index(ctx), max_n=max_n)), tag=tag)
    reqs = []
    for r in out.get("requests", [])[:max_n]:
        if not isinstance(r, dict):
            continue
        t = (r.get("target") or "").strip("[] ")
        if t == "NEW_PAPER" or ctx.store.get(t) or r.get("pmid"):
            reqs.append({"target": t, "pmid": (r.get("pmid") or "").strip() or None, "feedback": r.get("feedback") or text})
    if not reqs:   # fall back to the single-target locator
        loc = locate(ctx, text, tag)
        reqs = [{"target": loc.get("target"), "pmid": loc.get("pmid") or None, "feedback": text}]
    return reqs


def _revise_target(sb: Ctx, cs: dict, target: str, text: str, verify: bool, propagate: bool, tp: str):
    st = sb.store
    n = st.get(target)
    if n is None:
        cs["verdict"], cs["explanation"] = "insufficient_evidence", f"unknown target {target}"
        return
    node_json = json.dumps(editable(n), ensure_ascii=False, indent=1)
    ntype = f"{n['type']} — {TYPE_HINTS.get(n['type'], '')}"
    if verify:
        out = verifier(sb).chat(sb.msgs(prompts.FEEDBACK_VERIFY.format(
            feedback=text, node_id=target, ntype=ntype, node=node_json, evidence=evidence_for(sb, n))),
            tag=f"{tp}_verify")
        cs["verdict"] = out.get("verdict")
        cs["explanation"] = out.get("explanation", "")
        if out.get("verdict") == "unclear_request":
            cs["clarification_question"] = out.get("clarification_question") or "Could you say which value or statement should change, and to what?"
            return
        ev = resolve(out.get("evidence") or [], source_for(sb, n)[0]) \
            if study_of(target) else {"spans": []}
        cs["evidence"] = ev["spans"]
        cs["evidence_quote"] = joined(ev["spans"])
        revised = out.get("revised") if out.get("verdict") in ("supported", "partially_supported") else None
    else:
        out = sb.agent.chat(sb.msgs(prompts.FEEDBACK_APPLY.format(
            feedback=text, node_id=target, ntype=ntype, node=node_json)), tag=f"{tp}_apply")
        cs["verdict"] = "applied_without_verification"
        cs["explanation"] = out.get("explanation", "")
        revised = out.get("revised")
    if not revised or not isinstance(revised, dict):
        return
    before, after = apply_revised(sb, target, revised, reason=f"feedback {cs['id']}", actor="agent")
    if not _changed(before, after, n["type"]):
        return
    cs["changes"].append({"node": target, "role": "target", "before": before, "after": after})
    if propagate:
        _propagate(sb, cs, target, before, after, text, tp, depth=0, done={target})


def _propagate(sb: Ctx, cs: dict, src_id: str, before: dict, after: dict, reason: str, tp: str,
               depth: int, done: set):
    st = sb.store
    n = st.get(src_id)
    # --- eligibility flips have deterministic structural consequences
    if n["type"] == "screen" and before.get("decision") != after.get("decision"):
        rid = study_of(src_id)
        if after["decision"] == "exclude":
            for kind in ("ext", "rob"):
                nid = f"{kind}:{rid}"
                if st.get(nid) and st.get(nid)["status"] == "active":
                    st.set_status(nid, "excluded", reason=f"feedback {cs['id']}: study excluded")
                    cs["changes"].append({"node": nid, "role": "dependent", "before": {"status": "active"},
                                          "after": {"status": "excluded"}})
                    done.add(nid)
            # statements citing the study must drop it
            cands = [x["id"] for x in st.by_type("synthesis", active_only=True) if rid in x["data"]["cites"]]
            for sid in cands:
                _revise_dependent(sb, cs, sid, src_id, {"included": True, "study": rid},
                                  {"included": False, "study": rid}, tp)
                done.add(sid)
            return
        if after["decision"] == "include":
            _include_study(sb, cs, rid, tp)
            return
        if after["decision"] == "forward":
            # forwarded past title/abstract: its full-text review is done now, not left to the next run
            screening.review_one(sb, st.get("protocol")["data"], rid)
            screening.sync_awaiting_flags(sb)
            final = st.get(src_id)["data"]
            for c in cs["changes"]:
                if c["node"] == src_id and c.get("role") == "target":
                    c["after"] = copy.deepcopy(final)
                    c["explanation"] = (c.get("explanation", "") + f" Full-text review: {final['decision']}.").strip()
            if final["decision"] == "include":
                _include_study(sb, cs, rid, tp)
            return
    # everything downstream in the graph: the update engine (stale because src_id moved to a new version)
    from . import engine
    before_n = len(cs["changes"])
    engine.execute(sb, cs, roots={src_id}, tag=tp, render=False, max_rounds=2, policy=False)
    done.update(c["node"] for c in cs["changes"][before_n:])
    done.update(st.descendants(src_id))
    if depth >= 2:
        return
    # outputs of the same study that are not downstream (e.g. the eligibility decision after a corrected
    # population): the model decides whether the change reaches them
    cands = [c for c in st.related(src_id) if c not in done and st.get(c) and st.get(c)["status"] == "active"
             and st.get(c)["type"] not in SKIP_TYPES]
    if not cands:
        return
    cand_txt = "\n".join(f"[{c}] ({st.get(c)['type']}): {json.dumps(editable(st.get(c)), ensure_ascii=False)[:1500]}"
                         for c in cands)
    imp = sb.agent.chat(sb.msgs(prompts.IMPACT.format(
        node_id=src_id, before=json.dumps(editable({"type": n["type"], "data": before}), ensure_ascii=False)[:3000],
        after=json.dumps(editable({"type": n["type"], "data": after}), ensure_ascii=False)[:3000],
        reason=reason[:500], candidates=cand_txt)), tag=f"{tp}_impact")
    cs.setdefault("impact_assessments", []).append({"source": src_id, "updates": imp.get("updates", [])})
    for u in imp.get("updates", []):
        cid = u.get("id", "").strip("[]")
        if u.get("needs_update") and cid in cands and cid not in done:
            done.add(cid)
            res = _revise_dependent(sb, cs, cid, src_id, before, after, tp)
            if res:
                _propagate(sb, cs, cid, res[0], res[1], f"upstream change in {src_id}", tp, depth + 1, done)


def _check_one(sb: Ctx, node_id: str, before, tp: str) -> list[str]:
    """Independent re-check of a revised output. Returns the problems found (empty = fine)."""
    st = sb.store
    n = st.get(node_id)
    if not n or n["status"] != "active":
        return []
    issues = []
    if n["type"] == "extraction":
        from .stages.extraction import context_budget
        rid = study_of(node_id)
        text, kind = st.text_of(rid)
        old = (before or {}).get("fields", {}) if isinstance(before, dict) else {}
        for name, item in n["data"]["fields"].items():
            if item.get("value") == (old.get(name) or {}).get("value") or str(item.get("value", "")).upper().startswith("NR"):
                continue
            desc = next((f["description"] for f in sb.topic["extraction_fields"] if f["name"] == name), "")
            r = verifier(sb).chat(sb.msgs(prompts.SOURCE_CHECK.format(rid=rid, field=name, description=desc, value=item["value"],
                                                                     kind=kind, source=packed(text, context_budget(sb)))),
                                  tag=f"{tp}_check")
            if r.get("label") in ("unsupported", "partially_supported"):
                issues.append(f"{name}: {r.get('label')}: {r.get('rationale', '')}")
    elif n["type"] == "synthesis":
        d = n["data"]
        r = verifier(sb).chat(sb.msgs(prompts.SYNTH_VERIFY.format(sid=d["id"], text=d["text"],
                                                                 evidence=synthesis.statement_evidence(sb, d["cites"]))),
                              tag=f"{tp}_check")
        if r.get("support") in ("unsupported", "partially_supported") or r.get("overgeneralization"):
            issues.append(f"{r.get('support')}; overgeneralization={r.get('overgeneralization')}; {'; '.join(r.get('issues') or [])}")
    return issues


def _check_and_repair(sb: Ctx, cs: dict, tp: str, max_rounds: int = 1):
    log = cs.setdefault("post_checks", [])
    for c in cs["changes"]:
        if c.get("after") is None or not isinstance(c.get("after"), dict) or "status" in c["after"] and len(c["after"]) == 1:
            continue
        n = sb.store.get(c["node"])
        if not n or n["type"] not in ("extraction", "synthesis"):
            continue
        for rnd in range(max_rounds + 1):
            issues = _check_one(sb, c["node"], c.get("before"), tp)
            log.append({"node": c["node"], "round": rnd, "ok": not issues, "issues": issues[:4]})
            if not issues or rnd == max_rounds:
                break
            out = sb.agent.chat(sb.msgs(prompts.REPAIR.format(
                node_id=c["node"], ntype=f"{n['type']} — {TYPE_HINTS.get(n['type'], '')}",
                node=json.dumps(editable(sb.store.get(c["node"])), ensure_ascii=False, indent=1),
                issues="; ".join(issues), evidence=_context_for(sb, sb.store.get(c["node"])))), tag=f"{tp}_repair")
            if isinstance(out.get("revised"), dict):
                _, after = apply_revised(sb, c["node"], out["revised"], reason=f"feedback {cs['id']}: repaired after check")
                c["after"] = after
                c["repaired"] = rnd + 1


def _global_audit(sb: Ctx, cs: dict, tp: str, max_fix: int = 3):
    """Review-wide consistency audit after the change; repairs statements the graph did not reach."""
    from .eval.claim2 import compact_table
    st = sb.store
    stmts = st.by_type("synthesis", active_only=True)
    if not stmts:
        return
    inc = st.included_ids()
    changed = {c["node"] for c in cs["changes"]}
    stm = "\n".join(f"[{n['data']['id']}] {n['data']['text']} (cites: {', '.join(n['data']['cites'])})" for n in stmts)
    excl = sorted({c for n in stmts for c in n["data"]["cites"] if c not in inc})
    out = sb.agent.chat(sb.msgs(prompts.CONSISTENCY.format(table=compact_table(st, inc), excluded=", ".join(excl) or "(none)",
                                                           statements=stm)), tag=f"{tp}_audit")
    found = out.get("contradictions") or []
    cs["global_audit"] = {"found": len(found), "fixed": []}
    full = [c for c in cs["changes"] if isinstance(c.get("after"), dict) and not ("status" in c["after"] and len(c["after"]) == 1)]
    if not full:
        return
    src = next((c for c in full if c.get("role") == "target"), full[0])
    for con in found:
        for sid in con.get("ids") or []:
            nid = sid if str(sid).startswith("syn:") else f"syn:{str(sid).strip('[]')}"
            if nid in changed or not st.get(nid) or len(cs["global_audit"]["fixed"]) >= max_fix:
                continue
            src_t = (st.get(src["node"]) or {}).get("type", "screen")
            b = src["before"] if isinstance(src.get("before"), dict) else {}
            a = src["after"] if isinstance(src.get("after"), dict) else {}
            res = _revise_dependent(sb, cs, nid, src["node"], b, a, tp) if b and a and src_t != "record" else None
            changed.add(nid)
            cs["global_audit"]["fixed"].append({"node": nid, "issue": str(con.get("description", ""))[:200], "changed": bool(res)})


def _context_for(sb: Ctx, node: dict) -> str:
    sid = study_of(node["id"])
    if node["type"] == "synthesis":
        return extraction.table_text(sb, node["data"]["cites"], with_quotes=True) or "(none)"
    if sid:
        ext_txt = extraction.table_text(sb, [sid], with_quotes=True)
        return (ext_txt + "\n\n" + evidence_for(sb, node, limit=15000))[:20000]
    return ""


def _revise_dependent(sb: Ctx, cs: dict, node_id: str, src_id: str, before: dict, after: dict, tp: str):
    st = sb.store
    n = st.get(node_id)
    src_t = st.get(src_id)["type"] if st.get(src_id) else "screen"

    def view(x):   # a full node's editable view; a status-only or unknown "before" is shown as it is
        if not isinstance(x, dict) or "included" in x or not x or ("status" in x and len(x) == 1):
            return x if x else {"(not recorded)": True}
        try:
            return editable({"type": src_t, "data": x})
        except (KeyError, TypeError):
            return x
    b, a = view(before), view(after)
    out = sb.agent.chat(sb.msgs(prompts.REVISE_DEPENDENT.format(
        src_id=src_id, before=json.dumps(b, ensure_ascii=False)[:3000], after=json.dumps(a, ensure_ascii=False)[:3000],
        node_id=node_id, ntype=f"{n['type']} — {TYPE_HINTS.get(n['type'], '')}",
        node=json.dumps(editable(n), ensure_ascii=False, indent=1), context=_context_for(sb, n))),
        tag=f"{tp}_revise_dependent")
    rev = out.get("revised")
    if not isinstance(rev, dict):
        return None
    bd, ad = apply_revised(sb, node_id, rev, reason=f"feedback {cs['id']} (propagated from {src_id})")
    retired = st.get(node_id)["status"] == synthesis.RETIRED
    if _changed(bd, ad, n["type"]) or retired:
        cs["changes"].append({"node": node_id, "role": "dependent", "before": bd, "after": ad,
                              "explanation": out.get("explanation", "")
                              + (" Retired: no included study supports it any more." if retired else ""),
                              **({"status": synthesis.RETIRED} if retired else {})})
        return bd, ad
    return None


def _include_study(sb: Ctx, cs: dict, rid: str, tp: str, integrate: bool = True):
    st = sb.store
    if st.get(f"ft:{rid}") is None:
        fulltext.fetch_one(sb, rid)
    for kind, fn in (("ext", extraction.extract_one), ("rob", rob.rob_one)):
        nid = f"{kind}:{rid}"
        if st.get(nid) and st.get(nid)["status"] != "active":
            st.set_status(nid, "active", reason=f"feedback {cs['id']}: study re-included")
            cs["changes"].append({"node": nid, "role": "dependent", "before": {"status": "excluded"},
                                  "after": {"status": "active"}})
        elif st.get(nid) is None:
            fn(sb, rid)
            cs["changes"].append({"node": nid, "role": "dependent", "before": None, "after": st.get(nid)["data"]})
    if not integrate:
        return
    # dependency-aware: the graph remembers which statements cited this study before it was excluded (their earlier
    # versions) and which were retired for lack of it; those are revived and shown to the integrate step as they read
    previous = []
    for n in list(st.nodes.values()):
        if n["type"] != "synthesis" or n["status"] not in ("active", synthesis.RETIRED):
            continue
        if n["status"] == "active" and rid in (n["data"].get("cites") or []):
            continue
        then = next((h["data"] for h in reversed(n.get("history") or []) if rid in ((h.get("data") or {}).get("cites") or [])), None)
        if then is None and rid in (n["data"].get("cites") or []):
            then = n["data"]
        if then is None:
            continue
        previous.append(f"- {n['data'].get('id')} (then cited {then.get('cites')}): {then.get('text')}")
        if n["status"] == synthesis.RETIRED:
            st.set_status(n["id"], "active", reason=f"feedback {cs['id']}: study {rid} re-included; this statement cited it")
            cs["changes"].append({"node": n["id"], "role": "dependent", "before": {"status": synthesis.RETIRED}, "after": {"status": "active"}})
    cur = synthesis.statements_json(sb)
    out = sb.agent.chat(sb.msgs(prompts.SYNTH_INTEGRATE.format(
        rid=rid, new=extraction.table_text(sb, [rid], with_quotes=True), statements=cur,
        table=extraction.table_text(sb),
        previous=("\nSTATEMENTS THAT CITED THIS STUDY BEFORE IT WAS EXCLUDED (as they read then; restore the citation and "
                  "the claim its data support, if the data still support it):\n" + "\n".join(previous) + "\n") if previous else "")),
        tag=f"{tp}_integrate")
    before_nodes = {n["id"]: copy.deepcopy(n["data"]) for n in st.by_type("synthesis", active_only=True)}
    synthesis.write_statements(sb, out.get("statements", []), reason=f"feedback {cs['id']}: integrate {rid}")
    for n in st.by_type("synthesis", active_only=True):
        b = before_nodes.get(n["id"])
        if b is None or _changed(b, n["data"], "synthesis"):
            cs["changes"].append({"node": n["id"], "role": "dependent", "before": b, "after": n["data"]})


def _add_paper(sb: Ctx, cs: dict, pmid: str | None, text: str, propagate: bool, tp: str):
    from .sources import pubmed, synergy
    st = sb.store
    if not pmid:
        cs["verdict"], cs["explanation"] = "insufficient_evidence", "no PMID supplied for the paper to add"
        return
    rid = pmid
    if st.get(f"rec:{rid}") is None:
        recs = pubmed.efetch([pmid])
        if not recs:
            cs["verdict"], cs["explanation"] = "insufficient_evidence", f"PMID {pmid} not found in PubMed"
            return
        r = recs[0]
        r["rid"] = synergy.rid_for(r)
        rid = r["rid"]
        st.upsert(f"rec:{rid}", "record", r, depends_on=["search"], reason=f"feedback {cs['id']}: user-supplied")
        cs["changes"].append({"node": f"rec:{rid}", "role": "target", "before": None, "after": r})
    p = st.get("protocol")["data"]
    prev = copy.deepcopy(st.get(f"screen:{rid}")["data"]) if st.get(f"screen:{rid}") else None
    # the paper is assessed against the protocol: this is how unsupported "add" requests are caught.
    # Title/abstract first, then the full-text review (inclusion only on the full text).
    if prev is None:
        screening.screen_one(sb, p, rid, "title_abstract", tag_suffix="_feedback")
    if st.get(f"screen:{rid}")["data"]["decision"] != "exclude":
        screening.review_one(sb, p, rid)
    d = st.get(f"screen:{rid}")["data"]
    cs["changes"].append({"node": f"screen:{rid}", "role": "target", "before": prev, "after": d})
    if d["decision"] in screening.RETRIEVAL_FAILURES:
        cs["verdict"] = "insufficient_evidence"
        cs["explanation"] = f"Paper {rid}: full text not available ({d['decision']}); it cannot be included before a full-text review."
        return
    if d["decision"] == "awaiting":
        cs["verdict"] = "insufficient_evidence"
        cs["explanation"] = f"Paper {rid}: eligibility undecided after the full-text review ({d.get('because')})."
        return
    if d["decision"] == "exclude":
        cs["verdict"] = "unsupported"
        cs["explanation"] = f"The supplied paper fails eligibility criteria {d['because']}; not added to the synthesis."
        return
    cs["verdict"] = "supported"
    cs["explanation"] = f"Paper {rid} meets the eligibility criteria ({d['decision']})."
    _include_study(sb, cs, rid, tp, integrate=propagate)


# ------------------------------------------------------------------ accept / reject
def _save_changeset(ctx: Ctx, cs: dict):
    d = ctx.run_dir / "feedback"
    d.mkdir(exist_ok=True)
    (d / f"{cs['id']}.json").write_text(json.dumps(cs, indent=1, default=str))


def commit(ctx: Ctx, sandbox, cs: dict, actor: str = "user"):
    """Apply a change set to the live store. `sandbox` is the sandbox store, or the nodes saved with the
    proposal (cs["sandbox_nodes"]). Listed changes are applied as the person's acceptance; the other nodes
    the sandbox created or changed come along as dependent changes, with their flags and meta keys."""
    if hasattr(sandbox, "nodes"):
        sandbox_delta(ctx.store, sandbox, cs)
        nodes, meta = cs.pop("sandbox_nodes"), cs.pop("sandbox_meta")
    else:
        nodes, meta = sandbox, cs.get("sandbox_meta") or {}
    listed = {c["node"] for c in cs["changes"]}
    for nid in sorted(nodes, key=lambda x: (x not in listed, x)):   # listed changes first
        sn = nodes[nid]
        live = ctx.store.get(nid)
        why = f"accepted feedback {cs['id']}" + ("" if nid in listed else " (dependent change)")
        if live is None:
            ctx.store.put_node(copy.deepcopy(sn), actor=actor, reason=why)
            continue
        if any(live.get(k) != sn.get(k) for k in ("data", "status", "evidence", "depends_on")):
            ctx.store.upsert(nid, sn["type"], copy.deepcopy(sn["data"]), depends_on=sn["depends_on"],
                             evidence=copy.deepcopy(sn["evidence"]), status=sn["status"], actor=actor, reason=why)
        have = {(f.get("text"), f.get("source")) for f in live["flags"]}
        for f in sn.get("flags", []):              # flags raised in the sandbox (evidence checks, retirements)
            if (f.get("text"), f.get("source")) not in have:
                ctx.store.flag(nid, f["text"], actor=f.get("actor", "agent"), source=f.get("source", "feedback"))
    for k, v in meta.items():
        ctx.store.meta[k] = copy.deepcopy(v)
    tgt = cs.get("target")
    if tgt and ctx.store.get(tgt):
        for f in ctx.store.get(tgt)["flags"]:
            if f["status"] == "open" and cs["id"] in f["text"]:
                ctx.store.resolve_flag(tgt, f["id"], "accepted", actor=actor)
    cs["status"] = "accepted"
    cs["accepted"] = time.time()


def accept(ctx: Ctx, cs_id: str):
    p = ctx.run_dir / "feedback" / f"{cs_id}.json"
    cs = json.loads(p.read_text())
    if cs["status"] != "proposed":
        raise ValueError(f"change set {cs_id} is {cs['status']}")
    stale = [n for n, v in (cs.get("base_versions") or {}).items() if (ctx.store.get(n) or {}).get("version") != v]
    if stale:
        cs["status"] = "stale"
        cs["stale_nodes"] = stale
        _save_changeset(ctx, cs)
        for f in (ctx.store.get(cs.get("target") or "") or {}).get("flags", []):
            if f["status"] == "open" and cs_id in f["text"]:
                ctx.store.resolve_flag(cs["target"], f["id"], "proposal out of date", actor="agent")
        ctx.save()
        raise StaleProposal(f"Proposal {cs_id} is out of date: {', '.join(stale)} changed after it was made. "
                            f"Nothing was applied; ask the agent again.")
    commit(ctx, cs["sandbox_nodes"], cs)
    _save_changeset(ctx, cs)
    ctx.save()
    return cs


def reject(ctx: Ctx, cs_id: str, note: str = ""):
    p = ctx.run_dir / "feedback" / f"{cs_id}.json"
    cs = json.loads(p.read_text())
    cs["status"] = "rejected_by_user"
    cs["note"] = note
    tgt = cs.get("target")
    if tgt and ctx.store.get(tgt):
        for f in ctx.store.get(tgt)["flags"]:
            if f["status"] == "open" and cs_id in f["text"]:
                ctx.store.resolve_flag(tgt, f["id"], f"proposal rejected by user. {note}".strip(), actor="user")
        ctx.save()
    _save_changeset(ctx, cs)
    return cs
