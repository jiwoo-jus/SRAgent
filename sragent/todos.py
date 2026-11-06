"""The human to-do list: every decision the review needs from a person, asked simply.

Each to-do has a short question, a few option buttons, an optional free-text box and (sometimes) one
extra field. Answers are written to <run_dir>/human_decisions.jsonl (an audit trail and research data)
and applied to the review store. Answers that need the LLM (e.g. "ask the agent to fix this") return a
job for the UI to start.

To-do kinds and their step
  protocol      plan       approve the drafted protocol (or re-derive decisions after editing it)
  eligibility   screen     studies "awaiting classification" after full-text screening
  fulltexts     fulltext   one grouped item listing the PDFs that would help (expected file names)
  flag          by node    agent self-check / evidence / term-validation flags
  rob           rob        risk-of-bias drafts: a person decides the points neither agent settled; the rest optional
  changeset     feedback   accept or reject a proposed revision
  sync          synthesis  outputs that are out of date after human decisions
"""
from __future__ import annotations

import json
import time

from .context import Ctx
from .store import criteria_by_id, record_of
from .stages import fulltext

from . import selection, stagespec, state

# the To-do groups: one per stage (stagespec.STAGES), then feedback and revisions (across stages)
STEPS = [(s.key, f"{s.order}. {s.label}") for s in stagespec.STAGES] + \
        [(stagespec.FEEDBACK, f"{len(stagespec.STAGES) + 1}. Feedback & revisions")]
STEP_OF_TYPE = {"screen": "screen", "eligibility": "eligibility", "extraction": "extract", "rob": "rob", "synthesis": "synthesis"}


def _study_label(st, rid):
    r = record_of(st, rid)
    a = (r.get("authors") or "").split(",")[0]
    return f"{a} {r.get('year', '')}".strip() or rid


def _short(text: str, n: int = 6) -> str:
    import re
    w = re.sub(r"^(the )?(study|studies) (provides?|investigates? or administers|conducted)\s*", "", text.strip(), flags=re.I)
    w = w.split()
    return " ".join(w[:n]).rstrip(".,;") + ("…" if len(w) > n else "")


