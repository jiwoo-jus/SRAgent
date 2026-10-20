"""Read-only views of the whole review for the UI and for export (no LLM calls).

studies_list(ctx)        one row per record: status, reason, flags, extraction / RoB summary
study_detail(ctx, rid)   everything about one study: record + links, screening with criteria and evidence,
                         source, extraction with evidence and term checks, RoB, synthesis statements that
                         cite it, all flags, comments, change proposals, full version history
study_markdown(detail)   the same as readable Markdown (copy / download)
history(ctx, ...)        global history: change proposals, human decisions, store changelog, LLM usage
"""
from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

from .context import Ctx
from .store import criteria_by_id, record_of, study_of

GROUPS = ["included", "pending", "excluded"]


def links(rec: dict, ft: dict | None = None) -> dict:
    out = {}
    if rec.get("pmid"):
        out["PubMed"] = f"https://pubmed.ncbi.nlm.nih.gov/{rec['pmid']}/"
    if rec.get("doi"):
        out["DOI"] = f"https://doi.org/{rec['doi']}"
    pmc = rec.get("pmcid") or (ft or {}).get("pmcid")
    if pmc:
        out["PMC"] = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc}/"
        out["Europe PMC"] = f"https://europepmc.org/article/PMC/{pmc.replace('PMC', '')}"
    elif rec.get("pmid"):
        out["Europe PMC"] = f"https://europepmc.org/article/MED/{rec['pmid']}"
    return out


def _stage(s):
    return {"title_abstract": "title/abstract", "full_text": "full text", "retrieval": "full-text retrieval"}.get(s, s or "")


def status_of(st, rid: str) -> tuple[str, str, str]:
    """(group, status label, reason)"""
    sc = st.get(f"screen:{rid}")
    if not sc:
        return "pending", "not screened yet", ""
    d = sc["data"]
    crit = criteria_by_id(st)
    because = ", ".join(f"{b} ({crit[b]['text'][:70]})" if b in crit else b for b in d.get("because", []))
    hd = d.get("human_decision")
    if d["decision"] == "exclude":
        if d.get("rule"):
            return "excluded", "excluded (rule)", d.get("rule_reason", d["rule"])
        if hd:
            return "excluded", "excluded (you)", (hd.get("reason") or "") + (f"; {hd['note']}" if hd.get("note") else "")
        return "excluded", f"excluded (agent, {_stage(d['stage'])})", because
    if d["decision"] == "include":
        return "included", "included (you)" if hd else "included", (hd or {}).get("note", "") if hd else ""
    if d["decision"] == "awaiting":
        return "pending", "undecided after full-text review", "unclear: " + because
    if d["decision"] == "fulltext_unavailable":
        no = rid in st.meta.get("pdf_unavailable", [])
        return "pending", "full text not retrieved" + (" (declared not obtainable)" if no else ""), d.get("retrieval_note", "")
    if d["decision"] == "parse_failed":
        return "pending", "full text unreadable", d.get("retrieval_note", "")
    if d["decision"] == "forward" or (d["decision"] == "include" and d["stage"] == "title_abstract"):
        return "pending", "forwarded to full-text review", because
    return "pending", f"{d['decision']} ({_stage(d['stage'])})", because


def _study_nodes(st, rid):
    return [f"{k}:{rid}" for k in ("rec", "screen", "ft", "ext", "rob") if st.get(f"{k}:{rid}")]


def _flags_of(st, rid):
    out = []
    for nid in _study_nodes(st, rid):
        for f in st.get(nid)["flags"]:
            out.append({**{k: f.get(k) for k in ("id", "text", "status", "actor", "source", "ts", "resolution")}, "node": nid})
    return out


def _comments(st, targets: set) -> list[dict]:
    return [c for c in st.meta.get("comments", []) if c["target"] in targets]


