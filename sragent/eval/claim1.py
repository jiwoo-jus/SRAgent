"""Claim 1 (LLM-simulated): source-linked evidence and intermediate decisions help a reviewer find
and correct review errors better than a final report alone.

Following the advisor's suggestion, the primary signal comes from an LLM reviewer + LLM judge;
human sessions are optional (materials are exported for a light human study).

Design
  * Two error sets (A, B), each planting one error of each type + extra extraction errors into a
    copy of the finished review (disjoint studies). Gold corrections are known by construction.
  * Two conditions with the same underlying review and the same source papers:
      report_only     report.md + source papers
      evidence_linked report.md + evidence bundle (quotes, quote checks, criterion-level decisions,
                      RoB rationales, synthesis citations/self-checks, open flags) + source papers
  * Every set is reviewed in both conditions (full crossing; for an LLM there is no learning effect,
    so counterbalancing reduces to crossing). Repeat with `claim1_repeats` > 1 for variance.

Measures (per condition): recall of planted errors, repair rate (correct correction), precision of
flags (a flag counts as correct if it matches a planted error OR the judge confirms it is a genuine
natural error), false alarms on accurate content, tokens / seconds per correctly resolved error.
"""
from __future__ import annotations

import json
import random
import time

from .. import prompts
from ..context import Ctx
from ..stages import report
from .common import Judge, save, short, wilson
from . import inject


def make_set(base: Ctx, rnd: random.Random, rids: list[str], sids: list[str]) -> tuple[Ctx, list[dict]]:
    import dataclasses
    ctx = dataclasses.replace(base, store=base.store.clone())
    planted = []
    pool = list(rids)
    rnd.shuffle(pool)
    # 2 extraction errors, 1 eligibility, 1 RoB (different studies), 1 synthesis
    for rid in pool:
        if sum(p["type"] == "extraction" for p in planted) >= 2:
            break
        e = inject.inject_extraction(ctx, rid, rnd)
        if e:
            planted.append(e)
    used = {p["node"].split(":", 1)[1] for p in planted}
    for rid in pool:
        if rid not in used:
            e = inject.inject_eligibility(ctx, rid, structural=True)
            if e:
                planted.append(e)
                used.add(rid)
                break
    for rid in pool:
        if rid not in used:
            e = inject.inject_rob(ctx, rid, rnd)
            if e:
                planted.append(e)
                used.add(rid)
                break
    for sid in sids:
        e = inject.inject_synthesis(ctx, sid)
        if e:
            planted.append(e)
            break
    for i, p in enumerate(planted):
        p["id"] = f"P{i + 1}"
    return ctx, planted


def materials(ctx: Ctx, condition: str, source_rids: list[str], max_src_chars: int) -> str:
    parts = ["## REPORT", report.report_markdown(ctx)]
    if condition == "evidence_linked":
        parts.append(report.evidence_text(ctx, max_chars_per_study=max_src_chars))   # same cap as the source papers
    parts.append(report.sources_text(ctx, source_rids, max_src_chars))
    return "\n\n".join(parts)


def fit_cap(ctx: Ctx, source_rids: list[str], max_src_chars: int, max_total_chars: int) -> int:
    """The per-study cap both conditions use: max_src_chars, lowered until the larger bundle (evidence-linked)
    fits max_total_chars. One cap for both conditions, so they differ in what the reviewer sees, not in how much
    of each study, and neither exceeds what the reviewer model can read."""
    cap = max_src_chars
    while max_total_chars and cap > 500 and len(materials(ctx, "evidence_linked", source_rids, cap)) > max_total_chars:
        cap = int(cap * 0.7)
    return cap


def _loc_match(planted: dict, flag: dict) -> bool:
    loc = (flag.get("location") or "") + " " + (flag.get("description") or "")
    key = planted["node"].split(":", 1)[1]  # study id or statement id
    if planted["type"] == "synthesis":
        return bool(__import__("re").search(rf"\b{key}\b", loc))
    return key in loc


