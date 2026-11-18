"""Stage 6: narrative synthesis as citeable statements, each verified against cited data."""
from __future__ import annotations

import json
import time
import re

from .. import prompts, schemas
from ..context import Ctx
from .extraction import table_text
from .rob import rob_text
from .. import selection


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
        old = st.get(sid)
        if old is not None and old["status"] != "active":   # an id of a retired statement: give the new one a fresh id
            k = max([int(x["data"]["id"][1:]) for x in st.by_type("synthesis") if str(x["data"].get("id", ""))[1:].isdigit()] + [0]) + 1
            s = {**s, "id": f"S{k}"}
            sid = f"syn:{s['id']}"
        keep.add(sid)
        cites = [str(c).strip("[] ") for c in s.get("cites", [])]
        cites, fixes = reconcile_citations(st, s["text"], cites)
        data = {"id": s["id"], "text": s["text"], "cites": cites, "scope": s.get("scope", "")}
        for k in ("pooled", "basis"):         # the pooled result a statement reports, and what it was written from (its check reads it)
            if s.get(k):
                data[k] = s[k]
        if fixes:
            data["citation_fixes"] = fixes
        old = st.get(sid)
        if old is None or {k: old["data"].get(k) for k in ("id", "text", "cites", "scope")} != \
                {k: data[k] for k in ("id", "text", "cites", "scope")}:
            st.write(sid, "synthesis", data, depends_on=statement_deps(st, cites), reason=reason, actor=actor)
            if retire_if_uncited(st, sid, bool(old and old["data"].get("cites"))):
                keep.discard(sid)
    return keep


def not_retrieved(st) -> int:
    """Forwarded reports with no usable full text (not retrieved, or an unreadable file)."""
    return sum(1 for n in selection.study_nodes(st) if n["data"]["decision"] in ("fulltext_unavailable", "parse_failed"))


def coverage_note(st) -> str:
    """One sentence for the report: what the synthesis rests on and what was missing."""
    from .. import state
    w = (st.get(state.SCOPE) or {}).get("data", {}).get("written_with") or {}
    now = not_retrieved(st)
    inc = len(st.included_ids())
    parts = []
    if w:
        parts.append(f"The synthesis was first written on {w.get('included')} included studies while {w.get('not_retrieved')} "
                     f"forwarded reports had no full text ({w.get('when')})")
        if inc != w.get("included") or now != w.get("not_retrieved"):
            parts.append(f"it now rests on {inc} included studies with {now} reports still not retrieved")
    elif now:
        parts.append(f"{now} forwarded reports had no full text when this report was made")
    return ("; ".join(parts) + ".") if parts else ""


def pooled_text(g: dict) -> str:
    """One pooled group as text for a prompt: the estimate, its interval, heterogeneity and the sensitivity analyses."""
    P = g["pooled"]
    L = [f"{' · '.join(g['group'].values())} ({g['measure']}, treated / control): pooled ratio {P['ratio']:.2f} "
         f"(95% CI {P['ratio_ci'][0]:.2f} to {P['ratio_ci'][1]:.2f}), {P['k']} studies, random effects with the Hartung-Knapp interval; "
         f"I² = {P['i2']:.0f}%" + (f"; 95% prediction interval {P['ratio_prediction'][0]:.2f} to {P['ratio_prediction'][1]:.2f}"
                                  if P.get("ratio_prediction") else "") + "."]
    for name, x in (g.get("sensitivity") or {}).items():
        L.append(f"Sensitivity, {name}: " + (f"ratio {x['ratio']:.2f} (95% CI {x['ratio_ci'][0]:.2f} to {x['ratio_ci'][1]:.2f}), {x['k']} studies."
                                             if "ratio" in x else f"only {x['k']} studies left, not pooled."))
    n_est = sum(1 for s in g["studies"] if s["estimated"])
    if n_est:
        L.append(f"{n_est} of {P['k']} studies contribute values estimated from a figure (not reported numbers).")
    return "\n".join(L)