def studies_list(ctx: Ctx) -> list[dict]:
    st = ctx.store
    cited = {}
    for n in st.by_type("synthesis", active_only=True):
        for c in n["data"]["cites"]:
            cited.setdefault(c, []).append(n["data"]["id"])
    rows = []
    for n in st.by_type("record"):
        rid = n["id"].split(":", 1)[1]
        r = n["data"]
        g, s, why = status_of(st, rid)
        fl = _flags_of(st, rid)
        ext, rob = st.get(f"ext:{rid}"), st.get(f"rob:{rid}")
        ft = (st.get(f"ft:{rid}") or {}).get("data", {})
        rows.append({
            "rid": rid, "title": r.get("title") or "(no title)", "first_author": (r.get("authors") or "").split(",")[0],
            "year": r.get("year", ""), "journal": r.get("journal", ""), "pmid": r.get("pmid", ""), "doi": r.get("doi", ""),
            "group": g, "status": s, "reason": why,
            "source": ft.get("kind", "abstract" if r.get("abstract") else "title only"),
            "flags_open": sum(f["status"] == "open" for f in fl), "flags_total": len(fl),
            "extracted": bool(ext and ext["status"] == "active"),
            "rob_overall": (rob["data"]["overall"] if rob and rob["status"] == "active" else ""),
            "rob_confirmed": bool(rob and rob["data"].get("human_confirmed")),
            "cited_in": ", ".join(cited.get(rid, [])),
            "comments": len(_comments(st, {f"study:{rid}"} | set(_study_nodes(st, rid)))),
            "versions": sum(st.get(x)["version"] for x in _study_nodes(st, rid)),
            "updated": max(st.get(x)["updated"] for x in _study_nodes(st, rid)),
        })
    order = {g: i for i, g in enumerate(GROUPS)}
    return sorted(rows, key=lambda x: (order[x["group"]], x["status"], x["rid"]))


def diff_summary(ntype: str, before, after) -> list[str]:
    if before is None:
        return ["created"]
    if not isinstance(before, dict) or not isinstance(after, dict):
        return [f"{before} → {after}"]
    out = []
    if ntype in ("extraction",) and "fields" in after:
        for k, v in after["fields"].items():
            b = (before.get("fields") or {}).get(k, {}).get("value")
            if b != v.get("value"):
                out.append(f"{k}: “{str(b)[:160]}” → “{str(v.get('value'))[:160]}”")
        return out or ["evidence locations / term checks updated"]
    if ntype == "screen":
        if before.get("decision") != after.get("decision"):
            out.append(f"decision: {before.get('decision')} → {after.get('decision')}")
        bc = {a.get("id"): a.get("assessment") for a in before.get("criteria", [])}
        for a in after.get("criteria", []):
            if bc.get(a.get("id")) != a.get("assessment"):
                out.append(f"{a.get('id')}: {bc.get(a.get('id'))} → {a.get('assessment')}")
        if before.get("stage") != after.get("stage"):
            out.append(f"stage: {before.get('stage')} → {after.get('stage')}")
        return out or ["evidence / reasons updated"]
    if ntype == "rob" and "domains" in after:
        bd = {x.get("domain"): x.get("judgment") for x in before.get("domains", [])}
        out = [f"{x.get('domain')}: {bd.get(x.get('domain'))} → {x.get('judgment')}" for x in after["domains"]
               if bd.get(x.get("domain")) != x.get("judgment")]
        if before.get("overall") != after.get("overall"):
            out.append(f"overall: {before.get('overall')} → {after.get('overall')}")
        if after.get("human_confirmed") and not before.get("human_confirmed"):
            out.append("confirmed by you")
        return out or ["rationale / evidence updated"]
    if ntype == "synthesis":
        if before.get("text") != after.get("text"):
            out.append(f"text: “{before.get('text', '')[:200]}” → “{after.get('text', '')[:200]}”")
        if before.get("cites") != after.get("cites"):
            out.append(f"cites: {before.get('cites')} → {after.get('cites')}")
        return out or ["scope / verification updated"]
    if ntype == "fulltext":
        return [f"source: {before.get('origin')} → {after.get('origin')} ({after.get('chars')} chars)"]
    keys = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
    return [f"{k} changed" for k in keys[:6]] or ["updated"]