def score(ctx: Ctx, judge: Judge, planted: list[dict], flags: list[dict], gold_ctx: Ctx,
          max_genuine_checks: int, meta: dict) -> dict:
    used_flags = set()
    per = []
    for p in planted:
        cands = [(i, f) for i, f in enumerate(flags) if _loc_match(p, f)]
        res = {"planted": p["id"], "type": p["type"], "identified": False, "correction_correct": False}
        if cands:
            rep = "\n".join(f"- [{f.get('location')}] {f.get('description')} | proposed: {f.get('proposed_correction')}"
                            for _, f in cands)
            j = judge("match", prompts.JUDGE_MATCH.format(
                planted=f"{p['description']} Location: {p['location']}. Corrupted content: {short(p['corrupted'], 600)}",
                gold=short(p["gold"], 800), report=rep), "identifies_error", {**meta, "planted": p["id"]})
            res["identified"] = bool(j.get("identifies_error"))
            res["correction_correct"] = bool(j.get("correction_correct")) and res["identified"]
            if res["identified"]:
                used_flags.update(i for i, _ in cands)
        per.append(res)
    # unmatched flags: genuine natural errors or false alarms?
    genuine = 0
    checked = 0
    unmatched = [f for i, f in enumerate(flags) if i not in used_flags]
    for f in unmatched[:max_genuine_checks]:
        loc = f.get("location", "")
        content, evidence = _content_at(gold_ctx, loc)
        j = judge("genuine", prompts.JUDGE_GENUINE.format(report=json.dumps(f, ensure_ascii=False),
                                                         content=short(content, 2500), evidence=short(evidence, 6000)),
                  "genuine_error", meta)
        checked += 1
        genuine += bool(j.get("genuine_error"))
    n_flags = len(flags)
    n_ident = sum(r["identified"] for r in per)
    n_rep = sum(r["correction_correct"] for r in per)
    # precision: matched flags + genuine (extrapolated over unchecked unmatched flags)
    gen_rate = genuine / checked if checked else 0.0
    est_genuine = gen_rate * len(unmatched)
    return {"per_error": per, "n_planted": len(planted), "n_flags": n_flags,
            "recall": round(n_ident / len(planted), 3) if planted else None,
            "recall_95ci": wilson(n_ident, len(planted)),
            "repair_rate": round(n_rep / len(planted), 3) if planted else None,
            "unmatched_flags": len(unmatched), "unmatched_checked": checked, "unmatched_genuine": genuine,
            "precision_est": round((len(used_flags) + est_genuine) / n_flags, 3) if n_flags else None,
            "false_alarms_est": round(len(unmatched) - est_genuine, 2)}


def _content_at(ctx: Ctx, loc: str) -> tuple[str, str]:
    """Find review content + source evidence for a free-text location like 'ext:123 pk_results' or 'S3'."""
    import re
    st = ctx.store
    m = re.search(r"\b(ext|rob|screen):([\w]+)", loc)
    if m:
        n = st.get(f"{m.group(1)}:{m.group(2)}")
        text, _ = st.text_of(m.group(2))
        return (json.dumps(n["data"], ensure_ascii=False, default=str) if n else ""), text
    m = re.search(r"\bS(\d+)\b", loc)
    if m and st.get(f"syn:S{m.group(1)}"):
        n = st.get(f"syn:S{m.group(1)}")
        from ..stages.extraction import table_text
        return n["data"]["text"], table_text(ctx, n["data"]["cites"], with_quotes=True)
    for rid in st.included_ids():
        if rid in loc:
            n = st.get(f"ext:{rid}")
            return json.dumps(n["data"]["fields"], ensure_ascii=False), st.text_of(rid)[0]
    return loc, ""