def build(ctx: Ctx) -> list[dict]:
    from . import provenance
    st = ctx.store
    todos: list[dict] = []
    if not st.get("protocol"):
        return todos
    crit = criteria_by_id(st)

    # -- protocol approval (after approval the card stays, collapsed, so the criteria can still be changed with
    #    the agent or by hand; a change makes the dependent decisions stale and asks for approval again)
    approved = bool(state.approval(st)) or provenance.mode(ctx) == "agent_only"
    if st.get("protocol"):
        todos.append({
            "id": "protocol", "step": "protocol", "kind": "protocol", "priority": 0,
            "approved": approved, "optional": approved,
            "title": "Approve the review protocol" if not approved else "Protocol (approved)",
            "question": "Do these eligibility criteria match what you want to review?" if not approved else
                        "The criteria the review follows. Change them here if needed: dependent decisions are then updated.",
            "context": [f"{c['id']} ({c['type']}, {c.get('stage', '')}): {c['text']}"
                        + (f" [any one of group {c['any_of']} suffices]" if c.get("any_of") else "") for c in crit.values()],
            "criteria": [{k: c.get(k) for k in ("id", "type", "text", "stage", "any_of") if c.get(k) is not None} for c in crit.values()],
            "revisions": (st.meta.get("protocol_revisions") or [])[-5:],
            "options": [] if approved else [
                        {"value": "approve", "label": "Yes, approve"},
                        {"value": "edited", "label": "I edited protocol.yaml, re-apply it",
                         "hint": f"File: {ctx.run_dir / 'protocol.yaml'}. Decisions are re-derived without LLM calls."}],
            "free_text": "Anything to note about the protocol? (optional)"})

    # -- search strategy: before the search exists, the team can see alternatives, ask for changes and confirm the query
    from . import provenance as _prov
    if _prov.mode(ctx) != "agent_only":
        m = state.strategy(st)
        sn = (st.get("search") or {}).get("data") or {}
        todos.append({
            "id": "search", "step": "search", "kind": "search", "priority": 0,
            "title": "Search strategy" if not sn else "Search strategy (ran)",
            "question": "Which PubMed query should the search run?",
            "variants": m.get("variants") or [], "chat": m.get("chat") or [], "confirmed": m.get("confirmed"),
            "draft": m.get("draft"), "required": bool(ctx.cfg["pipeline"].get("confirm_search")),
            "optional": bool(sn) or bool((m.get("confirmed") or {}).get("query")),   # shown, not waiting for anyone
            "ran": {"query": sn.get("query"), "hits": sn.get("hits"), "retrieved": len(sn.get("retrieved_pmids") or []),
                    "records": sn.get("n_identified"), "duplicates": sn.get("n_duplicates"), "source": sn.get("query_source"),
                    "database": sn.get("database")} if sn else None,
            "maxdate": (ctx.topic.get("records") or {}).get("maxdate"),
            "retmax": (ctx.topic.get("records") or {}).get("pubmed_retmax"),
            "options": [], "context": []})

    # -- data items and quality domains: changeable at any stage (a review-defining setting, accepted when saved;
    #    new items are then extracted for every included study by the update engine)
    tp = ctx.topic or {}
    inc_ids = st.included_ids()
    todos.append({
        "id": "fields", "step": "extract", "kind": "fields", "priority": 9, "optional": True,
        "title": "Data items and quality domains",
        "question": "What the agent extracts from every included study, and the quality domains it judges.",
        "fields": [{k: f.get(k) for k in ("name", "kind", "description") if f.get(k)} for f in tp.get("extraction_fields") or []],
        "domains": [{k: d.get(k) for k in ("name", "description") if d.get(k)} for d in tp.get("rob_domains") or []],
        "tool": tp.get("rob_tool_name", ""), "n_included": len(inc_ids),
        "n_extracted": sum(1 for r in inc_ids if st.get(f"ext:{r}")),
        "options": [], "context": []})

    # -- eligibility: awaiting classification (shown as one table in the UI)
    wanted_pdf = state.wanted(st)
    for rid, d in selection.screened(st):
        if d["decision"] != "awaiting" or d.get("human_decision"):
            continue
        if rid in wanted_pdf and ((st.get(f"ft:{rid}") or {}).get("data", {}).get("kind") != "fulltext"):
            continue  # waiting for the PDF; listed under Full texts
        r = record_of(st, rid)
        def _bad(a):
            c = crit.get(a.get("id"))
            if not c or c["type"] == "practical":
                return False
            v = a.get("assessment")
            return v == "unclear" or (c["type"] == "inclusion" and v == "not_met") or (c["type"] == "exclusion" and v == "met")
        unclear = [a for a in d.get("criteria", []) if _bad(a)]
        why = "; ".join(f"{a['id']} ({_short(crit[a['id']]['text'])})" + (f": {a['reason']}" if a.get("reason") else "")
                        for a in unclear[:4])
        not_met = [a["id"] for a in unclear if (crit[a["id"]]["type"] == "inclusion" and a.get("assessment") == "not_met")
                   or (crit[a["id"]]["type"] == "exclusion" and a.get("assessment") == "met")]
        has_abs = bool(r.get("abstract"))
        read_full = d.get("source_kind") == "fulltext" or d.get("stage") == "full_text"
        rv = d.get("eligibility_review") or {}
        sv = d.get("screen_verify") or {}
        if sv.get("outcome") == "pending_disagreement":
            why = f"screener says {'exclude' if d.get('because') else 'include'}, the verifier says {sv.get('decision')}"
            suggest = {"option": "include" if sv.get("decision") == "include" else "exclude",
                       "criterion": (d.get("because") or [None])[0], "why": "the two agents disagree after full-text screening; read both and decide"}
        elif not_met:
            suggest = {"option": "exclude", "criterion": not_met[0], "why": f"agent: {not_met[0]} fails"}
        elif read_full and not rv and not sv:   # the paper was read in full and is still unclear: a judgment, not a missing file
            lean = d.get("llm_decision")
            if lean == "exclude":
                first = next((a["id"] for a in unclear), "other")
                suggest = {"option": "exclude", "criterion": first, "why": "read in full; the agent's overall reading leans exclude"}
            else:
                suggest = {"option": "include", "why": "read in full; still unclear on these criteria" + (" (the agent leans include)" if lean == "include" else "")}
        else:
            suggest = {"option": "pdf", "why": "needs the full text to decide" if has_abs else "no abstract; needs the full text"}
        todos.append({
            "id": f"elig:{rid}", "step": "eligibility", "kind": "eligibility", "priority": 1, "rid": rid,
            "title": (r.get("title") or "(no title)")[:200],
            "meta": f"{_study_label(st, rid)} · {r.get('journal', '')} {r.get('year', '')} · "
                    f"{', '.join(r.get('pub_types') or [])}",
            "pmid": r.get("pmid", ""), "doi": r.get("doi", ""), "has_abstract": has_abs,
            "abstract": (r.get("abstract") or "")[:1200], "why": ("Read in full; still unclear: " if read_full else "") + (why or "criteria unclear"),
            "agent_reason": d.get("reason") or "", "review": d.get("eligibility_review"), "verify": d.get("screen_verify"),
            "suggest": suggest, "criteria": [c for c in crit if crit[c]["type"] != "practical"],
            "question": "Should this study be included?",
            "options": [{"value": "include", "label": "Include"}, {"value": "exclude", "label": "Exclude"},
                        {"value": "pdf", "label": "Need full text"}],
            "link": f"/report#screen:{rid}"})

    # -- full texts (grouped)
    unavailable = state.unavailable(st)
    wanted = [x for x in fulltext.needed_pdfs(ctx) if x["rid"] not in unavailable]   # blocked or asked for
    if wanted:
        present = [x for x in wanted if x["present"]]
        todos.append({
            "id": "fulltexts", "step": "retrieve", "kind": "fulltexts", "priority": 2,
            "title": f"Full texts that would help ({len(wanted)}; {len(present)} already added)",
            "question": "Add the PDFs you can get, then process them.",
            "context": [f"Folder: {fulltext.fulltext_dir(ctx)}  (list also in needed_pdfs.csv)"] +
                       [f"{'✓' if x['present'] else '·'} {x['filename']}  |  {x['title'][:110]}  |  "
                        f"{'PMID ' + x['pmid'] if x['pmid'] else ''} {('doi:' + x['doi']) if x['doi'] else ''}  |  {x['why']}"
                        for x in wanted],
            "options": [{"value": "process", "label": "I added files, process them",
                         "hint": "Converts PDFs, re-screens and re-extracts those studies (LLM calls; uses the Run settings)."},
                        {"value": "later", "label": "Later"}],
            "extra": {"name": "unavailable", "label": "Not obtainable (record ids, comma separated)", "show_if": "*",
                      "text": True},
            "free_text": None, "files": wanted,
            "pmids": [x["pmid"] for x in wanted if x["pmid"] and not x["present"]],
            "dois": [x["doi"] for x in wanted if x["doi"] and not x["pmid"] and not x["present"]]})

    # -- agent flags
    inc_now = set(st.included_ids())
    for n in st.nodes.values():
        if n["type"] in ("extraction", "rob") and n["id"].split(":", 1)[1] not in inc_now:
            continue
        for f in n["flags"]:
            if f["status"] != "open" or f.get("source") not in ("synthesis_verify", "term_validation", "evidence_check", "eligibility_doubt"):
                continue
            step = STEP_OF_TYPE.get(n["type"], "feedback")
            what = n["data"].get("text") if n["type"] == "synthesis" else ""
            overview = None
            if f["source"] == "synthesis_verify":
                q = f"The self-check thinks statement {n['data'].get('id')} may misstate or overstate the evidence. What should happen?"
                opts = [{"value": "fix", "label": "Ask the agent to revise it",
                         "hint": "The agent rewrites the statement against the studies it cites, with your comment. The revision comes "
                                 "back here as a proposal to accept or reject. One model call."},
                        {"value": "ok", "label": "It is fine, keep it",
                         "hint": "The statement stays as written; the self-check's concern is closed and your comment recorded."}]
            elif f["source"] == "eligibility_doubt":
                q, overview, opts = _doubt_item(st, n)
            elif f["source"] == "term_validation":
                q = "A term was not found in RxNorm / PubChem / ChEMBL / MONDO. Is the extracted value right?"
                opts = [{"value": "ok", "label": "It is fine (e.g. a drug class)",
                         "hint": "The value stays as extracted; the term is recorded as checked by you."},
                        {"value": "fix", "label": "Correct it (describe below)",
                         "hint": "The agent re-checks the value with your description and proposes the correction for you to accept. "
                                 "One model call."}]
            else:
                q = "This value has no source location. Is it right?"
                opts = [{"value": "ok", "label": "It is right",
                         "hint": "The value stays as extracted, recorded as checked by you."},
                        {"value": "fix", "label": "Ask the agent to re-check it",
                         "hint": "The agent looks for the sentence that supports the value and proposes a correction or its "
                                 "source for you to accept. One model call."}]
            rid = n["id"].split(":", 1)[1] if ":" in n["id"] else ""
            label = {"synthesis_verify": f"Statement {n['data'].get('id', '')} may overstate the evidence",
                     "term_validation": f"Unrecognised term in study {rid}",
                     "evidence_check": f"Value without a source location in study {rid}",
                     "eligibility_doubt": f"Eligibility doubt for study {rid}"}[f["source"]]
            todos.append({
                "id": f"flag:{n['id']}:{f['id']}", "step": step, "kind": "flag", "priority": 3,
                "title": label,
                "question": q, "context": [] if overview else [x for x in [what, f["text"]] if x], "overview": overview,
                "options": opts, "free_text": "Your comment (kept with your answer; the agent gets it when it is asked to act)",
                "link": f"/report#{n['id'] if n['type'] in ('synthesis', 'screen') else 'ext:' + rid}"})

    # -- manual edits whose dependents are out of date with them (derived from the graph: attention.py)
    from .attention import edit_state
    for n in st.by_type("extraction", active_only=True) + st.by_type("rob", active_only=True):
        e = edit_state(st, n)
        if not e:
            continue
        rid = n["id"].split(":", 1)[1]
        todos.append({
                "id": f"edited:{n['id']}", "step": STEP_OF_TYPE.get(n["type"], "feedback"), "kind": "manual_edit",
                "priority": 2, "title": f"You edited {n['id']} ({_study_label(st, rid)})",
                "question": "Other outputs use this one. Should the agent update them?",
                "context": ["You edited this output.", "Depends on it: " + ", ".join(e["dependents"])],
                "options": [{"value": "update", "label": "Yes, propose updates",
                             "hint": "The agent revises only the dependent outputs (a cent or two). You approve the result here."},
                            {"value": "ok", "label": "No, they are fine"}],
                "free_text": "Note for the agent (optional)", "link": f"/report#{n['id']}"})

    # -- feedback the agent disagreed with
    for n in st.nodes.values():
        for f in n["flags"]:
            if f["status"] != "open" or f.get("source") != "feedback" or "-> agent:" not in f["text"]:
                continue
            yours, agent = f["text"].split("-> agent:", 1)
            todos.append({
                "id": f"disagree:{n['id']}:{f['id']}", "step": "feedback", "kind": "disagreement", "priority": 0,
                "title": f"The agent did not apply your feedback on {n['id']}",
                "question": "The agent checked the source and disagreed. What now?",
                "context": [f"You: {yours.split(':', 1)[-1].strip()}", f"Agent: {agent.strip()}"],
                "options": [{"value": "agree", "label": "The agent is right, close it"},
                            {"value": "insist", "label": "Apply my change anyway",
                             "hint": "The agent proposes the change without its evidence check; you approve it here."}],
                "free_text": "Add detail or a corrected value (optional)",
                "link": f"/report#{n['id'] if n['type'] == 'synthesis' else n['id'].replace('rob:', 'rob:')}"})

    # -- risk of bias: a draft needs a person only with a reason the verifier did not settle; the rest are optional
    from .stages.rob import needs_person, reasons_of
    included = set(st.included_ids())
    for n in st.by_type("rob", active_only=True):
        if n["data"].get("human_confirmed"):
            continue
        rid = n["id"].split(":", 1)[1]
        if rid not in included:
            continue
        doms = [d.get("domain") for d in n["data"]["domains"]]
        rv = n["data"].get("rob_verify") or {}
        todos.append({
            "id": f"rob:{rid}", "step": "rob", "kind": "rob", "priority": 4,
            "optional": not needs_person(n["data"]),
            "reasons": [r["text"] for r in (n["data"].get("reasons") if "reasons" in n["data"] else reasons_of(n["data"]))],
            "verify": {k: rv.get(k) for k in ("by", "outcome", "explanation", "changed", "domains")} if rv else None,
            "title": f"{_study_label(st, rid)} [{rid}]: overall {n['data']['overall']}",
            "question": "Is this risk-of-bias draft right?",
            "context": [f"{d.get('domain')}: {d.get('judgment')}  ({(d.get('rationale') or '')[:160]})" for d in n["data"]["domains"]],
            "options": [{"value": "confirm", "label": "Confirm"}, {"value": "change", "label": "Change one domain"},
                        {"value": "fix", "label": "Ask the agent to re-assess (comment below)"}],
            "extra": {"name": "domain_change", "label": "Domain and new judgment", "show_if": "change",
                      "choices": doms, "choices2": ["low", "some_concerns", "high", "unclear"]},
            "free_text": "Comment (optional)", "bulk": "rob_confirm", "rid": rid, "overall": n["data"]["overall"],
            "domains": [{"domain": d.get("domain"), "judgment": d.get("judgment"),
                         "rationale": (d.get("rationale") or "")[:300]} for d in n["data"]["domains"]],
            "link": f"/report#rob:{rid}"})

    # -- pending change sets
    fdir = ctx.run_dir / "feedback"
    if fdir.exists():
        for p in sorted(fdir.glob("*.json")):
            try:
                cs = json.loads(p.read_text())
            except ValueError:
                continue
            if cs.get("status") == "needs_clarification":
                todos.append({
                    "id": f"clarify:{cs['id']}", "step": "feedback", "kind": "clarify", "priority": 0, "target": cs.get("target"),
                    "title": "The agent needs a clarification about your feedback",
                    "question": cs.get("clarification_question") or "What exactly should change?",
                    "context": [f"Your feedback: {cs['feedback']}"],
                    "options": [{"value": "answer", "label": "Send my answer"}, {"value": "drop", "label": "Withdraw the feedback"}],
                    "free_text": "Your answer"})
                continue
            if cs.get("status") != "proposed":
                continue
            lines = [f"Your feedback: {cs['feedback']}", f"Agent's check: {cs.get('verdict')} · {cs.get('explanation', '')[:300]}"]
            for c in cs["changes"][:8]:
                lines.append(f"{c['role']}: {c['node']}  {_short_change(c)}")
            what = cs.get("label") or "Proposed revision for your feedback"
            if cs.get("kind") == "rerun":
                lines[0] = f"You asked: {cs['feedback']}"
                lines[1] = f"Model: {cs.get('model', '')} · {cs.get('explanation', '')}"
            elif cs.get("kind") == "propagate":
                lines[0] = f"You asked: {cs['feedback']}"
                lines[1] = cs.get("explanation", "")
            todos.append({
                "id": f"cs:{cs['id']}", "step": "feedback", "kind": "changeset", "priority": 0,
                "target": cs.get("target") or next((c["node"] for c in cs.get("changes", []) if c.get("role") == "target"), None),
                "title": f"{what} ({len(cs['changes'])} output{'s' if len(cs['changes']) != 1 else ''})",
                "question": "Apply these changes?",
                "context": lines,
                "options": [{"value": "accept", "label": "Accept"}, {"value": "reject", "label": "Reject"}],
                "free_text": "Note (optional)"})

    # -- out-of-date outputs
    from .sync import pending_changes
    pc = pending_changes(ctx)
    stale = pc.get("stale") or {}
    if st.by_type("extraction") and stale.get("n"):
        lines = []
        if pc["need_extract"]:
            lines.append(f"Included but not yet extracted / assessed: {', '.join(pc['need_extract'])}")
        if pc["new_for_synthesis"]:
            lines.append(f"Included but not yet considered in the synthesis: {', '.join(pc['new_for_synthesis'])}")
        if pc["excluded_but_cited"]:
            lines.append(f"Excluded but still cited by the synthesis: {', '.join(pc['excluded_but_cited'])}")
        lines.append("Planned: " + ", ".join(f"{k.replace(':', ' ')} ×{v}" for k, v in stale["summary"].items()))
        if stale.get("kept"):
            lines.append("Kept as you decided or edited them: " + ", ".join(stale["kept"][:8]))
        cost = f" (about ${stale['est_usd']:.2f})" if stale.get("est_usd") is not None else ""
        todos.append({
            "id": "sync", "step": "synthesis", "kind": "sync", "priority": 1,
            "title": "Some outputs are out of date after your decisions or a protocol change",
            "question": f"Update them now?{cost}",
            "context": lines,
            "options": [{"value": "sync", "label": "Update now",
                         "hint": "Re-assesses screening on the changed criteria only, extracts and assesses new studies, "
                                 "and revises only the affected synthesis statements. Outputs you decided or edited are kept."},
                        {"value": "later", "label": "Later"}],
            "free_text": None})

    from . import provenance
    if provenance.mode(ctx) == "agent_only":
        # people may only supply the full texts the agent asked for
        todos = [t for t in todos if t["kind"] == "fulltexts"]
    order = {s: i for i, (s, _) in enumerate(STEPS)}
    todos.sort(key=lambda t: (order.get(t["step"], 99), t["priority"], t["id"]))
    snoozed = set(st.meta.get("snoozed", []))
    for t in todos:
        t["snoozed"] = t["id"] in snoozed
    return todos


