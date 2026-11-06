"""Stage 5: evidence-linked risk-of-bias drafts. A draft with a reason for a second look (the agent's own doubt, a
judgment without a cited sentence, an overall milder than a domain) is settled by the verifier when
pipeline.verify_rob is "flagged", and reaches a person only when neither settles it; the rest stay drafts a person
may confirm."""
from __future__ import annotations

import json

from .. import prompts
from ..context import Ctx
from ..evidence import attach, cites_of, packed


def _budget(ctx: Ctx) -> int:
    from .extraction import context_budget
    return context_budget(ctx)


def rob_one(ctx: Ctx, rid: str) -> dict:
    st = ctx.store
    old = st.get(f"rob:{rid}")
    if old and old["data"].get("human_confirmed"):
        # a person confirmed (or changed) this assessment: it is not drafted again; the source change is noted
        ftn = st.get(f"ft:{rid}")
        d = dict(old["data"], source_version=ftn["version"] if ftn else None)
        st.annotate(old["id"], d, evidence=old["evidence"], reason="source changed; the assessment a person confirmed is kept")
        return d   # it stays out of date with the new text: a derived note (attention.py) says so
    text, kind = st.text_of(rid)
    src_node = f"ft:{rid}" if st.get(f"ft:{rid}") else f"rec:{rid}"
    ext_brief = _ext_brief(st, rid)
    out = ctx.writer.chat(ctx.msgs(prompts.ROB.format(domains=_domains(ctx), extraction=ext_brief, kind=kind,
                                                     rid=rid, text=packed(text, _budget(ctx)))), tag="rob")
    evidence = []
    doms = [d for d in out.get("domains", []) if isinstance(d, dict)]
    for d in doms:
        cites = cites_of(d)
        d.pop("quote", None)
        evidence.append(attach(d, cites, text, f"{d.get('domain')}={d.get('judgment')}", src_node))
    ftn = st.get(f"ft:{rid}")
    names = {d.get("domain") for d in doms}
    doubts = [{"domain": str(x.get("domain")), "reason": str(x.get("reason") or "")[:300]}
              for x in out.get("doubts") or [] if isinstance(x, dict) and x.get("domain") in names]
    data = {"rid": rid, "tool": ctx.topic.get("rob_tool_name", "custom"), "source_kind": kind,
            "source_version": ftn["version"] if ftn else None,
            "domains": doms, "overall": out.get("overall", "unclear"), "doubts": doubts,
            "status_note": "DRAFT (agent)"}
    data["reasons"] = reasons_of(data)
    if data["reasons"] and _verify_wanted(ctx):
        _verify(ctx, rid, data, evidence, text, kind, src_node, ext_brief)
    from ..graph import domain_deps
    st.write(f"rob:{rid}", "rob", data, depends_on=[f"ext:{rid}", src_node] + domain_deps(ctx.cfg), evidence=evidence,
              reason="risk-of-bias draft" + (f"; {data['rob_verify']['outcome'].replace('_', ' ')}" if data.get("rob_verify") else ""))
    return data


# --------------------------------------------------------------------------- reasons for a second look
RANK = {"low": 0, "some_concerns": 1, "high": 2}


def reasons_of(data: dict) -> list[dict]:
    """Why a draft needs a second look; most drafts have none and need no one's attention. The agent's own doubts;
    a low or high judgment that cites no sentence of the source; an overall judgment milder than a domain."""
    out = [{"kind": "agent_doubt", "domain": x["domain"], "text": f"The assessing agent could not settle it: {x['reason']}"}
           for x in data.get("doubts") or []]
    for d in data.get("domains") or []:
        if d.get("judgment") in ("low", "high") and (d.get("check") or {}).get("status") == "none":
            out.append({"kind": "no_evidence", "domain": d.get("domain"),
                        "text": f"'{d.get('judgment')}' for {d.get('domain')} cites no sentence of the source"})
    worst = max((d for d in data.get("domains") or [] if d.get("judgment") in RANK),
                key=lambda d: RANK[d["judgment"]], default=None)
    ov = data.get("overall")
    if worst and ov in RANK and RANK[ov] < RANK[worst["judgment"]]:
        out.append({"kind": "overall_milder", "domain": worst.get("domain"),
                    "text": f"overall '{ov}' is milder than '{worst['judgment']}' for {worst.get('domain')}"})
    return out