def _ntype(st, nid):
    n = st.get(nid)
    return n["type"] if n else nid.split(":", 1)[0]


def node_history(st, node_ids: list[str]) -> list[dict]:
    ids = set(node_ids)
    out = []
    for e in st.changelog:
        if e["node"] not in ids:
            continue
        t = _ntype(st, e["node"])
        if e["op"] in ("create", "update"):
            what = diff_summary(t, e.get("before"), e.get("after"))
        elif e["op"] == "status":
            what = [f"status: {e.get('before')} → {e.get('after')}"]
        elif e["op"] == "flag":
            what = [f"flag {(e.get('after') or {}).get('id', '')} raised"]
        elif e["op"] == "resolve_flag":
            what = [f"flag {e.get('before')} resolved"]
        elif e["op"] in ("comment", "resolve_comment"):
            what = [e["op"].replace("_", " ")]
        else:
            what = [e["op"]]
        out.append({"ts": e["ts"], "node": e["node"], "op": e["op"], "actor": e["actor"],
                    "reason": str(e.get("reason") or "")[:600], "changes": what})
    return out


def _changesets(ctx: Ctx, rid: str | None = None) -> list[dict]:
    d = ctx.run_dir / "feedback"
    out = []
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            cs = json.loads(p.read_text())
        except ValueError:
            continue
        nodes = [c["node"] for c in cs.get("changes", [])]
        if rid and not (cs.get("rid") == rid or (cs.get("target") and study_of(cs["target"]) == rid)
                        or any(study_of(x) == rid for x in nodes)):
            continue
        out.append({"id": cs["id"], "kind": cs.get("kind", "feedback"), "status": cs.get("status"),
                    "target": cs.get("target"), "request": cs.get("feedback", ""), "verdict": cs.get("verdict"),
                    "explanation": cs.get("explanation", ""), "model": cs.get("model", ""),
                    "cost_usd": cs.get("cost_usd"), "created": cs.get("created"),
                    "changes": [{"node": c["node"], "role": c.get("role"),
                                 "what": diff_summary(_ntype(ctx.store, c["node"]), c.get("before"), c.get("after"))}
                                for c in cs.get("changes", [])]})
    return sorted(out, key=lambda x: -(x["created"] or 0))


def _ev(x: dict) -> list[dict]:
    return [{"sid": s.get("sid"), "section": s.get("section"), "text": s.get("text", "")[:500]}
            for s in (x.get("spans") or [])]


