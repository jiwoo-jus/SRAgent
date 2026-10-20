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
  rob           rob        confirm or change each risk-of-bias draft
  changeset     feedback   accept or reject a proposed revision
  sync          synthesis  outputs that are out of date after human decisions
"""
from __future__ import annotations

import json
import time

from .context import Ctx
from .store import criteria_by_id, record_of
from .stages import fulltext

STEPS = [
    ("plan", "1. Protocol"), ("search", "2. Search"), ("screen", "3. Screening"),
    ("fulltext", "4. Full texts"), ("extract", "5. Data extraction"), ("rob", "6. Risk of bias"),
    ("synthesis", "7. Synthesis"), ("feedback", "8. Feedback & revisions"),
]
STEP_OF_TYPE = {"screen": "screen", "extraction": "extract", "rob": "rob", "synthesis": "synthesis"}


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
    st = ctx.store
    todos: list[dict] = []
    if not st.get("protocol"):
        return todos
    crit = criteria_by_id(st)

    # -- protocol approval
    if not st.meta.get("protocol_approved"):
        todos.append({
            "id": "protocol", "step": "plan", "kind": "protocol", "priority": 0,
            "title": "Approve the review protocol",
            "question": "Do these eligibility criteria match what you want to review?",
            "context": [f"{c['id']} ({c['type']}, {c.get('stage', '')}): {c['text']}" for c in crit.values()],
            "options": [{"value": "approve", "label": "Yes, approve"},
                        {"value": "edited", "label": "I edited protocol.yaml, re-apply it",
                         "hint": f"File: {ctx.run_dir / 'protocol.yaml'}. Decisions are re-derived without LLM calls."}],
            "free_text": "Anything to note about the protocol? (optional)"})

    # -- eligibility: awaiting classification (shown as one table in the UI)
    wanted_pdf = st.meta.get("pdf_wanted", {})
    for n in st.by_type("screen"):
        d = n["data"]
        rid = n["id"].split(":", 1)[1]
        if d["decision"] != "awaiting" or d.get("human_decision"):
            continue
        if wanted_pdf.get(rid) and ((st.get(f"ft:{rid}") or {}).get("data", {}).get("kind") != "fulltext"):
            continue  # waiting for the PDF; listed under Full texts
        r = record_of(st, rid)
        def _bad(a):
            c = crit.get(a.get("id"))
            if not c or c["type"] == "practical":
                return False
            v = a.get("assessment")
            return v == "unclear" or (c["type"] == "inclusion" and v == "not_met") or (c["type"] == "exclusion" and v == "met")
        unclear = [a for a in d.get("criteria", []) if _bad(a)]
        why = ", ".join(f"{a['id']} ({_short(crit[a['id']]['text'])})" for a in unclear[:4])
        not_met = [a["id"] for a in unclear if (crit[a["id"]]["type"] == "inclusion" and a.get("assessment") == "not_met")
                   or (crit[a["id"]]["type"] == "exclusion" and a.get("assessment") == "met")]
        has_abs = bool(r.get("abstract"))
        if not_met:
            suggest = {"option": "exclude", "criterion": not_met[0], "why": f"agent: {not_met[0]} fails"}
        else:
            suggest = {"option": "pdf", "why": "needs the full text to decide" if has_abs else "no abstract; needs the full text"}
        todos.append({
            "id": f"elig:{rid}", "step": "screen", "kind": "eligibility", "priority": 1, "rid": rid,
            "title": (r.get("title") or "(no title)")[:200],
            "meta": f"{_study_label(st, rid)} · {r.get('journal', '')} {r.get('year', '')} · "
                    f"{', '.join(r.get('pub_types') or [])}",
            "pmid": r.get("pmid", ""), "doi": r.get("doi", ""), "has_abstract": has_abs,
            "abstract": (r.get("abstract") or "")[:1200], "why": why or "criteria unclear",
            "suggest": suggest, "criteria": [c for c in crit if crit[c]["type"] != "practical"],
            "question": "Should this study be included?",
            "options": [{"value": "include", "label": "Include"}, {"value": "exclude", "label": "Exclude"},
                        {"value": "pdf", "label": "Need full text"}],
            "link": f"/report#screen:{rid}"})

    # -- full texts (grouped)
    unavailable = set(st.meta.get("pdf_unavailable", []))
    wanted = [x for x in fulltext.needed_pdfs(ctx) if x["rid"] not in unavailable]   # blocked or asked for
    if wanted:
        present = [x for x in wanted if x["present"]]
        todos.append({
            "id": "fulltexts", "step": "fulltext", "kind": "fulltexts", "priority": 2,
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
            if f["status"] != "open" or f.get("source") not in ("synthesis_verify", "term_validation", "evidence_check"):
                continue
            step = STEP_OF_TYPE.get(n["type"], "feedback")
            what = n["data"].get("text") if n["type"] == "synthesis" else ""
            if f["source"] == "synthesis_verify":
                q = f"The self-check thinks statement {n['data'].get('id')} may misstate or overstate the evidence. What should happen?"
                opts = [{"value": "fix", "label": "Ask the agent to revise it"}, {"value": "ok", "label": "It is fine, keep it"}]
            elif f["source"] == "term_validation":
                q = "A term was not found in RxNorm / PubChem / ChEMBL / MONDO. Is the extracted value right?"
                opts = [{"value": "ok", "label": "It is fine (e.g. a drug class)"},
                        {"value": "fix", "label": "Correct it (describe below)"}]
            else:
                q = "This value has no source location. Is it right?"
                opts = [{"value": "ok", "label": "It is right"}, {"value": "fix", "label": "Ask the agent to re-check it"}]
            rid = n["id"].split(":", 1)[1] if ":" in n["id"] else ""
            label = {"synthesis_verify": f"Statement {n['data'].get('id', '')} may overstate the evidence",
                     "term_validation": f"Unrecognised term in study {rid}",
                     "evidence_check": f"Value without a source location in study {rid}"}[f["source"]]
            todos.append({
                "id": f"flag:{n['id']}:{f['id']}", "step": step, "kind": "flag", "priority": 3,
                "title": label,
                "question": q, "context": [x for x in [what, f["text"]] if x],
                "options": opts, "free_text": "Your comment (sent to the agent if you choose to fix)",
                "link": f"/report#{n['id'] if n['type'] == 'synthesis' else 'ext:' + rid}"})

    # -- manual edits whose dependents may be stale
    for n in st.nodes.values():
        for f in n["flags"]:
            if f["status"] != "open" or f.get("source") != "manual_edit":
                continue
            rid = n["id"].split(":", 1)[1] if ":" in n["id"] else ""
            todos.append({
                "id": f"edited:{n['id']}:{f['id']}", "step": STEP_OF_TYPE.get(n["type"], "feedback"), "kind": "manual_edit",
                "priority": 2, "title": f"You edited {n['id']} ({_study_label(st, rid)})",
                "question": "Other outputs use this one. Should the agent update them?",
                "context": [f["text"].split(" dependent output")[0].split(". ")[0] + ".",
                            "Depends on it: " + ", ".join(f.get("dependents", []))],
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

    # -- risk of bias confirmation
    included = set(st.included_ids())
    for n in st.by_type("rob", active_only=True):
        if n["data"].get("human_confirmed"):
            continue
        rid = n["id"].split(":", 1)[1]
        if rid not in included:
            continue
        doms = [d.get("domain") for d in n["data"]["domains"]]
        todos.append({
            "id": f"rob:{rid}", "step": "rob", "kind": "rob", "priority": 4,
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
                    "id": f"clarify:{cs['id']}", "step": "feedback", "kind": "clarify", "priority": 0,
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
                "title": f"{what} ({len(cs['changes'])} output{'s' if len(cs['changes']) != 1 else ''})",
                "question": "Apply these changes?",
                "context": lines,
                "options": [{"value": "accept", "label": "Accept"}, {"value": "reject", "label": "Reject"}],
                "free_text": "Note (optional)"})

    # -- out-of-date outputs
    from .sync import pending_changes
    pc = pending_changes(ctx)
    if st.by_type("extraction") and (pc["need_extract"] or pc["new_for_synthesis"] or pc["excluded_but_cited"]):
        lines = []
        if pc["need_extract"]:
            lines.append(f"Included but not yet extracted / assessed: {', '.join(pc['need_extract'])}")
        if pc["new_for_synthesis"]:
            lines.append(f"Included but not yet considered in the synthesis: {', '.join(pc['new_for_synthesis'])}")
        if pc["excluded_but_cited"]:
            lines.append(f"Excluded but still cited by the synthesis: {', '.join(pc['excluded_but_cited'])}")
        todos.append({
            "id": "sync", "step": "synthesis", "kind": "sync", "priority": 1,
            "title": "Some outputs are out of date after your decisions",
            "question": "Update them now?",
            "context": lines,
            "options": [{"value": "sync", "label": "Update now",
                         "hint": "Extracts and assesses new studies, then revises only the affected synthesis statements."},
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
    with open(ctx.run_dir / "human_decisions.jsonl", "a") as f:
        f.write(json.dumps({**answer, "status": status, "ts": time.time()}) + "\n")


def _category(tid: str, opt: str | None) -> str | None:
    """Provenance category of an answer: PDF supply is kept apart from human judgment."""
    if opt == "later":
        return None
    if tid == "fulltexts":
        return "pdf_provision"
    if tid == "sync":
        return None          # starts the agent; no human judgment about content
    return "human_judgment"


def apply(ctx: Ctx, answer: dict) -> dict:
    """Apply one answer {todo_id, option, text, extra}. Returns {"ok", "message", "job"?}."""
    from . import feedback, provenance
    st = ctx.store
    tid, opt = answer["todo_id"], answer.get("option")
    text = (answer.get("text") or "").strip()
    extra = answer.get("extra") or {}
    msg, job = "saved", None
    cat = _category(tid, opt)
    if cat == "human_judgment":
        try:
            provenance.require_judgment_allowed(ctx, f"{tid} -> {opt}")
        except provenance.NotAllowed as e:
            return {"ok": False, "message": str(e)}

    if opt == "later":
        st.meta.setdefault("snoozed", [])
        if tid not in st.meta["snoozed"]:
            st.meta["snoozed"].append(tid)
        msg = "snoozed"
    elif tid == "protocol":
        if opt == "approve":
            st.meta["protocol_approved"] = {"ts": time.time(), "note": text}
            msg = "protocol approved"
        elif opt == "edited":
            import yaml
            from .stages import screening
            from .stages.planning import sync_criteria
            protocol = yaml.safe_load(open(ctx.run_dir / "protocol.yaml"))
            st.upsert("protocol", "protocol", protocol, actor="user", reason="protocol edited by user")
            sync_criteria(st, protocol, actor="user", reason="protocol edited")
            r = screening.rescreen_rule_only(ctx, protocol)
            msg = f"protocol re-applied: {r['changed']} decisions changed"
    elif tid.startswith("elig:"):
        rid = tid.split(":", 1)[1]
        n = st.get(f"screen:{rid}")
        if opt == "include" and (st.get(f"ft:{rid}") or {}).get("data", {}).get("kind") != "fulltext":
            return {"ok": False, "message": f"{rid} cannot be included without a full-text review; supply {rid}.pdf first."}
        if opt in ("include", "exclude"):
            d = dict(n["data"])
            d["decision"] = opt
            d["because"] = [extra.get("criterion") or "human"] if opt == "exclude" else []
            d["human_decision"] = {"decision": opt, "note": text, "reason": extra.get("criterion"), "ts": time.time()}
            st.upsert(n["id"], "screen", d, depends_on=n["depends_on"], actor="user",
                      reason=f"human eligibility decision: {opt}" + (f" ({text})" if text else ""))
            for f in n["flags"]:
                if f["status"] == "open":
                    st.resolve_flag(n["id"], f["id"], f"human decided: {opt}", actor="user")
            msg = f"{rid} {opt}d" + ("; extraction and synthesis can be updated from the to-do list" if opt == "include" else "")
        elif opt == "pdf":
            st.meta.setdefault("pdf_wanted", {})[rid] = True
            msg = f"waiting for {rid}.pdf in {fulltext.fulltext_dir(ctx)}"
    elif tid == "fulltexts":
        ids = [x.strip() for x in (extra.get("unavailable") or "").replace(";", ",").split(",") if x.strip()]
        if ids:
            st.meta["pdf_unavailable"] = sorted(set(st.meta.get("pdf_unavailable", [])) | set(ids))
            for rid in ids:
                st.meta.get("pdf_wanted", {}).pop(rid, None)
        if opt == "process":
            job = {"action": "pdfs"}
            msg = "processing added files"
        else:
            msg = f"marked {len(ids)} as not obtainable" if ids else "saved"
    elif tid.startswith("flag:"):
        # node ids contain a colon themselves (e.g. syn:S4), so split from the right
        node_id, fid = tid[5:].rsplit(":", 1)
        n = st.get(node_id)
        flag = next((f for f in n["flags"] if f["id"] == fid), None)
        if opt == "ok":
            st.resolve_flag(node_id, fid, f"human: fine as is. {text}".strip(), actor="user")
            msg = "flag closed"
        elif opt == "fix":
            fb = text or f"Please check and fix: {flag['text'] if flag else ''}"
            if flag and text:
                fb = f"{text} (context: {flag['text'][:300]})"
            st.resolve_flag(node_id, fid, "sent to the agent as feedback", actor="user")
            job = {"action": "feedback", "target": node_id, "text": fb}
            msg = "the agent will propose a revision; it appears here for your approval"
    elif tid.startswith("clarify:"):
        cid = tid.split(":", 1)[1]
        p = ctx.run_dir / "feedback" / f"{cid}.json"
        cs = json.loads(p.read_text())
        cs["status"] = "clarified" if opt == "answer" else "withdrawn"
        cs["clarification_answer"] = text
        p.write_text(json.dumps(cs, indent=1, default=str))
        if opt == "answer":
            job = {"action": "feedback", "target": cs.get("target"), "text": f"{cs['feedback']}\nClarification: {text}"}
            msg = "sent to the agent with your clarification"
        else:
            msg = "feedback withdrawn"
    elif tid.startswith("edited:"):
        node_id, fid = tid[len("edited:"):].rsplit(":", 1)
        n = st.get(node_id)
        flag = next((f for f in n["flags"] if f["id"] == fid), None)
        if opt == "ok":
            st.resolve_flag(node_id, fid, f"human: dependents are fine. {text}".strip(), actor="user")
            msg = "closed"
        elif opt == "update":
            job = {"action": "propagate", "target": node_id, "from_version": (flag or {}).get("from_version"),
                   "text": text, "rid": node_id.split(":", 1)[1]}
            msg = "the agent will propose updates to the dependent outputs"
    elif tid.startswith("disagree:"):
        node_id, fid = tid[len("disagree:"):].rsplit(":", 1)
        n = st.get(node_id)
        flag = next((f for f in n["flags"] if f["id"] == fid), None)
        if opt == "agree":
            st.resolve_flag(node_id, fid, f"human agreed with the agent. {text}".strip(), actor="user")
            msg = "closed"
        elif opt == "insist":
            orig = flag["text"].split("-> agent:", 1)[0].split(":", 1)[-1].strip() if flag else ""
            st.resolve_flag(node_id, fid, "human insisted; re-sent without the evidence check", actor="user")
            job = {"action": "feedback", "target": node_id, "text": (orig + (" " + text if text else "")).strip(),
                   "no_verify": True}
            msg = "the agent will propose your change for approval"
    elif tid.startswith("rob:"):
        rid = tid.split(":", 1)[1]
        n = st.get(f"rob:{rid}")
        d = json.loads(json.dumps(n["data"]))
        if opt == "confirm":
            d["human_confirmed"] = {"ts": time.time(), "note": text}
            st.upsert(n["id"], "rob", d, depends_on=n["depends_on"], evidence=n["evidence"], actor="user",
                      reason="risk of bias confirmed by human")
            msg = "confirmed"
        elif opt == "change" and extra.get("changes") is not None:
            from . import actions
            r = actions.edit(ctx, {"kind": "rob", "rid": rid, "changes": extra["changes"], "note": text})
            log_answer(ctx, answer, "applied")
            return {"ok": r["ok"], "message": r["message"]}
        elif opt == "change":
            dom, judg = extra.get("domain"), extra.get("judgment")
            for x in d["domains"]:
                if x.get("domain") == dom:
                    x["judgment_agent"] = x.get("judgment")
                    x["judgment"] = judg
                    x["human_note"] = text
            order = {"low": 0, "unclear": 1, "some_concerns": 2, "high": 3}
            d["overall"] = max((x.get("judgment") for x in d["domains"]), key=lambda j: order.get(j, 1))
            d["human_confirmed"] = {"ts": time.time(), "note": f"changed {dom} to {judg}. {text}".strip()}
            st.upsert(n["id"], "rob", d, depends_on=n["depends_on"], evidence=n["evidence"], actor="user",
                      reason=f"human changed {dom} to {judg}")
            msg = f"{dom} set to {judg}"
        elif opt == "fix":
            job = {"action": "feedback", "target": n["id"], "text": text or "Please re-assess this risk-of-bias draft."}
            msg = "the agent will propose a re-assessment"
    elif tid.startswith("cs:"):
        cid = tid.split(":", 1)[1]
        if opt == "accept":
            try:
                feedback.accept(ctx, cid)
            except feedback.StaleProposal as e:
                log_answer(ctx, answer, "refused_stale")
                return {"ok": False, "message": str(e)}
            from .stages import report
            report.write_reports(ctx)
            msg = "changes applied, report regenerated"
        else:
            feedback.reject(ctx, cid, text)
            msg = "proposal rejected; the original stays"
    elif tid == "sync":
        if opt == "sync":
            job = {"action": "sync"}
            msg = "updating dependent outputs"
    else:
        return {"ok": False, "message": f"unknown to-do {tid}"}
    ctx.save()
    log_answer(ctx, answer, "applied")
    if cat == "pdf_provision":
        ids = [x.strip() for x in (extra.get("unavailable") or "").replace(";", ",").split(",") if x.strip()]
        for rid in ids:
            provenance.record(ctx, "pdf_provision", "not_obtainable", rid=rid)
    elif cat == "human_judgment":
        provenance.record(ctx, "human_judgment", f"{tid.split(':', 1)[0]}:{opt}", target=tid,
                          rid=tid.split(":")[-1] if tid.startswith(("elig:", "rob:")) else None, note=text[:300])
    return {"ok": True, "message": msg, "job": job}


def bulk(ctx: Ctx, kind: str, ids: list[str], note: str = "", items: list[dict] | None = None) -> int:
    from . import provenance
    if kind != "pdf_unavailable":
        provenance.require_judgment_allowed(ctx, kind)
    n = 0
    if kind == "eligibility":
        for it in items or []:
            apply(ctx, {"todo_id": f"elig:{it['rid']}", "option": it["option"], "text": it.get("text", ""),
                        "extra": {"criterion": it.get("criterion")}})
            n += 1
    if kind == "restore_rule":
        from . import rules
        for rid in ids:
            rules.restore(ctx, rid, note)
            log_answer(ctx, {"todo_id": f"rule:{rid}", "option": "restore", "text": note}, "applied")
            provenance.record(ctx, "human_judgment", "restore_rule_exclusion", rid=rid, note=note)
            n += 1
        ctx.save()
    if kind == "pdf_unavailable":
        st = ctx.store
        st.meta["pdf_unavailable"] = sorted(set(st.meta.get("pdf_unavailable", [])) | set(ids))
        for rid in ids:
            st.meta.get("pdf_wanted", {}).pop(rid, None)
            log_answer(ctx, {"todo_id": f"pdf:{rid}", "option": "unavailable"}, "applied")
            provenance.record(ctx, "pdf_provision", "not_obtainable", rid=rid)
            n += 1
        ctx.save()
    if kind == "pdf_unwant":
        for rid in ids:
            ctx.store.meta.get("pdf_wanted", {}).pop(rid, None)
            provenance.record(ctx, "human_judgment", "decide_without_fulltext", rid=rid)
            n += 1
        ctx.save()
    if kind == "rob_confirm":
        for tid in ids:
            if tid.startswith("rob:"):
                apply(ctx, {"todo_id": tid, "option": "confirm", "text": note or "bulk confirm"})
                n += 1
    return n


def rule_exclusions(ctx: Ctx) -> list[dict]:
    st = ctx.store
    out = []
    for n in st.by_type("screen"):
        d = n["data"]
        if d.get("rule") and d["decision"] == "exclude":
            rid = n["id"].split(":", 1)[1]
            r = record_of(st, rid)
            out.append({"rid": rid, "rule": d["rule"], "reason": d.get("rule_reason", ""),
                        "title": (r.get("title") or "(no title)")[:160], "pmid": r.get("pmid", ""), "doi": r.get("doi", "")})
    return sorted(out, key=lambda x: (x["rule"], x["rid"]))


def next_action(ctx: Ctx, todos: list[dict], job_running: bool, new_files: int) -> dict:
    """The single most useful thing to do now, in plain words."""
    st = ctx.store
    open_t = [t for t in todos if not t.get("snoozed")]
    by = {}
    for t in open_t:
        by.setdefault(t["kind"], []).append(t)
    if job_running:
        return {"text": "A job is running. You can keep answering; answers are applied when it finishes.", "go": "run"}
    from . import provenance
    if provenance.mode(ctx) == "agent_only" and st.get("protocol"):
        if new_files:
            return {"text": f"Process {new_files} added full-text file{'s' if new_files > 1 else ''}.", "go": "pdfs"}
        if "fulltexts" in by:
            return {"text": "Agent-only mode: the agent decides everything. You can only supply the full texts it requested.",
                    "go": "pdfs"}
        if not st.by_type("synthesis"):
            return {"text": "Agent-only mode: run the review; the agent makes every decision.", "go": "run", "until": "report"}
        return {"text": "Agent-only mode: the review is complete. Read the report.", "go": "report"}
    if not st.get("protocol"):
        return {"text": "Start the review: draft the protocol (one cheap LLM call).", "go": "run", "until": "plan"}
    if "protocol" in by:
        return {"text": "Check and approve the eligibility criteria.", "go": "todo", "group": "protocol"}
    if not st.get("search") or not st.by_type("screen"):
        return {"text": "Run search and screening.", "go": "run", "until": "fulltext"}
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
    if not st.by_type("synthesis"):
        return {"text": "Continue the review: extraction, risk of bias and synthesis.", "go": "run", "until": "report"}
    if "manual_edit" in by:
        return {"text": "Decide whether the agent should update the outputs that depend on your edits.", "go": "todo", "group": "edits"}
    if "flag" in by:
        return {"text": f"Check {len(by['flag'])} point{'s' if len(by['flag']) > 1 else ''} the agent flagged.", "go": "todo", "group": "flags"}
    if "rob" in by:
        return {"text": f"Confirm the risk-of-bias drafts ({len(by['rob'])} studies).", "go": "todo", "group": "rob"}
    if "fulltexts" in by:
        return {"text": "Optional: add full texts for studies extracted from abstracts only.", "go": "pdfs"}
    return {"text": "Everything is up to date. Read the report, and flag anything you disagree with.", "go": "report"}