def needs_person(data: dict) -> bool:
    """A draft goes to a person's To-do list only with a reason the verifier did not settle."""
    if data.get("human_confirmed"):
        return False
    v = data.get("rob_verify") or {}
    reasons = data.get("reasons") if "reasons" in data else reasons_of(data)
    return bool(reasons) and v.get("outcome") not in ("agreed", "decided_by_verifier")


def _verify_wanted(ctx: Ctx) -> bool:
    return (ctx.cfg["pipeline"].get("verify_rob") or "off") == "flagged"


def _domains(ctx: Ctx) -> str:
    return "\n".join(f"- {d['name']}: {d['description']}" for d in ctx.topic["rob_domains"])


def _ext_brief(st, rid: str) -> str:
    ext = st.get(f"ext:{rid}")
    return json.dumps({k: v["value"] for k, v in ext["data"]["fields"].items()}, ensure_ascii=False) if ext else "{}"


def _verify(ctx: Ctx, rid: str, data: dict, evidence: list, text: str, kind: str, src_node: str, ext_brief: str) -> None:
    """The verifier reads the source, the draft and the reasons, and settles the flagged points: its judgments replace
    the draft's (the agent's are kept beside them), or, when it cannot settle them either, the draft goes to a person.
    The record of both readings is kept on the node (`rob_verify`)."""
    import time
    chk = ctx.hub.model("rob", "verifier")
    draft = "\n".join(f"- {d.get('domain')}: {d.get('judgment')} ({(d.get('rationale') or '')[:400]}) "
                      f"[{', '.join(map(str, d.get('evidence') or []))}]" for d in data["domains"]) + f"\noverall: {data['overall']}"
    reasons = "\n".join(f"- {r['text']}" for r in data["reasons"])
    out = chk.chat(ctx.msgs(prompts.ROB_VERIFY.format(domains=_domains(ctx), extraction=ext_brief, draft=draft,
                                                      reasons=reasons, kind=kind, rid=rid,
                                                      text=packed(text, _budget(ctx)))), tag="rob_verify")
    rec = {"by": chk.name, "reasons": data["reasons"], "explanation": str(out.get("explanation") or "")[:600],
           "ts": time.strftime("%Y-%m-%d %H:%M"), "changed": []}
    if not out.get("decided", True):
        rec["outcome"] = "still_undecided"
        rec["domains"] = [{k: x.get(k) for k in ("domain", "judgment", "rationale")}
                          for x in out.get("domains") or [] if isinstance(x, dict)]
        data["rob_verify"] = rec
        return
    by_name = {d.get("domain"): (i, d) for i, d in enumerate(data["domains"])}
    for v in out.get("domains") or []:
        if not isinstance(v, dict) or v.get("domain") not in by_name or v.get("judgment") not in ("low", "some_concerns", "high", "unclear"):
            continue
        i, d = by_name[v["domain"]]
        if v["judgment"] == d.get("judgment") and (d.get("evidence") or not cites_of(v)):
            continue
        rec["changed"].append({"domain": d.get("domain"), "agent": d.get("judgment"), "verifier": v["judgment"],
                               "agent_rationale": (d.get("rationale") or "")[:400]})
        d.update({"judgment_agent": d.get("judgment_agent") or d.get("judgment"), "judgment": v["judgment"],
                  "rationale": f"verifier: {v.get('rationale') or ''}"[:900], "decided_by": "verifier"})
        ev = attach(d, cites_of(v), text, f"{d.get('domain')}={d.get('judgment')}", src_node)
        if i < len(evidence):
            evidence[i] = ev
    ov = out.get("overall")
    if ov in ("low", "some_concerns", "high", "unclear") and ov != data["overall"]:
        rec["changed"].append({"domain": "overall", "agent": data["overall"], "verifier": ov})
        data["overall_agent"] = data["overall"]
        data["overall"] = ov
    rec["outcome"] = "decided_by_verifier" if rec["changed"] else "agreed"
    data["rob_verify"] = rec
    data["status_note"] = "DRAFT (agent); flagged points settled by the verifier"