def _short_change(c: dict) -> str:
    b, a = c.get("before"), c.get("after")
    if isinstance(a, dict) and "text" in a:
        if isinstance(b, dict) and b.get("text"):
            return f"BEFORE “{b['text'][:400]}”  AFTER “{a['text'][:400]}”"
        return f"NEW “{a['text'][:400]}”"
    if isinstance(a, dict) and isinstance(b, dict) and "fields" in a and "fields" in b:
        ch = [f"{k}: “{b['fields'].get(k, {}).get('value', '')[:200]}” → “{v.get('value', '')[:200]}”"
              for k, v in a["fields"].items() if v.get("value") != b["fields"].get(k, {}).get("value")]
        return " ; ".join(ch) or "evidence locations updated"
    if isinstance(a, dict) and isinstance(b, dict) and "domains" in a and "domains" in b:
        bd = {x.get("domain"): x.get("judgment") for x in b["domains"]}
        ch = [f"{x.get('domain')}: {bd.get(x.get('domain'))} → {x.get('judgment')}" for x in a["domains"]
              if bd.get(x.get("domain")) != x.get("judgment")]
        return " ; ".join(ch) or "rationale/evidence updated"
    if isinstance(a, dict) and "decision" in a:
        return f"decision → {a['decision']}"
    if isinstance(a, dict) and "status" in a and len(a) == 1:
        return f"status → {a['status']}"
    return "updated"