def pooled_sentence(g: dict) -> str:
    """The pooled finding of a group in plain words, written by rule from the computed result (no model call): what a
    statement falls back to when the pooled numbers change after it was written."""
    P = g["pooled"]
    r, (lo, hi) = P["ratio"], P["ratio_ci"]
    what = " · ".join(g["group"].values())
    size = "the same as" if abs(r - 1) < 0.005 else f"{abs(r - 1) * 100:.0f}% {'lower' if r < 1 else 'higher'} than"
    s = [f"{what}: across {P['k']} studies the pooled {g['measure']} (treated / control) was {r:.2f} (95% CI {lo:.2f} to {hi:.2f}), "
         f"that is {size} in controls; the interval {'includes' if lo <= 1 <= hi else 'excludes'} no effect."]
    het = "consistent" if P["i2"] < 30 else "moderately inconsistent" if P["i2"] < 60 else "inconsistent" if P["i2"] < 90 else "highly inconsistent"
    pr = P.get("ratio_prediction")
    s.append(f"The studies were {het} (I² = {P['i2']:.0f}%" + (f"; 95% prediction interval {pr[0]:.2f} to {pr[1]:.2f}" if pr else "") + ").")
    for name, x in (g.get("sensitivity") or {}).items():
        lead = {"reported numbers only": "With reported numbers only (no values estimated from figures)",
                "without high risk of bias": "Without the studies at high risk of bias"}.get(name, f"Sensitivity ({name})")
        s.append(f"{lead}, " + (f"{x['k']} studies gave {x['ratio']:.2f} (95% CI {x['ratio_ci'][0]:.2f} to {x['ratio_ci'][1]:.2f})."
                                     if "ratio" in x else f"only {x['k']} {'study was' if x['k'] == 1 else 'studies were'} left, too few to pool."))
    n_est = sum(1 for x in g["studies"] if x["estimated"])
    if n_est:
        s.append(f"{n_est} of {P['k']} studies contribute values estimated from a figure, not reported numbers.")
    return " ".join(s)


def group_basis(ctx: Ctx, plan: dict, g: dict) -> tuple[str, str, str]:
    """What a pooled group's statements rest on, as text: (pooled result, its studies with their effects and rows,
    their risk of bias). The writer and the check both get exactly this."""
    from .. import rows
    st = ctx.store
    cols = rows.columns_of(next(f for f in ctx.topic["extraction_fields"] if f["name"] == plan["field"]))
    lines = []
    for s in g["studies"]:
        got = (st.get(f"ext:{s['rid']}")["data"]["fields"][plan["field"]].get("rows")) or []
        lines.append(f"[{s['rid']}] {s['label']}: ratio {s['ratio']:.2f} (95% CI {s['ratio_ci'][0]:.2f} to {s['ratio_ci'][1]:.2f})"
                     + (f", weight {s['weight']:.0f}%" if "weight" in s else "") + (", estimated from a figure" if s["estimated"] else "")
                     + (", high risk of bias" if s["high_risk"] else "") + ("; " + "; ".join(s["notes"]) if s["notes"] else ""))
        lines += [f"    row {i}: {rows.row_text(got[i - 1], cols)}" for i in s["rows"] if i <= len(got)]
    return pooled_text(g), "\n".join(lines), rob_text(ctx, [s["rid"] for s in g["studies"]]) or "(not assessed)"


def unpooled_basis(ctx: Ctx, plan: dict, res: dict) -> tuple[str, str, str] | None:
    """The groups that were not pooled, as text: per group the studies by direction of effect; why rows could not be
    used; the names of the pooled groups. None when the plan names no direction column or nothing is left."""
    from collections import defaultdict
    from .. import meta
    st = ctx.store
    dcol = (plan.get("columns") or {}).get("direction")
    if not dcol:
        return None
    pooled = [g for g in res["groups"] if g["pooled"]]
    done = {tuple(g["group"].values()) for g in pooled}
    votes: dict = defaultdict(lambda: defaultdict(set))
    for rid in st.included_ids():
        ext = st.get(f"ext:{rid}")
        for r in ((ext["data"]["fields"].get(plan["field"]) or {}).get("rows") or []) if ext and ext["status"] == "active" else []:
            if meta.left_out(r, plan) or not r.get(dcol):
                continue
            key = tuple(meta.refined(r, plan).values())
            if key not in done:
                votes[key][r[dcol]].add(rid)
    lines = [f"- {' · '.join(k)}: " + "; ".join(f"{d}: {len(v)} ({', '.join(sorted(v)[:8])}{', …' if len(v) > 8 else ''})"
                                                for d, v in sorted(by.items(), key=lambda x: -len(x[1])))
             for k, by in sorted(votes.items(), key=lambda x: -len(set().union(*x[1].values()))) if len(set().union(*by.values())) >= 2]
    if not lines:
        return None
    return ("\n".join(lines[:80]), "; ".join(f"{k}: {v}" for k, v in res["not_used"].items()),
            "; ".join(" · ".join(g["group"].values()) for g in pooled) or "none")


