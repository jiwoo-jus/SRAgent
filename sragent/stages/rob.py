"""Stage 5: evidence-linked risk-of-bias drafts (for human review, never final)."""
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
        st.upsert(old["id"], "rob", d, depends_on=old["depends_on"], evidence=old["evidence"],
                  reason="source changed; the assessment a person confirmed is kept")
        if not any(f["status"] == "open" and f["source"] == "source_changed" for f in old["flags"]):
            st.flag(old["id"], "The full text changed after you confirmed this assessment; it was kept. "
                    "Ask for a re-assessment if the new text should change it.", actor="agent", source="source_changed")
        return d
    text, kind = st.text_of(rid)
    src_node = f"ft:{rid}" if st.get(f"ft:{rid}") else f"rec:{rid}"
    ext = st.get(f"ext:{rid}")
    ext_brief = json.dumps({k: v["value"] for k, v in ext["data"]["fields"].items()}, ensure_ascii=False)
    domains = "\n".join(f"- {d['name']}: {d['description']}" for d in ctx.topic["rob_domains"])
    out = ctx.agent.chat(ctx.msgs(prompts.ROB.format(domains=domains, extraction=ext_brief, kind=kind,
                                                     rid=rid, text=packed(text, _budget(ctx)))), tag="rob")
    evidence = []
    doms = [d for d in out.get("domains", []) if isinstance(d, dict)]
    for d in doms:
        cites = cites_of(d)
        d.pop("quote", None)
        evidence.append(attach(d, cites, text, f"{d.get('domain')}={d.get('judgment')}", src_node))
    out["domains"] = doms
    ftn = st.get(f"ft:{rid}")
    data = {"rid": rid, "tool": ctx.topic.get("rob_tool_name", "custom"), "source_kind": kind,
            "source_version": ftn["version"] if ftn else None,
            "domains": out.get("domains", []), "overall": out.get("overall", "unclear"),
            "status_note": "DRAFT - requires human confirmation"}
    from ..graph import domain_deps
    st.upsert(f"rob:{rid}", "rob", data, depends_on=[f"ext:{rid}", src_node] + domain_deps(ctx.cfg), evidence=evidence,
              reason="risk-of-bias draft")
    for d in data["domains"]:
        if d.get("judgment") in ("low", "high") and d["check"]["status"] == "none":
            st.flag(f"rob:{rid}", f"Judgment '{d.get('judgment')}' for '{d.get('domain')}' cites no source "
                    f"location.", actor="agent", source="evidence_check")
    return data


def rob_all(ctx: Ctx, only: list[str] | None = None) -> int:
    st = ctx.store
    from .extraction import source_changed
    rids = only or [rid for rid in st.included_ids() if st.get(f"ext:{rid}") and
                    (st.get(f"rob:{rid}") is None or source_changed(st, f"rob:{rid}"))]
    ctx.log(f"[rob] assessing {len(rids)} studies")
    ctx.map(lambda r: rob_one(ctx, r), rids)
    ctx.save()
    return len(rids)


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