# ------------------------------------------------------------------ answers
def log_answer(ctx: Ctx, answer: dict, status: str):
    """A line in human_decisions.jsonl for an answer that is not an operation (kept for answers queued during a job)."""
    with open(ctx.run_dir / "human_decisions.jsonl", "a") as f:
        f.write(json.dumps({**answer, "status": status, "ts": time.time()}) + "\n")


def _doubt_item(st, n: dict) -> tuple[str, list[dict], list[dict]]:
    """An eligibility doubt that reached a person: both agents say the study fails a criterion and the person included
    it. The question, who said what (the overview), and the three answers, each saying what it does."""
    from .store import criteria_by_id
    rid = n["id"].split(":", 1)[1]
    d = n["data"]
    rv = d.get("eligibility_review") or {}
    claim, ver = rv.get("claim") or {}, rv.get("verifier") or {}
    cid = claim.get("criterion") or ""
    c = criteria_by_id(st).get(cid) or {}
    ctext = c.get("text", "")
    hd = d.get("human_decision") or {}
    cites = [s["data"].get("id") or s["id"] for s in st.by_type("synthesis", active_only=True) if rid in s["data"].get("cites", [])]
    fails = ("meets exclusion criterion " if c.get("type") == "exclusion" else "fails inclusion criterion ") + cid + \
        (f" ({ctext})" if ctext else "")
    q = (f"Both agents say this study {fails}. You included it. What should happen?" if ver.get("verdict") == "exclude" else
         f"Data extraction says this study {fails}; the verifier finds it eligible, as you did. What should happen?")
    overview = [{"who": "You", "says": "include", "why": hd.get("note") or hd.get("reason") or ""},
                {"who": "Data extraction (read the whole paper)", "says": "exclude", "why": claim.get("why", "")},
                {"who": f"Verifier ({ver.get('by', '')})", "says": ver.get("verdict", ""), "why": ver.get("explanation", ""),
                 "quote": (ver.get("quote") or "")[:500]}]
    opts = [{"value": "ok", "label": "Keep it included",
             "hint": "Your decision stands. The question is closed and your comment is recorded with it. Nothing else changes."},
            {"value": f"exclude:{cid}", "label": f"Exclude it ({cid})",
             "hint": "The study leaves the review: its data extraction and risk of bias are set aside"
                     + (f", and {len(cites)} synthesis statement(s) citing it ({', '.join(cites[:4])}) are revised" if cites else
                        "; no synthesis statement cites it") + ". You see every change before it is applied."},
            {"value": "rescreen", "label": "Screen it again on the full text",
             "hint": "The full-text screener reads the paper again, with both agents' reasons and your comment. Its answer comes "
                     "back here as a proposal; your inclusion stands until you accept it. One model call."}]
    return q, overview, opts