def study_detail(ctx: Ctx, rid: str, with_text: bool = False) -> dict:
    st = ctx.store
    rec = record_of(st, rid)
    if not rec:
        raise KeyError(rid)
    crit = criteria_by_id(st)
    g, s, why = status_of(st, rid)
    ftn, sc, ext, rob = (st.get(f"{k}:{rid}") for k in ("ft", "screen", "ext", "rob"))
    out = {"rid": rid, "group": g, "status": s, "reason": why,
           "record": {k: rec.get(k) for k in ("title", "authors", "journal", "year", "pmid", "doi", "pmcid",
                                              "pub_types", "abstract", "source")},
           "links": links(rec, (ftn or {}).get("data")),
           "pdf_wanted": bool(st.meta.get("pdf_wanted", {}).get(rid)),
           "pdf_unavailable": rid in st.meta.get("pdf_unavailable", [])}
    if ftn:
        f = ftn["data"]
        out["source"] = {k: f.get(k) for k in ("kind", "origin", "pmcid", "chars", "local_file")}
        if with_text:
            out["source"]["text"] = f.get("text", "")
    if sc:
        d = sc["data"]
        out["screening"] = {
            "decision": d["decision"], "stage": d.get("stage"), "because": d.get("because", []),
            "rule": d.get("rule"), "rule_reason": d.get("rule_reason"), "human_decision": d.get("human_decision"),
            "llm_decision": d.get("llm_decision"), "llm_reason": d.get("reason"), "version": sc["version"],
            "title_abstract": d.get("title_abstract"),
            "criteria": [{"id": a.get("id"), "type": (crit.get(a.get("id")) or {}).get("type"),
                          "text": (crit.get(a.get("id")) or {}).get("text", ""), "assessment": a.get("assessment"),
                          "reason": a.get("reason") or "", "model_assessment": a.get("model_assessment"),
                          "settled": a.get("settled"),
                          "evidence": _ev(a), "check": (a.get("check") or {}).get("status")}
                         for a in d.get("criteria", [])]}
    if ext:
        out["extraction"] = {"status": ext["status"], "version": ext["version"], "source_kind": ext["data"].get("source_kind"),
                             "fields": [{"name": k, "value": v.get("value"), "evidence": _ev(v),
                                         "check": (v.get("check") or {}).get("status"), "terms": v.get("terms", []),
                                         "edited": v.get("edited")} for k, v in ext["data"]["fields"].items()]}
    if rob:
        out["rob"] = {"status": rob["status"], "version": rob["version"], "overall": rob["data"].get("overall"),
                      "confirmed": rob["data"].get("human_confirmed"),
                      "domains": [{"domain": x.get("domain"), "judgment": x.get("judgment"), "rationale": x.get("rationale"),
                                   "judgment_agent": x.get("judgment_agent"), "human_note": x.get("human_note"),
                                   "evidence": _ev(x)} for x in rob["data"]["domains"]]}
    out["synthesis"] = [{"id": n["id"], "text": n["data"]["text"], "cites": n["data"]["cites"],
                         "support": (n["data"].get("verification") or {}).get("support")}
                        for n in st.by_type("synthesis", active_only=True) if rid in n["data"]["cites"]]
    nodes = _study_nodes(st, rid)
    out["flags"] = _flags_of(st, rid)
    out["comments"] = _comments(st, {f"study:{rid}"} | set(nodes))
    out["proposals"] = _changesets(ctx, rid)
    out["history"] = node_history(st, nodes)
    out["nodes"] = nodes
    return out