def verify_existing(ctx: Ctx, only: list[str] | None = None) -> dict:
    """The verifier's second look for drafts made before pipeline.verify_rob was on: only drafts with a reason (see
    reasons_of; the agent's own doubts exist only on drafts made since they were asked for), not confirmed by a
    person and not already checked."""
    from collections import Counter
    st = ctx.store
    todo = []
    for n in st.by_type("rob", active_only=True):
        rid = n["id"].split(":", 1)[1]
        d = n["data"]
        if (only and rid not in only) or d.get("human_confirmed") or d.get("rob_verify") or rid not in st.included_ids():
            continue
        if reasons_of(d):
            todo.append(rid)
    ctx.log(f"[rob] second look at {len(todo)} flagged drafts")

    def one(rid):
        n = st.get(f"rob:{rid}")
        d = json.loads(json.dumps(n["data"]))
        d["reasons"] = reasons_of(d)
        text, kind = st.text_of(rid)
        src_node = f"ft:{rid}" if st.get(f"ft:{rid}") else f"rec:{rid}"
        evidence = json.loads(json.dumps(n.get("evidence") or []))
        _verify(ctx, rid, d, evidence, text, kind, src_node, _ext_brief(st, rid))
        st.annotate(n["id"], d, evidence=evidence,
                    reason=f"second look by the verifier: {d['rob_verify']['outcome'].replace('_', ' ')}")
        return d["rob_verify"]["outcome"]
    outcomes = Counter(ctx.map(one, todo))
    ctx.save()
    ctx.log(f"[rob] second look done: {dict(outcomes)}")
    return {"checked": len(todo), "outcomes": dict(outcomes)}


def rob_all(ctx: Ctx, only: list[str] | None = None) -> int:
    st = ctx.store
    from .extraction import source_changed
    rids = only or [rid for rid in st.included_ids() if st.get(f"ext:{rid}") and
                    ((st.get(f"rob:{rid}") or {}).get("status") != "active" or source_changed(st, f"rob:{rid}"))]
    ctx.log(f"[rob] assessing {len(rids)} studies")
    ctx.map(lambda r: rob_one(ctx, r), rids)
    ctx.save()
    return len(rids)


def reassess_all(ctx: Ctx, only: list[str] | None = None) -> dict:
    """Draft every included study's risk of bias again with the current prompt and settings (the agent's own doubts,
    verify_rob), asking the model afresh; drafts a person confirmed or changed are kept. The synthesis statements
    citing a study become out of date as usual and are revised by the next update. A pass that stopped part-way (budget,
    error) resumes: drafts it already made are not paid for again."""
    import time
    st = ctx.store
    rids = [rid for rid in (only or st.included_ids()) if st.get(f"ext:{rid}")
            and not (st.get(f"rob:{rid}") and st.get(f"rob:{rid}")["data"].get("human_confirmed"))]
    run = st.meta.get("rob_reassess") or {}
    if run.get("rids") == sorted(rids):        # the same pass, unfinished: skip what it already drafted
        rids = [r for r in rids if r not in set(run.get("done") or [])]
        ctx.log(f"[rob] resuming the redraft started {time.strftime('%Y-%m-%d %H:%M', time.localtime(run['started']))}: "
                f"{len(rids)} left")
    else:
        st.meta["rob_reassess"] = run = {"started": time.time(), "rids": sorted(rids), "done": []}
        ctx.save()
    run.setdefault("done", [])
    ctx.hub.cache_read = False
    for c in list(ctx.hub._clients.values()):
        c.cache_read = False
    ctx.log(f"[rob] redrafting {len(rids)} studies")

    def one(rid):
        rob_one(ctx, rid)
        run["done"].append(rid)                # saved with the store every few studies (Ctx.map)
    ctx.map(one, rids)
    kept = sum(1 for rid in (only or st.included_ids()) if st.get(f"rob:{rid}") and st.get(f"rob:{rid}")["data"].get("human_confirmed"))
    st.meta.pop("rob_reassess", None)
    ctx.save()
    from collections import Counter
    out = Counter((st.get(f"rob:{r}")["data"].get("rob_verify") or {}).get("outcome") or
                  ("flagged" if reasons_of(st.get(f"rob:{r}")["data"]) else "no reason") for r in rids)
    return {"redrafted": len(rids), "kept_confirmed": kept, "outcomes": dict(out)}


def rob_text(ctx: Ctx, rids: list[str] | None = None) -> str:
    st = ctx.store
    rids = rids if rids is not None else st.included_ids()
    out = []
    for rid in rids:
        n = st.get(f"rob:{rid}")
        if n and n["status"] == "active":
            doms = "; ".join(f"{d.get('domain')}: {d.get('judgment')}" for d in n["data"]["domains"])
            out.append(f"[{rid}] overall={n['data']['overall']} ({doms})")
    return "\n".join(out)