def to_operation(answer: dict) -> tuple[str, dict] | None:
    """The operation a To-do answer {todo_id, option, text, extra} stands for (sragent/ops), with its arguments;
    None for an answer that only starts a job."""
    tid, opt = answer["todo_id"], answer.get("option")
    text = (answer.get("text") or "").strip()
    extra = answer.get("extra") or {}
    if opt == "later":
        return "snooze", {"item": tid}
    if tid == "protocol":
        return {"approve": ("protocol.approve", {"note": text}), "edited": ("protocol.import_file", {"note": text})}.get(opt)
    if tid.startswith("elig:"):
        rid = tid.split(":", 1)[1]
        if opt == "pdf":
            return "retrieve.request", {"rid": rid, "note": text}
        return "eligibility.decide", {"rid": rid, "decision": opt, "criterion": extra.get("criterion"), "note": text}
    if tid == "fulltexts":
        ids = [x.strip() for x in (extra.get("unavailable") or "").replace(";", ",").split(",") if x.strip()]
        if ids:
            return "retrieve.declare_unobtainable", {"rids": ids}
        return ("retrieve.process_added_files", {}) if opt == "process" else None
    if tid == "sync" and opt == "sync":
        return "update_all", {}
    if tid.startswith("flag:"):
        node, fid = tid[5:].rsplit(":", 1)        # node ids contain a colon themselves (syn:S4)
        if opt and opt.startswith("exclude:"):   # an eligibility doubt answered by excluding the study
            return "eligibility.decide", {"rid": node.split(":", 1)[1], "decision": "exclude", "criterion": opt.split(":", 1)[1], "note": text}
        return "flag.answer", {"node": node, "flag": fid, "option": opt, "text": text}
    if tid.startswith("clarify:"):
        return "feedback.clarify", {"changeset": tid.split(":", 1)[1], "option": opt, "text": text}
    if tid.startswith("edited:"):
        node = tid[len("edited:"):]
        return ("edit.dependents_fine", {"node": node, "note": text}) if opt == "ok" else \
            ("edit.update_dependents", {"node": node, "note": text}) if opt == "update" else None
    if tid.startswith("disagree:"):
        node, fid = tid[len("disagree:"):].rsplit(":", 1)
        return "feedback.disagreement", {"node": node, "flag": fid, "option": opt, "text": text}
    if tid.startswith("rob:"):
        rid = tid.split(":", 1)[1]
        if opt == "confirm":
            return "rob.confirm", {"rids": [rid], "note": text}
        if opt == "change":
            ch = extra.get("changes")
            if ch is None:
                ch = [{"domain": extra.get("domain"), "judgment": extra.get("judgment")}]
            return "rob.edit_domains", {"rid": rid, "changes": ch, "note": text}
        if opt == "fix":   # the same operation as the study page's re-run (03 I06)
            return "rob.recompute", {"rid": rid, "note": text}
        return None
    if tid.startswith("cs:"):
        cid = tid.split(":", 1)[1]
        return ("proposal.accept", {"changeset": cid, "note": text}) if opt == "accept" else ("proposal.reject", {"changeset": cid, "note": text})
    return None


