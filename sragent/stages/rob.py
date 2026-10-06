"""Stage 5: evidence-linked risk-of-bias drafts (for human review, never final)."""
from __future__ import annotations

import json

from .. import prompts
from ..context import Ctx
from ..evidence import attach, cites_of, numbered


def rob_one(ctx: Ctx, rid: str) -> dict:
    st = ctx.store
    text, kind = st.text_of(rid)
    src_node = f"ft:{rid}" if st.get(f"ft:{rid}") else f"rec:{rid}"
    ext = st.get(f"ext:{rid}")
    ext_brief = json.dumps({k: v["value"] for k, v in ext["data"]["fields"].items()}, ensure_ascii=False)
    domains = "\n".join(f"- {d['name']}: {d['description']}" for d in ctx.topic["rob_domains"])
    out = ctx.agent.chat(ctx.msgs(prompts.ROB.format(domains=domains, extraction=ext_brief, kind=kind,
                                                     rid=rid, text=numbered(text))), tag="rob")
    evidence = []
    doms = [d for d in out.get("domains", []) if isinstance(d, dict)]
    for d in doms:
        cites = cites_of(d)
        d.pop("quote", None)
        evidence.append(attach(d, cites, text, f"{d.get('domain')}={d.get('judgment')}", src_node))
    out["domains"] = doms
    data = {"rid": rid, "tool": ctx.topic.get("rob_tool_name", "custom"), "source_kind": kind,
            "domains": out.get("domains", []), "overall": out.get("overall", "unclear"),
            "status_note": "DRAFT - requires human confirmation"}
    st.upsert(f"rob:{rid}", "rob", data, depends_on=[f"ext:{rid}", src_node], evidence=evidence,
              reason="risk-of-bias draft")
    for d in data["domains"]:
        if d.get("judgment") in ("low", "high") and d["check"]["status"] == "none":
            st.flag(f"rob:{rid}", f"Judgment '{d.get('judgment')}' for '{d.get('domain')}' cites no source "
                    f"location.", actor="agent", source="evidence_check")
    return data


def rob_all(ctx: Ctx, only: list[str] | None = None) -> int:
    st = ctx.store
    rids = only or [rid for rid in st.included_ids() if st.get(f"rob:{rid}") is None and st.get(f"ext:{rid}")]
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
