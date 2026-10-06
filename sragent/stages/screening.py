"""Stage 3: study selection (title/abstract, then full text).

The LLM assesses each atomic criterion separately (met / not_met / unclear + quote).
The decision is then derived by a transparent rule, so every exclusion can be traced to
a specific criterion and passage, and a reviewer can overturn a single criterion:

  exclude    if any inclusion criterion is not_met or any exclusion criterion is met
  uncertain  else if any criterion is unclear
             - title/abstract stage: forwarded to full-text screening
             - full-text stage: pipeline.fulltext_unclear decides:
                 "await"   -> 'awaiting' (PRISMA "studies awaiting classification"; flagged for a
                              human, not synthesised)  [default]
                 "include" -> included but flagged;  "exclude" -> excluded
  include    otherwise
Practical criteria (document access, language) are not judged by the LLM.

The LLM's own holistic decision is stored as `llm_decision` for analysis (hypothesis H1b:
how often does the holistic decision disagree with its own criterion-level assessments?).
"""
from __future__ import annotations

from .. import prompts
from ..context import Ctx
from ..evidence import attach, cites_of, numbered
from .planning import criteria_text, synonyms_text


def finalize(decision: str, stage: str, policy: str = "await") -> str:
    if decision == "uncertain" and stage == "full_text":
        return {"await": "awaiting", "include": "include", "exclude": "exclude"}.get(policy, "awaiting")
    return decision


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


def screen_one(ctx: Ctx, protocol: dict, rid: str, stage: str = "title_abstract", tag_suffix: str = "") -> dict:
    st = ctx.store
    text, src_node = screen_source(st, rid, stage)
    limit = None if stage == "title_abstract" else int(ctx.cfg["pipeline"].get("fulltext_screen_chars", 15000))
    out = ctx.agent.chat(ctx.msgs(prompts.SCREEN.format(criteria=criteria_text(protocol, stage),
                                                        synonyms=synonyms_text(protocol),
                                                        record=numbered(text, limit), stage=stage.replace("_", "/"))),
                         tag=f"screen_{stage}{tag_suffix}")
    assessments = [a for a in out.get("criteria", []) if isinstance(a, dict)]
    evidence = []
    for a in assessments:
        cites = cites_of(a)
        a.pop("quote", None)
        if cites:
            evidence.append(attach(a, cites, text, f"{a.get('id')}={a.get('assessment')}", src_node))
    decision, because = rule_decision(protocol, assessments, stage)
    decision = finalize(decision, stage, ctx.cfg["pipeline"].get("fulltext_unclear", "await"))
    data = {"stage": stage, "decision": decision, "because": because, "criteria": assessments,
            "llm_decision": out.get("decision"), "reason": out.get("reason", ""),
            "confidence": out.get("confidence")}
    if stage == "full_text":
        prev = st.get(f"screen:{rid}")
        if prev:
            data["title_abstract"] = {k: prev["data"].get(k) for k in ("decision", "because")}
    deps = [f"rec:{rid}", "protocol"] + [f"crit:{c['id']}" for c in protocol.get("criteria", [])]
    if stage == "full_text":
        deps.append(src_node)
    st.upsert(f"screen:{rid}", "screen", data, depends_on=deps, evidence=evidence,
              reason=f"{stage} screening")
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


def fulltext_screen_all(ctx: Ctx, protocol: dict) -> dict:
    """Second pass on forwarded records (include/uncertain) using full text when available."""
    st = ctx.store
    has_ft_criteria = any(c.get("stage") == "full_text" for c in protocol.get("criteria", []))
    todo = []
    for n in st.by_type("screen"):
        rid = n["id"].split(":", 1)[1]
        if n["data"]["stage"] == "title_abstract" and n["data"]["decision"] in ("include", "uncertain"):
            ft = st.get(f"ft:{rid}")
            if (ft and ft["data"].get("kind") == "fulltext") or has_ft_criteria or n["data"]["decision"] == "uncertain":
                todo.append(rid)
    ctx.log(f"[screen] full-text screening {len(todo)} records")
    ctx.map(lambda rid: screen_one(ctx, protocol, rid, "full_text"), todo)
    flag_awaiting(ctx)
    ctx.save()
    from .. import rules
    rules.apply(ctx, protocol)
    ctx.save()
    inc = st.included_ids()
    ctx.log(f"[screen] final included studies: {len(inc)}")
    return {"included": len(inc)}


def flag_awaiting(ctx: Ctx):
    st = ctx.store
    for n in st.by_type("screen"):
        if n["data"]["decision"] == "awaiting" and not any(f["source"] == "screening" and f["status"] == "open"
                                                           for f in n["flags"]):
            st.flag(n["id"], f"Awaiting classification: criteria {n['data'].get('because')} unclear after "
                    f"full-text screening (source: {st.text_of(n['id'].split(':', 1)[1])[1]}). Please decide.",
                    actor="agent", source="screening")


def rescreen_rule_only(ctx: Ctx, protocol: dict) -> dict:
    """Re-derive every decision from the STORED criterion assessments after a protocol edit
    (e.g. a criterion re-typed as practical). No LLM calls. Records newly forwarded from the
    title/abstract stage are screened at full text by the next `sragent run`."""
    st = ctx.store
    policy = ctx.cfg["pipeline"].get("fulltext_unclear", "await")
    changed = 0
    from .. import rules
    for n in st.by_type("screen"):
        if n["data"].get("rule") or n["data"].get("human_decision"):
            continue
        d = dict(n["data"])
        dec, because = rule_decision(protocol, d.get("criteria", []), d["stage"])
        dec = finalize(dec, d["stage"], policy)
        if (dec, because) != (d["decision"], d.get("because")):
            d.pop("note", None)
            d["decision"], d["because"] = dec, because
            st.upsert(n["id"], "screen", d, depends_on=n["depends_on"], actor="user",
                      reason="protocol revised: decision re-derived from stored assessments")
            for f in n["flags"]:
                if f["status"] == "open" and f["source"] == "screening":
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
    flag_awaiting(ctx)
    ctx.save()
    ctx.log(f"[screen] rule-only re-screen: {changed} decisions changed; included now {len(inc)}")
    return {"changed": changed, "included": len(inc)}