def apply(ctx: Ctx, answer: dict) -> dict:
    """Apply one To-do answer {todo_id, option, text, extra}: the operation it stands for (sragent/ops); a model
    operation is not run here but returned as the job that runs it. Returns {"ok", "message", "job"?}."""
    from . import ops
    op = to_operation(answer)
    if op is None:
        return {"ok": False, "message": f"unknown to-do answer {answer.get('todo_id')} -> {answer.get('option')}"}
    name, args = op
    if ops.get(name).kind == "llm":
        from .ops import _log_decision
        res = {"ok": True, "message": f"{name} queued", "job": ops.submit(name, args)}
        _log_decision(ctx, f"job.{name}", {"target": args.get("node") or args.get("rid") or answer["todo_id"],
                                           "note": answer.get("text") or ""}, res)
        return res
    res = ops.run(ctx, name, args)
    job = res.get("job")
    if res.get("ok") and answer["todo_id"] == "fulltexts" and answer.get("option") == "process":
        job = ops.submit("retrieve.process_added_files", {})   # declared the missing ones; now read the added files
    return {"ok": res.get("ok", False), "message": res.get("message", ""), "job": job}


def bulk(ctx: Ctx, kind: str, ids: list[str], note: str = "", items: list[dict] | None = None) -> int:
    """Several answers at once; returns how many were applied (a refused one is not counted)."""
    from . import ops
    if kind == "eligibility":
        return sum(1 for it in items or [] if apply(ctx, {"todo_id": f"elig:{it['rid']}", "option": it["option"],
                                                          "text": it.get("text", ""),
                                                          "extra": {"criterion": it.get("criterion")}})["ok"])
    if kind == "restore_rule":
        return ops.run(ctx, "screen.restore_rule_exclusion", {"rids": ids, "note": note}).get("count", 0)
    if kind == "pdf_unavailable":
        return ops.run(ctx, "retrieve.declare_unobtainable", {"rids": ids, "note": note}).get("count", 0)
    if kind == "pdf_unwant":
        return ops.run(ctx, "retrieve.decide_without", {"rids": ids, "note": note}).get("count", 0)
    if kind == "rob_confirm":
        return ops.run(ctx, "rob.confirm", {"rids": [t.split(":", 1)[1] for t in ids if t.startswith("rob:")],
                                            "note": note or "bulk confirm"}).get("count", 0)
    raise ValueError(f"unknown bulk kind {kind}")