POOLED_BASIS = "POOLED RESULT (computed by code; its numbers are correct by construction):\n{}\n\nSTUDIES IN THIS GROUP " \
               "(effect of each; the extracted rows it comes from):\n{}\n\nRISK OF BIAS (overall per study):\n{}"
UNPOOLED_BASIS = "OUTCOME GROUPS NOT POOLED (studies by direction of effect, with their ids):\n{}\n\nWHY ROWS COULD NOT BE USED " \
                 "IN THE POOLING (rows, by reason): {}\n\nPOOLED GROUPS: {}"


def _grouped(ctx: Ctx, plan: dict) -> list[dict]:
    """A review with a meta-analysis plan: one call per pooled group (its pooled result, its studies' rows), and one
    for what could not be pooled. The whole extraction table is never sent in one prompt. Each statement keeps what
    it was written from (`basis`), which is what its check and repair read."""
    from .. import meta
    res = meta.analyse(ctx)
    pooled = [g for g in res["groups"] if g["pooled"]]

    def one(g):
        ptxt, studies, rob = group_basis(ctx, plan, g)
        rids = [s["rid"] for s in g["studies"]]
        out = ctx.writer.chat(ctx.msgs(prompts.SYNTH_POOLED.format(
            question=ctx.topic["question"], group=" · ".join(g["group"].values()) + f" ({g['measure']})", pooled=ptxt,
            studies=studies, rob=rob)), tag="synthesis")
        return [{**s, "pooled": ptxt, "basis": POOLED_BASIS.format(ptxt, studies, rob),
                 "cites": [c for c in (s.get("cites") or rids) if str(c).strip("[] ") in rids] or rids}
                for s in out.get("statements", []) if isinstance(s, dict) and s.get("text")]
    statements = [s for ss in ctx.map(one, pooled, label="Writing the synthesis") for s in ss]
    ub = unpooled_basis(ctx, plan, res)
    if ub:
        out = ctx.writer.chat(ctx.msgs(prompts.SYNTH_UNPOOLED.format(question=ctx.topic["question"], groups=ub[0], not_used=ub[1],
                                                                    pooled=ub[2])), tag="synthesis")
        statements += [{**s, "basis": UNPOOLED_BASIS.format(*ub)} for s in out.get("statements", []) if isinstance(s, dict) and s.get("text")]
    return [{**s, "id": f"S{i}"} for i, s in enumerate(statements, 1)]


def synthesize(ctx: Ctx) -> int:
    from .. import meta
    plan = meta.plan_of(ctx.topic)
    if plan:
        statements = _grouped(ctx, plan)
    else:
        out = ctx.writer.chat(ctx.msgs(prompts.SYNTH.format(question=ctx.topic["question"], table=table_text(ctx),
                                                           rob=rob_text(ctx))), tag="synthesis")
        statements = out.get("statements", [])
    keep = write_statements(ctx, statements, "synthesis drafted")
    for n in ctx.store.by_type("synthesis", active_only=True):   # a re-written synthesis replaces the old one
        if n["id"] not in keep:
            ctx.store.set_status(n["id"], "superseded", reason="synthesis re-written")
    from .. import state
    state.set_considered(ctx.store, ctx.store.included_ids(), written_with={
        "included": len(ctx.store.included_ids()), "not_retrieved": not_retrieved(ctx.store), "when": time.strftime("%Y-%m-%d %H:%M")})
    ctx.log(f"[synthesis] {len(statements)} statements")
    if ctx.cfg["pipeline"].get("verify_synthesis", True):
        verify_all(ctx)
        if ctx.cfg["pipeline"].get("repair_synthesis", True):
            repair_all(ctx)
    ctx.save()
    return len(statements)


# a claim that something is absent from the included studies' data (then every study's data is shown to the check);
# hedges such as "evidence is limited" or "few studies" are not absence claims
ABSENCE = re.compile(r"\b(not reported|not available|not provided|not stated|not measured|not estimated|"
                     r"no (data|stud(y|ies)|estimates?|values?|parameters?) (were |was |are |is )?(reported|available|provided|identified|found)|"
                     r"none of the (included )?studies|in (none|no) (of the )?(included )?stud(y|ies))\b", re.I)