def run(ctx: Ctx, judge: Judge, out_dir, n_sets: int = 2, repeats: int = 1, max_src_chars: int = 5000,
        max_errors: int = 12, max_genuine_checks: int = 5, max_total_chars: int = 300000) -> dict:
    rnd = random.Random(ctx.cfg.get("eval", {}).get("seed", 2026))
    inc = ctx.store.included_ids()
    sids = [n["id"] for n in ctx.store.by_type("synthesis", active_only=True)]
    half = max(1, len(inc) // n_sets)
    results = {"sets": [], "conditions": {}}
    reviewer = ctx.hub["reviewer"]
    if not (ctx.cfg.get("roles") or {}).get("reviewer"):
        ctx.log("[claim1] WARNING: roles.reviewer is not set, so the simulated reviewer is the agent model; "
                "Claim 1 then measures that model's reading as much as the materials. Fix roles.reviewer across runs.")
    results["reviewer"] = {"model": reviewer.name, "configured": bool((ctx.cfg.get("roles") or {}).get("reviewer")),
                           "reasoning_effort": reviewer.c.get("reasoning_effort"),
                           "source_chars": max_src_chars, "total_chars_cap": max_total_chars}
    for s in range(n_sets):
        rids = inc[s * half:(s + 1) * half] if s < n_sets - 1 else inc[s * half:]
        sset = [sids[(s + k) % len(sids)] for k in range(len(sids))]
        cctx, planted = make_set(ctx, rnd, rids, sset)
        set_id = "ABCDEFGH"[s] if s < 8 else str(s)
        results["sets"].append({"set": set_id, "planted": planted})
        export_human_materials(cctx, out_dir, set_id)
        cap = fit_cap(cctx, inc, max_src_chars, max_total_chars)
        if cap != max_src_chars:
            ctx.log(f"[claim1] set {set_id}: per-study cap lowered to {cap} chars so the evidence-linked bundle fits {max_total_chars} chars")
        results["sets"][-1]["source_chars_used"] = cap
        for cond in ("report_only", "evidence_linked"):
            for r in range(repeats):
                mats = materials(cctx, cond, inc, cap)
                prompt = prompts.REVIEWER_TASK.format(materials=mats, max_errors=max_errors)
                t0 = time.time()
                usd0 = ctx.hub.tracker.spent
                temp = 0.0 if r == 0 else 0.7
                out = reviewer.chat([{"role": "user", "content": prompt}], tag=f"claim1_reviewer_{cond}",
                                    temperature=temp, use_cache=(r == 0))
                dt = time.time() - t0
                flags = (out.get("errors") if isinstance(out.get("errors"), list) else [])[:max_errors]
                reply_ok = isinstance(out.get("errors"), list)
                if not reply_ok:
                    ctx.log(f"[claim1] set {set_id} {cond} repeat {r}: the reviewer's reply had no 'errors' list; counted as 0 flags (reply_ok=false)")
                sc = score(cctx, judge, planted, flags, ctx, max_genuine_checks,
                           {"claim": 1, "set": set_id, "condition": cond, "repeat": r})
                sc.update({"set": set_id, "condition": cond, "repeat": r, "temperature": temp, "reviewer_seconds": round(dt, 1), "reply_ok": reply_ok,
                           "reviewer_usd": round(ctx.hub.tracker.spent - usd0, 5),
                           "materials_chars": len(mats), "flags": flags})
                n_rep = sum(x["correction_correct"] for x in sc["per_error"])
                sc["seconds_per_resolved_error"] = round(dt / n_rep, 1) if n_rep else None
                results["conditions"].setdefault(cond, []).append(sc)
                ctx.log(f"[claim1] set {set_id} {cond}: recall={sc['recall']} repair={sc['repair_rate']} "
                        f"flags={sc['n_flags']} precision~{sc['precision_est']}")
                save(out_dir, "claim1_partial", results)
    # aggregate
    agg = {}
    for cond, runs in results["conditions"].items():
        planted = sum(r["n_planted"] for r in runs)
        ident = sum(sum(x["identified"] for x in r["per_error"]) for r in runs)
        rep = sum(sum(x["correction_correct"] for x in r["per_error"]) for r in runs)
        by_type = {}
        for r in runs:
            for x in r["per_error"]:
                t = by_type.setdefault(x["type"], [0, 0])
                t[0] += x["identified"]
                t[1] += 1
        agg[cond] = {"planted": planted, "identified": ident, "recall": round(ident / planted, 3) if planted else None,
                     "recall_95ci": wilson(ident, planted), "repaired": rep,
                     "repair_rate": round(rep / planted, 3) if planted else None,
                     "precision_est_mean": round(sum(r["precision_est"] or 0 for r in runs) / len(runs), 3),
                     "false_alarms_est_total": round(sum(r["false_alarms_est"] for r in runs), 2),
                     "flags_total": sum(r["n_flags"] for r in runs),
                     "recall_by_type": {k: f"{v[0]}/{v[1]}" for k, v in by_type.items()},
                     "reviewer_usd_total": round(sum(r["reviewer_usd"] for r in runs), 4),
                     "reviewer_seconds_total": round(sum(r["reviewer_seconds"] for r in runs), 1),
                     "seconds_per_resolved_error": round(sum(r["reviewer_seconds"] for r in runs) / rep, 1) if rep else None,
                     "materials_chars_mean": int(sum(r["materials_chars"] for r in runs) / len(runs))}
    results["aggregate"] = agg
    results["design"] = design(results, repeats)
    results["paired"] = paired_analysis(results)
    return results


def design(results: dict, repeats: int) -> dict:
    planted = [p for s in results["sets"] for p in s["planted"]]
    papers = {p["node"].split(":", 1)[1] for p in planted if not p["node"].startswith("syn:")}
    stmts = {p["node"] for p in planted if p["node"].startswith("syn:")}
    return {"unique_errors": len(planted), "unique_papers_with_errors": len(papers), "unique_statements_with_errors": len(stmts),
            "error_sets": len(results["sets"]), "repeats_per_condition": repeats,
            "observations_per_condition": len(planted) * repeats,
            "errors_by_type": dict(__import__("collections").Counter(p["type"] for p in planted)),
            "same_errors_in_both_conditions": True,
            "note": "Each error set is one corrupted copy of the same review; both conditions review the same sets. "
                    "Repeats re-run the reviewer on the same materials (temperature 0, then 0.7) and are not independent."}


def paired_analysis(results: dict) -> dict:
    from .stats import mcnemar_exact, paired
    unit = {}   # condition -> unit -> list of (identified, repaired)
    obs = {}
    for cond, runs in results["conditions"].items():
        for r in runs:
            for x in r["per_error"]:
                u = f"{r['set']}:{x['planted']}"
                unit.setdefault(cond, {}).setdefault(u, []).append((x["identified"], x["correction_correct"]))
                obs[(cond, u, r["repeat"])] = x["identified"]
    if not {"report_only", "evidence_linked"} <= set(unit):
        return {}
    mean = lambda v, i: sum(t[i] for t in v) / len(v)
    a, b = unit["report_only"], unit["evidence_linked"]
    pairs = [(obs[("report_only", u, k)], obs[("evidence_linked", u, k)])
             for (c, u, k) in obs if c == "report_only" and ("evidence_linked", u, k) in obs]
    by_type = {}
    for s in results["sets"]:
        for p in s["planted"]:
            by_type.setdefault(p["type"], []).append(f"{s['set']}:{p['id']}")
    return {"unit": "unique planted error (repeats averaged within the error)",
            "detection": paired({u: mean(v, 0) for u, v in a.items()}, {u: mean(v, 0) for u, v in b.items()},
                                "report_only", "evidence_linked"),
            "repair": paired({u: mean(v, 1) for u, v in a.items()}, {u: mean(v, 1) for u, v in b.items()},
                             "report_only", "evidence_linked"),
            "detection_by_type": {t: paired({u: mean(a[u], 0) for u in us if u in a}, {u: mean(b[u], 0) for u in us if u in b},
                                            "report_only", "evidence_linked") for t, us in by_type.items()},
            "observation_level_mcnemar_reference_only": mcnemar_exact(pairs)}


def export_human_materials(cctx: Ctx, out_dir, set_id: str):
    """Optional light human study: the two interfaces of the same corrupted review, per set."""
    d = out_dir / "claim1_human_materials"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"set_{set_id}_report_only.md").write_text(report.report_markdown(cctx))
    (d / f"set_{set_id}_evidence_linked.html").write_text(report.report_html(cctx))
    (d / "README.txt").write_text(
        "Each set has a report-only and an evidence-linked version of the same corrupted review.\n"
        "Give each participant set A in one condition and set B in the other (counterbalance the order),\n"
        "with a fixed time budget, plus access to the source papers. Collect flags as\n"
        "{location, error_type, description, proposed_correction} so they can be scored with the same judge.\n"
        "The answer key is claim1.json -> sets[*].planted. Never show it to participants.\n")