def rule_exclusions(ctx: Ctx) -> list[dict]:
    st = ctx.store
    out = []
    for rid, d in selection.screened(st):
        if d.get("rule") and d["decision"] == "exclude":
            r = record_of(st, rid)
            out.append({"rid": rid, "rule": d["rule"], "reason": d.get("rule_reason", ""),
                        "title": (r.get("title") or "(no title)")[:160], "pmid": r.get("pmid", ""), "doi": r.get("doi", "")})
    return sorted(out, key=lambda x: (x["rule"], x["rid"]))


def next_action(ctx: Ctx, todos: list[dict], job_running: bool, new_files: int) -> dict:
    """The single most useful thing to do now, in plain words."""
    st = ctx.store
    open_t = [t for t in todos if not t.get("snoozed") and not t.get("optional")]   # optional: shown, not waiting
    by = {}
    for t in open_t:
        by.setdefault(t["kind"], []).append(t)
    if job_running:
        return {"text": "A job is running. You can keep answering; answers are applied when it finishes.", "go": "run"}
    from . import provenance
    if provenance.mode(ctx) == "agent_only" and st.get("protocol"):
        if new_files:
            return {"text": f"Process {new_files} added full-text file{'s' if new_files > 1 else ''}.", "go": "pdfs"}
        if fulltext.waiting_for_pdfs(ctx):
            return {"text": f"The synthesis waits for {fulltext.waiting_for_pdfs(ctx)} requested full text(s) (pipeline.wait_for_pdfs): "
                            "add them or mark them not obtainable, then run again.", "go": "pdfs"}
        if "fulltexts" in by:
            return {"text": "Agent-only mode: the agent decides everything. You can only supply the full texts it requested.",
                    "go": "pdfs"}
        if not st.by_type("synthesis"):
            return {"text": "Agent-only mode: run the review; the agent makes every decision.", "go": "run", "until": "report"}
        return {"text": "Agent-only mode: the review is complete. Read the report.", "go": "report"}
    if not st.get("protocol"):
        return {"text": "Start the review: draft the protocol (one cheap LLM call).", "go": "run", "until": "protocol"}
    if "protocol" in by:
        return {"text": "Check and approve the eligibility criteria.", "go": "todo", "group": "protocol"}
    if not st.get("search") and ctx.cfg["pipeline"].get("confirm_search") and not state.confirmed_query(st):
        return {"text": "Choose the search strategy: the agent's query, an alternative, or one you ask for; the search runs "
                        "with the query you confirm.", "go": "todo", "group": "search"}
    if not st.get("search") and state.confirmed_query(st):
        c = state.confirmed_query(st)
        return {"text": f"Run the search with your confirmed query ({c.get('count')} PubMed hits), then screening.",
                "go": "run", "until": "eligibility"}
    if not st.get("search") or not st.by_type("screen"):
        return {"text": "Run search and screening.", "go": "run", "until": "eligibility"}
    if "changeset" in by or "disagreement" in by or "clarify" in by:
        n = len(by.get("changeset", [])) + len(by.get("disagreement", [])) + len(by.get("clarify", []))
        return {"text": f"Review {n} answer{'s' if n > 1 else ''} from the agent to your feedback.", "go": "todo", "group": "feedback"}
    if "eligibility" in by:
        n = len(by["eligibility"])
        return {"text": f"Decide eligibility for {n} stud{'ies' if n > 1 else 'y'} the agent could not decide "
                        f"(suggestions are pre-filled).", "go": "todo", "group": "eligibility"}
    if new_files:
        return {"text": f"Process {new_files} added full-text file{'s' if new_files > 1 else ''}.", "go": "pdfs"}
    if "sync" in by:
        return {"text": "Update the outputs affected by your decisions.", "go": "todo", "group": "sync"}
    if fulltext.waiting_for_pdfs(ctx) and not st.by_type("synthesis"):
        return {"text": f"The synthesis waits for {fulltext.waiting_for_pdfs(ctx)} requested full text(s) (pipeline.wait_for_pdfs): "
                        "add them or mark them not obtainable, then continue the review.", "go": "pdfs"}
    if not st.by_type("synthesis"):
        return {"text": "Continue the review: extraction, risk of bias and synthesis.", "go": "run", "until": "report"}
    if "manual_edit" in by:
        return {"text": "Decide whether the agent should update the outputs that depend on your edits.", "go": "todo", "group": "edits"}
    if "flag" in by:
        return {"text": f"Check {len(by['flag'])} point{'s' if len(by['flag']) > 1 else ''} the agent flagged.", "go": "todo", "group": "flags"}
    if "rob" in by:
        return {"text": f"Decide the risk-of-bias points neither agent could settle ({len(by['rob'])} studies).", "go": "todo", "group": "rob"}
    if "fulltexts" in by:
        return {"text": "Optional: add full texts for studies extracted from abstracts only.", "go": "pdfs"}
    return {"text": "Everything is up to date. Read the report, and flag anything you disagree with.", "go": "report"}