def statement_evidence(ctx: Ctx, cites: list[str], text: str = "", basis: str = "") -> str:
    """The cited studies' data with quotes; for a statement that says something is absent or not reported,
    every included study's data as well, so the check can find what the statement says is missing."""
    if basis:      # a statement of a grouped synthesis: judged on exactly what it was written from
        return basis
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
    res = ctx.hub.model("synthesis", "verifier").chat(ctx.msgs(prompts.SYNTH_VERIFY.format(sid=d["id"], text=d["text"],
                                                                 evidence=statement_evidence(ctx, d["cites"], d["text"], d.get("basis", "")))), tag=tag, schema=schemas.SYNTH_VERIFY)
    n["data"]["verification"] = res
    if res.get("support") in ("unsupported", "partially_supported") or res.get("overgeneralization"):
        st.flag(sid, f"Self-check: {res.get('support')}; overgeneralization={res.get('overgeneralization')}; "
                f"{'; '.join(res.get('issues', []))[:300]}", actor="agent", source="synthesis_verify")
    return res


def needs_repair(res: dict | None) -> bool:
    return bool(res) and (res.get("support") in ("unsupported", "partially_supported") or bool(res.get("overgeneralization")))


def repair_one(ctx: Ctx, sid: str) -> str:
    """One repair of a statement the self-check faulted: the stage model narrows it to what the cited data show
    (or retires it), then the checker looks again. Still faulted after that: it stays, flagged by the second check
    (the checker is one model's opinion; a person decides). Returns what happened."""
    st = ctx.store
    n = st.get(sid)
    d = n["data"]
    res = d.get("verification") or {}
    issues = f"{res.get('support')}; overgeneralization={res.get('overgeneralization')}; " + "; ".join(res.get("issues") or [])
    out = ctx.writer.chat(ctx.msgs(prompts.SYNTH_REPAIR.format(sid=d["id"], text=d["text"], scope=d.get("scope", ""),
                                                               cites=", ".join(d["cites"]), issues=issues,
                                                               evidence=statement_evidence(ctx, d["cites"], d["text"], d.get("basis", "")))),
                         tag="synthesis_repair")
    for f in n["flags"]:
        if f["status"] == "open" and f.get("source") == "synthesis_verify":
            st.resolve_flag(sid, f["id"], "repaired after the self-check", actor="agent")
    if out.get("retire") or not out.get("text"):
        st.set_status(sid, RETIRED, reason=f"retired after the self-check: {str(out.get('why') or 'no supported version')[:200]}")
        return "retired"
    cites = [str(c).strip("[] ") for c in out.get("cites") or d["cites"]]
    cites = [c for c in cites if c in d["cites"]] or list(d["cites"])      # no study the check did not see
    cites, fixes = reconcile_citations(st, out["text"], cites)
    data = {"id": d["id"], "text": str(out["text"]), "cites": cites, "scope": str(out.get("scope") or d.get("scope", "")),
            "repaired_from": {"text": d["text"], "cites": d["cites"], "check": res}}
    for k in ("pooled", "basis"):
        if d.get(k):
            data[k] = d[k]
    if fixes:
        data["citation_fixes"] = fixes
    st.write(sid, "synthesis", data, depends_on=statement_deps(st, cites), reason="repaired after the self-check")
    if retire_if_uncited(st, sid, True):
        return "retired"
    again = verify_one(ctx, sid, tag="synthesis_verify_repair")   # still faulted: it stays, with the check's flag open
    return "repaired" if not needs_repair(again) else f"repaired_{again.get('support')}"


def repair_all(ctx: Ctx) -> dict:
    """One repair round: every active statement the self-check faulted is repaired once (or retired)."""
    sids = [n["id"] for n in ctx.store.by_type("synthesis", active_only=True) if needs_repair(n["data"].get("verification"))]
    if not sids:
        return {}
    res = ctx.map(lambda s: repair_one(ctx, s), sids)
    c = {}
    for r in res:
        c[r] = c.get(r, 0) + 1
    ctx.log(f"[synthesis] repair round on {len(sids)} faulted statements: {c}")
    return c


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