def study_markdown(d: dict) -> str:
    r = d["record"]
    L = [f"# [{d['rid']}] {r.get('title')}", "",
         f"{r.get('authors', '')}. *{r.get('journal', '')}* {r.get('year', '')}.",
         " · ".join(f"[{k}]({v})" for k, v in d["links"].items()), "",
         f"**Status:** {d['status']}" + (f" ({d['reason']})" if d.get("reason") else "")]
    if d.get("source"):
        L.append(f"**Source used:** {d['source'].get('kind')} ({d['source'].get('origin') or d['source'].get('kind')}, {d['source'].get('chars')} chars)")
    if d.get("screening"):
        s = d["screening"]
        L += ["", "## Screening", f"Decision **{s['decision']}** at {s['stage']}; because {', '.join(s['because']) or '-'}"]
        if s.get("rule"):
            L.append(f"Rule: {s['rule']}: {s.get('rule_reason')}")
        if s.get("human_decision"):
            L.append(f"Your decision: {s['human_decision']}")
        for c in s["criteria"]:
            ev = " ".join(f"[{e['sid']}] “{e['text'][:200]}”" for e in c["evidence"])
            L.append(f"- {c['id']} ({c['type']}) **{c['assessment']}**: {c['text']}  {ev}")
    if d.get("extraction"):
        L += ["", "## Extracted data", "| Item | Value | Evidence |", "|---|---|---|"]
        for f in d["extraction"]["fields"]:
            ev = "<br>".join(f"{e['sid']} ({e['section']}): “{e['text'][:200]}”" for e in f["evidence"])
            v = str(f["value"]).replace("|", "/").replace("\n", " ")
            if f.get("edited"):
                v += f" *(edited by you; was: {str(f['edited'].get('before'))[:80]})*"
            L.append(f"| {f['name']} | {v} | {ev.replace('|', '/')} |")
    if d.get("rob"):
        L += ["", f"## Risk of bias (overall {d['rob']['overall']}{', confirmed' if d['rob'].get('confirmed') else ', draft'})"]
        for x in d["rob"]["domains"]:
            L.append(f"- {x['domain']}: **{x['judgment']}**. {x.get('rationale') or ''}")
    if d.get("synthesis"):
        L += ["", "## Cited in the synthesis"] + [f"- {s['id']}: {s['text']}" for s in d["synthesis"]]
    if d.get("flags"):
        L += ["", "## Flags"] + [f"- {f['id']} [{f['status']}] {f['node']} ({f.get('source')}): {f['text']}"
                                 + (f" → {f['resolution']}" if f.get("resolution") else "") for f in d["flags"]]
    if d.get("comments"):
        L += ["", "## Comments"] + [f"- {c['id']} [{c['status']}] on {c['target']}: {c['text']}" for c in d["comments"]]
    if d.get("proposals"):
        L += ["", "## Change proposals"] + [f"- {p['id']} ({p['kind']}, {p['status']}): {p['request'][:200]}" for p in d["proposals"]]
    if d.get("history"):
        L += ["", "## History"] + [f"- {time.strftime('%Y-%m-%d %H:%M', time.localtime(h['ts']))} {h['node']} {h['op']} "
                                   f"by {h['actor']}: {'; '.join(h['changes'])} ({h['reason'][:160]})" for h in d["history"]]
    return "\n".join(L) + "\n"


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    for r in rows:
        w.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in r.items()})
    return buf.getvalue()


def review_export(ctx: Ctx, with_text: bool = False) -> dict:
    st = ctx.store
    return {"exported": time.time(), "run": ctx.run_dir.name,
            "protocol": (st.get("protocol") or {}).get("data"),
            "synthesis": [{"id": n["id"], **{k: n["data"].get(k) for k in ("text", "cites", "scope")},
                           "support": (n["data"].get("verification") or {}).get("support"), "version": n["version"]}
                          for n in st.by_type("synthesis", active_only=True)],
            "comments": [c for c in st.meta.get("comments", []) if not c["target"].startswith(("study:", "rec:", "screen:", "ft:", "ext:", "rob:"))],
            "studies": [study_detail(ctx, r["rid"], with_text) for r in studies_list(ctx)]}


def _jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def history(ctx: Ctx, limit: int = 400, actor: str = "", q: str = "") -> dict:
    st = ctx.store
    log = [e for e in st.changelog if (not actor or e["actor"] == actor) and (not q or q in e["node"] or q in str(e.get("reason", "")))]
    log = node_history(st, list({e["node"] for e in log}))
    if actor:
        log = [e for e in log if e["actor"] == actor]
    if q:
        log = [e for e in log if q in e["node"] or q in e["reason"]]
    usage = _jsonl(ctx.run_dir / "usage.jsonl")
    by_tag = {}
    for u in usage:
        t = by_tag.setdefault(u.get("tag", ""), {"tag": u.get("tag", ""), "calls": 0, "cached": 0, "usd": 0.0, "last": 0})
        t["calls"] += 1
        t["cached"] += int(bool(u.get("cached")))
        t["usd"] += float(u.get("usd", 0))
        t["last"] = max(t["last"], u.get("ts", 0))
    return {"changelog": log[-limit:][::-1], "changelog_total": len(log),
            "proposals": _changesets(ctx),
            "decisions": _jsonl(ctx.run_dir / "human_decisions.jsonl")[::-1][:limit],
            "comments": st.meta.get("comments", [])[::-1],
            "llm_by_tag": sorted(by_tag.values(), key=lambda x: -x["last"])}
