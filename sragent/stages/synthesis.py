"""Stage 6: narrative synthesis as citeable statements, each verified against cited data."""
from __future__ import annotations

import json
import re

from .. import prompts
from ..context import Ctx
from .extraction import table_text
from .rob import rob_text


def statement_deps(st, cites: list[str]) -> list[str]:
    deps = ["cfg:question"] if st.get("cfg:question") else []
    for rid in cites:
        for kind in ("ext", "rob"):
            if st.get(f"{kind}:{rid}"):
                deps.append(f"{kind}:{rid}")
    return deps


def reconcile_citations(st, text: str, cites: list[str]) -> tuple[list[str], list[str]]:
    """Deterministic citation guard: every included study id mentioned in the text must be in
    `cites`; ids of studies that are not (or no longer) included are dropped."""
    import re
    included = set(st.included_ids())
    mentioned = [m for m in re.findall(r"\b(\d{6,9}|d[0-9a-f]{10})\b", text) if m in included]
    fixes = []
    out = []
    for c in cites:
        if c in included and c not in out:
            out.append(c)
        elif c not in included:
            fixes.append(f"dropped non-included {c}")
    for m in mentioned:
        if m not in out:
            out.append(m)
            fixes.append(f"added {m} (mentioned in text)")
    return out, fixes


RETIRED = "retired"   # a statement that cited studies and has none left: no included study supports it


def retire_if_uncited(st, sid: str, had_cites: bool) -> bool:
    """A statement whose every cited study is gone is taken out of the synthesis (status retired),
    not left standing without evidence. Statements written without citations are left alone."""
    n = st.get(sid)
    if had_cites and n and n["status"] == "active" and not n["data"].get("cites"):
        st.set_status(sid, RETIRED, reason="no included study supports it any more (all cited studies removed)")
        return True
    return False


def write_statements(ctx: Ctx, statements: list[dict], reason: str, actor: str = "agent"):
    st = ctx.store
    keep = set()
    for s in statements:
        if not isinstance(s, dict) or not s.get("id") or not s.get("text"):
            continue
        sid = f"syn:{s['id']}"
        keep.add(sid)
        cites = [str(c).strip("[] ") for c in s.get("cites", [])]
        cites, fixes = reconcile_citations(st, s["text"], cites)
        data = {"id": s["id"], "text": s["text"], "cites": cites, "scope": s.get("scope", "")}
        if fixes:
            data["citation_fixes"] = fixes
        old = st.get(sid)
        if old is None or {k: old["data"].get(k) for k in ("id", "text", "cites", "scope")} != \
                {k: data[k] for k in ("id", "text", "cites", "scope")}:
            st.upsert(sid, "synthesis", data, depends_on=statement_deps(st, cites), reason=reason, actor=actor)
            if retire_if_uncited(st, sid, bool(old and old["data"].get("cites"))):
                keep.discard(sid)
    return keep


def synthesize(ctx: Ctx) -> int:
    out = ctx.agent.chat(ctx.msgs(prompts.SYNTH.format(question=ctx.topic["question"], table=table_text(ctx),
                                                       rob=rob_text(ctx))), tag="synthesis")
    statements = out.get("statements", [])
    keep = write_statements(ctx, statements, "synthesis drafted")
    for n in ctx.store.by_type("synthesis", active_only=True):   # a re-written synthesis replaces the old one
        if n["id"] not in keep:
            ctx.store.set_status(n["id"], "superseded", reason="synthesis re-written")
    ctx.store.meta["synthesis_considered"] = sorted(ctx.store.included_ids())
    ctx.log(f"[synthesis] {len(statements)} statements")
    if ctx.cfg["pipeline"].get("verify_synthesis", True):
        verify_all(ctx)
    ctx.save()
    return len(statements)


ABSENCE = re.compile(r"\b(not (reported|available|extract\w*|provided|stated|measured|assessed|estimated|given)|"
                     r"no (data|stud(y|ies)|estimates?|values?|parameters?|information)|unavailable|none of the|"
                     r"could not be|were not|was not)\b", re.I)


def checker(ctx: Ctx):
    """The model that checks statements: roles.verifier if configured, else the agent role itself (not the
    stage's model), so a synthesis written by a stronger stage model is checked by another model."""
    return ctx.hub["verifier"] if (ctx.cfg.get("roles") or {}).get("verifier") else ctx.hub["agent"]


def statement_evidence(ctx: Ctx, cites: list[str], text: str = "") -> str:
    """The cited studies' data with quotes; for a statement that says something is absent or not reported,
    every included study's data as well, so the check can find what the statement says is missing."""
    ev = table_text(ctx, cites, with_quotes=True) or "(no active cited studies)"
    if text and ABSENCE.search(text):
        others = [r for r in ctx.store.included_ids() if r not in cites]
        if others:
            ev += "\n\nALL OTHER INCLUDED STUDIES (the statement claims an absence; check these too):\n" + table_text(ctx, others)
    return ev


def verify_one(ctx: Ctx, sid: str, tag: str = "synthesis_verify") -> dict:
    st = ctx.store
    n = st.get(sid)
    d = n["data"]
    res = checker(ctx).chat(ctx.msgs(prompts.SYNTH_VERIFY.format(sid=d["id"], text=d["text"],
                                                                 evidence=statement_evidence(ctx, d["cites"], d["text"]))), tag=tag)
    n["data"]["verification"] = res
    if res.get("support") in ("unsupported", "partially_supported") or res.get("overgeneralization"):
        st.flag(sid, f"Self-check: {res.get('support')}; overgeneralization={res.get('overgeneralization')}; "
                f"{'; '.join(res.get('issues', []))[:300]}", actor="agent", source="synthesis_verify")
    return res


def verify_all(ctx: Ctx):
    sids = [n["id"] for n in ctx.store.by_type("synthesis", active_only=True)]
    res = ctx.map(lambda s: verify_one(ctx, s), sids)
    c = {}
    for r in res:
        c[r.get("support")] = c.get(r.get("support"), 0) + 1
    ctx.log(f"[synthesis] self-verification: {c}")


def statements_json(ctx: Ctx) -> str:
    return json.dumps([{k: n["data"][k] for k in ("id", "text", "cites", "scope")}
                       for n in ctx.store.by_type("synthesis", active_only=True)], ensure_ascii=False, indent=1)
