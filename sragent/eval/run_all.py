"""Run the evaluation. Every invocation is one self-contained EVALUATION RUN:

  <run_dir>/eval/runs/<run_id>/
      results.json        everything this run measured (with its manifest); the only source of the report
      EVAL_REPORT.md      rendered from results.json (same object), never merged with older runs
      review_snapshot.json  the exact review that was evaluated
      usage.jsonl         this run's LLM calls only (cost and budget are per run)
      stagewise/          the gold-input review (separate copy; the real review is never written)
      ...                 judge logs, Claim 1 materials, Claim 2 R0 / R1 / R2 stores, human audit sheet

Suites
  e2e          end-to-end: the agent's own search results, real screening and full-text review (no LLM)
  screening    stage-wise screening on the benchmark candidate pool (input supplied; no LLM)
  stagewise    build the gold-input review (agent extraction, RoB, synthesis on the gold includes)
  grounding    stage-wise extraction / RoB evidence on the gold-input review (+ H6 term validation)
  synthesis    stage-wise synthesis from the judge-verified extraction table
  claim1       report-only vs evidence-linked review of planted errors (simulated reviewer)
  claim2       dependency-aware vs local-edit revision (R0 recovery and source correctness, separately)
  judge        judge validation: known-answer controls, second judge, human audit sheet (prepared only)
  all          all of the above in this order
  claim2_rescore  re-score a previous run's Claim 2 against THAT run's R0 (needs --from-run)
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

from .. import config_lock, gold, provenance
from ..provenance import file_sha
from ..config import role_cfg
from ..context import Ctx
from ..llm import BudgetExceeded, LLMHub, QuotaExhausted
from ..store import ReviewStore
from . import claim1, claim2, grounding_eval, judge_validation, screening_eval, stagewise, synthesis_eval
from .common import Judge, save

SUITES = ["e2e", "screening", "stagewise", "grounding", "synthesis", "claim1", "claim2", "judge"]
NEEDS_STAGEWISE = {"stagewise", "grounding", "synthesis", "claim1", "claim2", "judge"}


def code_version() -> str:
    h = hashlib.sha256()
    root = Path(__file__).resolve().parents[1]
    for p in sorted(root.rglob("*.py")):
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def _sanitize(cfg: dict) -> dict:
    def clean(x):
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if not k.startswith("_") and "key" not in k.lower() and "secret" not in k.lower()}
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x
    return clean(copy.deepcopy(cfg))


def eval_context(ctx: Ctx, out: Path, allow_cache: bool) -> Ctx:
    from ..stages.fulltext import fulltext_dir
    cfg = copy.deepcopy(ctx.cfg)
    cfg.setdefault("pipeline", {})["fulltext_dir"] = str(fulltext_dir(ctx))   # PDFs the person supplied
    cfg["budget_usd"] = float(cfg.get("eval", {}).get("budget_usd", 8.0))     # per evaluation run
    hub = LLMHub(cfg, out, mock_fn=ctx.hub.mock_fn)
    hub.cache_read = allow_cache
    return Ctx(cfg=cfg, run_dir=out, hub=hub, store=ctx.store, topic=ctx.topic, quiet=ctx.quiet)


def manifest(ctx: Ctx, ectx: Ctx, run_id: str, suite: str, allow_cache: bool, from_run: str | None, out: Path) -> dict:
    sp = ctx.run_dir / "store.json"
    snap = out / "review_snapshot.json"
    if sp.exists():
        shutil.copy(sp, snap)
    labels = gold.labels(ctx)
    models = {}
    for r in ("agent", "judge", "judge2", "reviewer"):
        rc = role_cfg(ectx.cfg, r)
        models[r] = {k: rc.get(k) for k in ("provider", "model", "reasoning_effort", "temperature", "pricing")}
    return {
        "run_id": run_id, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "suite": suite,
        "command": " ".join(sys.argv[1:]) or None,
        "review": {"run_dir": str(ctx.run_dir), "store_sha256": file_sha(sp) if sp.exists() else None,
                   "n_nodes": len(ctx.store.nodes), "snapshot": str(snap)},
        "condition": provenance.mode(ctx),
        "human_involvement": provenance.summary(ctx),
        "config_changes": config_lock.changes(ctx.store),   # review-defining settings changed after the start
        "models": models, "seed": ectx.cfg.get("eval", {}).get("seed"),
        "llm_sampling": "temperatures as configured per role (Claim 1 repeats: 0.0 then 0.7); provider-side sampling seeds are not set",
        "cache_policy": "reuse identical earlier calls" if allow_cache else "fresh: every model call in this run is new (answers are still cached for later)",
        "code_version": code_version(),
        "gold": {"source": gold.info(ctx).get("source"), "n_labelled": len(labels), "n_included": sum(labels.values()),
                 "used_by": "evaluator only (stripped from every store the agent sees)"},
        "from_run": from_run,
        "config": _sanitize(ectx.cfg),
    }


def _resume(ctx: Ctx, out: Path) -> dict:
    """Continue an interrupted run in its own directory: same review snapshot, same stage-wise review,
    completed suites kept, an unfinished suite restarted from scratch (its partial attempt is set aside)."""
    R = json.loads((out / "results.json").read_text())
    sp = ctx.run_dir / "store.json"
    if file_sha(sp) != R["manifest"]["review"]["store_sha256"]:
        raise RuntimeError("the review changed since this evaluation run started; start a new run instead of resuming")
    done = set(R["results"]) | {"e2e" if "end_to_end" in R["results"] else "", "screening" if "stagewise_screening" in R["results"] else "",
                                 "grounding" if "stagewise_extraction_rob" in R["results"] else "",
                                 "synthesis" if "stagewise_synthesis" in R["results"] else "",
                                 "judge" if "judge_validation" in R["results"] else ""}
    discarded = []
    for name in ("claim1_partial.json",):   # Claim 2 continues from its partial results (finished runs kept)
        p = out / name
        if p.exists() and name.split("_")[0] not in R["results"]:
            stamp = time.strftime("%H%M%S")
            p.rename(out / f"discarded_{stamp}_{name}")
            for d in ("claim2_runs", "claim2_R1"):
                if (out / d).exists() and name.startswith("claim2"):
                    (out / d).rename(out / f"discarded_{stamp}_{d}")
            discarded.append(f"{name} (partial attempt set aside; its calls stay in this run's cost)")
    R["manifest"].setdefault("resumed", []).append({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                    "previous_stop": R.pop("stopped", None), "set_aside": discarded})
    return R, {d for d in done if d}


def run(ctx: Ctx, suite: str = "all", n_cases: int | None = None, allow_cache: bool = False,
        from_run: str | None = None, label: str = "", resume: str | None = None) -> dict:
    if not gold.labels(ctx):
        raise ValueError(f"No known answers for this run ({ctx.run_dir}/eval_private/gold.json). Evaluation "
                         "needs a benchmark topic (configs/benchmark/) and a finished `sragent run` (search stage).")
    root = ctx.run_dir / "eval" / "runs"
    done = set()
    if resume:
        run_id, out = resume, root / resume
        R, done = _resume(ctx, out)
        suite = R["manifest"]["suite"]
        from_run = R["manifest"].get("from_run")
        allow_cache = R["manifest"]["cache_policy"].startswith("reuse")
    else:
        run_id = time.strftime("%Y%m%d-%H%M%S") + f"_{suite}" + (f"_{label}" if label else "")
        out = root / run_id
        out.mkdir(parents=True, exist_ok=True)
    ectx = eval_context(ctx, out, allow_cache)
    ecfg = ectx.cfg.get("eval", {})
    labels = gold.labels(ctx)
    if not resume:
        R = {"manifest": manifest(ctx, ectx, run_id, suite, allow_cache, from_run, out), "results": {}, "cost": {"by_suite": {}}}
    judge = Judge(ectx, out, "judge")
    todo = [s for s in (SUITES if suite == "all" else [suite]) if s not in done]
    sctx = None
    field_labels = None
    if resume and (out / "stagewise" / "store.json").exists():
        import dataclasses
        st = ReviewStore.load(out / "stagewise" / "store.json")
        st.run_dir = out / "stagewise"
        sctx = dataclasses.replace(ectx, store=st, run_dir=out / "stagewise")

    def get_stagewise():
        nonlocal sctx
        if sctx is not None:
            return sctx
        src = root / from_run / "stagewise" / "store.json" if from_run else None
        d = out / "stagewise"
        if src and src.exists():
            st = ReviewStore.load(src)
            d.mkdir(parents=True, exist_ok=True)
            st.run_dir = d
            import dataclasses
            sctx = dataclasses.replace(ectx, store=st, run_dir=d)
            sctx.save()
            R["manifest"]["stagewise_input"] = {**st.meta.get("gold_input", {}), "reused_from_run": from_run,
                                                "store_sha256": file_sha(src)}
        else:
            usd0 = ectx.hub.tracker.spent
            sctx, info = stagewise.build_gold_input_review(ctx, ectx, d)
            R["manifest"]["stagewise_input"] = {**info, "built_in_this_run": True}
            R["cost"]["by_suite"]["stagewise_build"] = round(ectx.hub.tracker.spent - usd0, 4)
        R["manifest"]["stagewise_input"]["snapshot"] = str(d / "store.json")
        return sctx

    try:
        for s in todo:
            ectx.log(f"[eval {run_id}] === {s} ===")
            if s in NEEDS_STAGEWISE:
                get_stagewise()
            usd0 = ectx.hub.tracker.spent
            if s == "e2e":
                R["results"]["end_to_end"] = screening_eval.end_to_end(ctx)
            elif s == "screening":
                R["results"]["stagewise_screening"] = screening_eval.stagewise_screening(ctx)
            elif s == "grounding":
                g = grounding_eval.run(sctx, judge, max_studies=ecfg.get("grounding_max_studies"))
                field_labels = g.pop("_field_labels", {})
                save(out, "field_labels", field_labels)
                g["input"] = R["manifest"]["stagewise_input"]
                R["results"]["stagewise_extraction_rob"] = g
            elif s == "synthesis":
                if field_labels is None:   # this run's grounding labels, else those of the run whose stage-wise review is reused
                    for p in [out / "field_labels.json"] + ([root / from_run / "field_labels.json"] if from_run else []):
                        if p.exists():
                            field_labels = json.loads(p.read_text())
                            break
                if field_labels is None:
                    R["results"]["stagewise_synthesis"] = {"skipped": "needs the grounding suite's judge labels (run it in the same run, or --from-run a run that has them)"}
                else:
                    vctx, info = stagewise.verified_extraction_review(sctx, field_labels, out / "synthesis_input")
                    R["results"]["stagewise_synthesis"] = synthesis_eval.run(vctx, judge, info)
            elif s == "claim1":
                R["results"]["claim1"] = claim1.run(sctx, judge, out, n_sets=ecfg.get("claim1_sets", 2),
                                                    repeats=ecfg.get("claim1_repeats", 1),
                                                    max_src_chars=ecfg.get("claim1_source_chars", 5000),
                                                    max_genuine_checks=ecfg.get("claim1_genuine_checks", 5),
                                                    max_total_chars=ecfg.get("claim1_total_chars", 300000))
                R["results"]["claim1"]["input"] = "gold-input review (stage-wise)"
                R["results"]["claim1"]["human_materials"] = {"dir": str(out / "claim1_human_materials"),
                                                             "status": "prepared_not_used"}
            elif s == "claim2":
                part = out / "claim2_partial.json"
                prev = json.loads(part.read_text()) if (resume and part.exists()) else None
                R["results"]["claim2"] = claim2.run(sctx, judge, out, n_per_type=n_cases or ecfg.get("claim2_cases_per_type", 1),
                                                    labels=labels, repeats=ecfg.get("claim2_repeats", 1), resume=prev)
                R["results"]["claim2"]["input"] = "R0 = gold-input review (stage-wise), before any error was planted"
            elif s == "claim2_rescore":
                if not from_run:
                    raise ValueError("claim2_rescore needs --from-run <run_id> (the experiment to re-score)")
                src = root / from_run
                prev = json.loads((src / "results.json").read_text())["results"]["claim2"]
                R["results"]["claim2"] = claim2.rescore(ectx, judge, out, prev, src)
                R["results"]["claim2"]["rescored_from_run"] = from_run
            elif s == "judge":
                planted = []
                for st_ in (R["results"].get("claim1") or {}).get("sets", []):
                    planted += [p for p in st_["planted"] if p["type"] in ("extraction", "synthesis")]
                for c in (R["results"].get("claim2") or {}).get("cases", []):
                    if c["type"] == "A_extraction_fix":
                        planted.append({"id": c["id"], "gold": c["gold_value"], "corrupted": c["corrupted_value"],
                                        "location": c["target"], "description": c["error"]})
                R["results"]["judge_validation"] = judge_validation.run(ectx, judge, out, planted,
                                                                        n_agree=ecfg.get("judge_agreement_items", 25),
                                                                        n_human=ecfg.get("human_audit_items", 15))
                R["results"]["judge_validation"]["controls_source"] = "planted items of this run only"
            R["cost"]["by_suite"][s] = round(ectx.hub.tracker.spent - usd0, 4)
            save(out, "results", R)
    except (BudgetExceeded, QuotaExhausted) as e:
        ectx.log(f"[eval] stopped: {e}")
        R["stopped"] = f"{type(e).__name__}: {e}. Resume with: sragent eval --config ... --resume {run_id}"
    t = ectx.hub.tracker
    R["status"] = "interrupted" if R.get("stopped") else "complete"
    R["cost"].update(usage_totals(out))
    R["cost"]["budget_usd"] = t.budget
    R["cost"].pop("not_attributed_to_a_suite_usd", None)
    R["cost"]["note"] = ("All calls of this evaluation run from its own usage.jsonl, including attempts interrupted by a budget "
                         "or quota stop and resumed. Earlier runs are not included.")
    save(out, "results", R)
    (out / "EVAL_REPORT.md").write_text(render(R))
    (ctx.run_dir / "eval" / "LATEST_RUN.txt").write_text(run_id + "\n")
    ectx.log(f"[eval] wrote {out / 'EVAL_REPORT.md'}")
    return R


def usage_totals(out: Path) -> dict:
    """Cost of the whole run from its own usage.jsonl, attributed to suites by the suite start times in run.log
    (so the calls of attempts that were interrupted and resumed are counted under their suite too)."""
    import re
    from datetime import datetime, timedelta
    marks = []
    lp = out / "run.log"
    day = datetime.strptime(out.name[:8], "%Y%m%d")
    prev = None
    for line in (lp.read_text().splitlines() if lp.exists() else []):
        m = re.match(r"^(\d\d):(\d\d):(\d\d) \[eval [^\]]+\] === (\w+) ===", line)
        if not m:
            continue
        t = day + timedelta(hours=int(m.group(1)), minutes=int(m.group(2)), seconds=int(m.group(3)))
        if prev and t < prev:      # crossed midnight
            day += timedelta(days=1)
            t += timedelta(days=1)
        prev = t
        marks.append((t.timestamp(), "stagewise_build" if m.group(4) == "stagewise" else m.group(4)))
    tot, calls, cached, by = 0.0, 0, 0, {}
    p = out / "usage.jsonl"
    for line in (p.read_text().splitlines() if p.exists() else []):
        try:
            u = json.loads(line)
        except ValueError:
            continue
        usd = float(u.get("usd", 0))
        tot += usd
        cached += int(bool(u.get("cached")))
        calls += int(not u.get("cached"))
        suite = next((sname for ts, sname in reversed(marks) if ts <= u.get("ts", 0) + 1), "unattributed")
        by[suite] = by.get(suite, 0.0) + usd
    return {"this_run_usd": round(tot, 4), "api_calls": calls, "cache_hits": cached,
            "by_suite_all_attempts": {k: round(v, 4) for k, v in by.items()}}


def import_audit(ctx: Ctx, run_id: str, sheet: str) -> dict:
    out = ctx.run_dir / "eval" / "runs" / run_id
    R = json.loads((out / "results.json").read_text())
    res = judge_validation.import_audit(sheet)
    R["results"].setdefault("judge_validation", {}).setdefault("human_audit", {}).update(res)
    R["results"]["judge_validation"]["human_audit"]["imported_from"] = str(sheet)
    save(out, "results", R)
    (out / "EVAL_REPORT.md").write_text(render(R))
    return res


# ------------------------------------------------------------------ report (rendered from results.json only)
def _f(x):
    return "n/a" if x is None else x


def _paired_line(name: str, p: dict) -> str:
    if not p or not p.get("units"):
        return f"| {name} | 0 | n/a | n/a | n/a | n/a | n/a |"
    keys = [k for k in p if k.startswith("mean_") and k != "mean_difference"]
    st = p["sign_test"]
    return (f"| {name} | {p['units']} | {p[keys[0]]} | {p[keys[1]]} | {p['mean_difference']} | "
            f"{p['difference_95ci_cluster_bootstrap']} | +{st['positive']} / -{st['negative']} / ={st['ties']}, p={_f(st['p_two_sided'])} |")


def render(R: dict) -> str:
    M, X = R["manifest"], R["results"]
    hi = M["human_involvement"]
    L = [f"# Evaluation run `{M['run_id']}`" + ("" if R.get("status") != "interrupted" else " (INTERRUPTED, incomplete)"), "",
         "All numbers below come from this run's `results.json` only. Earlier runs are not merged in.", "",
         "## Run record", "",
         f"- Condition: **{M['condition']}** · human judgments recorded in the evaluated review: {hi['human_judgment']['n']} "
         f"{hi['human_judgment']['by_action'] or ''} · full texts supplied by a person: {hi['pdf_provision']['files_supplied']} "
         f"(declared not obtainable: {hi['pdf_provision']['declared_not_obtainable']}) · condition respected: {hi['condition_respected']}",
         *([f"- **Settings changed after the review started** (accepted on purpose; outputs made before a change followed the earlier "
            f"settings, so this is not a clean single-condition run): "
            + "; ".join(f"{c['key']} ({e['reason']})" for e in M["config_changes"] for c in e["changes"])] if M.get("config_changes") else []),
         f"- Review snapshot: `{M['review']['run_dir']}` store sha256 `{M['review']['store_sha256']}` ({M['review']['n_nodes']} nodes), copied to `review_snapshot.json`",
         f"- Models: " + "; ".join(f"{r} `{v['provider']}:{v['model']}` (effort {v['reasoning_effort']})" for r, v in M["models"].items()),
         f"- Seed {M['seed']} · {M['llm_sampling']}",
         f"- Cache: {M['cache_policy']} · code version `{M['code_version']}`",
         f"- Gold: {M['gold']['source']} ({M['gold']['n_labelled']} labelled, {M['gold']['n_included']} included); {M['gold']['used_by']}"]
    if M.get("stagewise_input"):
        si = M["stagewise_input"]
        L.append(f"- **Stage-wise input supplied:** {si.get('what')} ({si.get('n_studies')} studies; sources {si.get('source_kinds')})"
                 + (f"; reused from run {si['reused_from_run']}" if si.get("reused_from_run") else ""))
    if M.get("from_run"):
        L.append(f"- Derived from run `{M['from_run']}`")
    for r in M.get("resumed", []):
        L.append(f"- Resumed {r['time']} after: {str(r['previous_stop'])[:160]}; set aside: {r['set_aside'] or 'nothing'}")
    L.append("")

    e = X.get("end_to_end")
    if e:
        s = e["search"]
        L += ["## End-to-end (real search, screening and full-text review)", "",
              f"Universe: {e['universe']}. Search recall {s['gold_found_by_search']}/{s['gold_includes']} ({s['search_recall']}); "
              f"{s['retrieved']} records retrieved ({s['retrieved_outside_benchmark_pool']} outside the benchmark pool, unscored). "
              f"Citation chasing added {s.get('records_added_by_citation_chasing', 0)} records and found "
              f"{s.get('gold_found_by_citation_chasing_only', 0)} gold includes the search missed.", "",
              f"- **End-to-end recall: {e['end_to_end_recall']}** (95% CI {e['end_to_end_recall_95ci']}); precision {_f(e['end_to_end_precision'])}; "
              f"included from the search: {e['included_from_search']}",
              f"- Gold includes by outcome: {e['gold_outcomes']}",
              *([f"- **{e['included_unlabelled']} included studies have no gold label** (found outside the benchmark pool, "
                 f"e.g. by citation chasing) and are left out of the precision above: {', '.join(e['included_unlabelled_rids'][:12])}"
                 + (" …" if e['included_unlabelled'] > 12 else "")] if e.get("included_unlabelled") else []),
              f"- Status of all retrieved records: {e['status_of_retrieved_records']}", ""]
    s = X.get("stagewise_screening")
    if s and "n_records" in s:
        L += ["## Stage-wise screening (input supplied: benchmark candidate pool)", "",
              f"{s['n_records']} labelled records, {s['n_gold_included']} gold includes.", "",
              "| Decision | Recall (95% CI) | Precision | F2 | Work saved |", "|---|---|---|---|---|"]
        for k, lab in (("title_abstract_forwarding", "Forwarded to full-text review"), ("final_included", "Included after full-text review")):
            m = s[k]
            L.append(f"| {lab} | {_f(m['recall'])} {m.get('recall_95ci')} | {_f(m['precision'])} | {_f(m['f2'])} | {m['work_saved']} |")
        ft = s["full_text_review_given_full_text"]
        if ft.get("decisions"):
            m = ft["decisions"]
            L += ["", f"Full-text review where the full text was available ({ft['n_with_full_text']} of {ft['n_forwarded']} forwarded): "
                      f"recall {m['recall']} {m['recall_95ci']}, precision {m['precision']}; undecided {ft['undecided']}"]
        L += ["", f"- Status by gold: {s['status_by_gold']}",
              f"- Gold includes not included: {s['gold_includes_not_included']}", ""]
    g = X.get("stagewise_extraction_rob")
    if g:
        L += ["## Stage-wise extraction and risk of bias (input supplied: gold includes)", "",
              "Gold inclusion does not give correct values; values are judged against their cited source sentences.", "",
              f"- Extraction evidence located: {g['evidence_extraction']}",
              f"- By source kind: {g['evidence_extraction_by_source']}",
              f"- RoB evidence located: {g['evidence_rob']}",
              f"- Judge, value supported by its cited sentences: {g['judge_value_support']['counts']} "
              f"(supported rate {g['judge_value_support']['supported_rate']}; by source {g['judge_value_support']['by_source_kind']})",
              f"- Terms (RxNorm/PubChem/ChEMBL/MONDO): {g['term_validation_extracted']}; corrupted-term probes flagged "
              f"{g['term_corruption_probes']['flagged']}/{g['term_corruption_probes']['n']}", ""]
    y = X.get("stagewise_synthesis")
    if y and "n_statements" in y:
        L += ["## Stage-wise synthesis (input supplied: judge-verified extraction table)", "",
              f"Input: {y['input']['what']}; values kept {y['input']['values_kept']}, replaced by NR {y['input']['values_dropped']}.", "",
              f"- {y['n_statements']} statements; judge: {y['judge_support']} (supported rate {y['supported_rate']}, 95% CI {y['supported_rate_95ci']})",
              f"- Overgeneralization rate {y['overgeneralization_rate']}; valid citations {y['citation_validity_rate']}; "
              f"agent self-check agrees with the judge {y['self_check_vs_judge_agreement']}", ""]
    elif y:
        L += ["## Stage-wise synthesis", "", f"- {y}", ""]
    c1 = X.get("claim1")
    if c1 and c1.get("aggregate"):
        d = c1["design"]
        L += ["## Claim 1: report-only vs evidence-linked (simulated reviewer; input: gold-input review)", "",
              f"Design: {d['unique_errors']} unique planted errors in {d['unique_papers_with_errors']} papers and "
              f"{d['unique_statements_with_errors']} statements ({d['errors_by_type']}), {d['error_sets']} error sets, "
              f"{d['repeats_per_condition']} repeats per condition = {d['observations_per_condition']} observations per condition. "
              "The same errors are given to both conditions.", "",
              "| Condition | Observations | Detected | Repaired | Recall by type | Precision (est.) | Reviewer USD |", "|---|---|---|---|---|---|---|"]
        for cond, a in c1["aggregate"].items():
            L.append(f"| {cond} | {a['planted']} | {a['identified']} ({a['recall']}) | {a['repaired']} ({a['repair_rate']}) | "
                     f"{a['recall_by_type']} | {a['precision_est_mean']} | {a['reviewer_usd_total']} |")
        p = c1.get("paired") or {}
        if p:
            L += ["", "Paired analysis, unit = unique error (repeats averaged within the error; cluster bootstrap over errors):", "",
                  "| Outcome | Units | Report-only | Evidence-linked | Difference | 95% CI | Sign test |", "|---|---|---|---|---|---|---|",
                  _paired_line("Detection", p["detection"]), _paired_line("Repair", p["repair"])]
            for t, pp in p.get("detection_by_type", {}).items():
                L.append(_paired_line(f"Detection: {t}", pp))
            L += ["", f"Observation-level McNemar (reference only; repeats are not independent): {p['observation_level_mcnemar_reference_only']}"]
        L += ["", f"Human study materials: {c1['human_materials']['status']} (`{c1['human_materials']['dir']}`)", ""]
    c2 = X.get("claim2")
    if c2 and c2.get("aggregate"):
        d = c2.get("design", {})
        L += ["## Claim 2: dependency-aware vs local-edit revision", "",
              f"Input: {c2.get('input', '')}. R0 snapshot: `{c2.get('R0_snapshot') or c2.get('rescored_against')}`.",
              f"Design: {d.get('unique_cases')} unique cases on {d.get('unique_papers')} papers {d.get('cases_by_type')}, "
              f"{d.get('repeats')} run(s) per case and condition, same R1 for both conditions. "
              f"Primary cases (R0 agrees with gold / source): {d.get('primary_cases')}.", ""]
        if d.get("flagged_cases"):
            L.append(f"Flagged cases, reported separately: {d['flagged_cases']}")
            L.append("")
        L += ["| Type : condition | Runs | R0 recovery (target) | Source-correct (target) | Downstream completion | Contradictions added | New errors | Accepted false feedback | USD / revision |",
              "|---|---|---|---|---|---|---|---|---|"]
        for k, a in c2["aggregate"].items():
            if k == "pooled_A_to_C":
                continue
            abc = k[0] in "ABC"
            L.append(f"| {k} | {a['n']} | {_f(a.get('r0_recovery_rate'))} | {_f(a.get('source_correct_rate'))} | "
                     f"{_f(a['downstream_completion_mean'])} | {a.get('contradictions_added', 'n/a') if abc else 'n/a'} | "
                     f"{a['new_errors'] if abc else 'n/a'} | {'n/a' if abc else a['accepted_false_rate']} | {a['usd_per_revision']} |")
        p = c2.get("paired") or {}
        if p:
            L += ["", "Paired analysis, unit = unique primary case:", "",
                  "| Outcome | Units | First | Second | Difference | 95% CI | Sign test |", "|---|---|---|---|---|---|---|",
                  _paired_line("Downstream completion (local -> dependency-aware)", p.get("downstream_completion")),
                  _paired_line("R0 recovery of the target", p.get("target_fixed")),
                  _paired_line("Source-correct target", p.get("source_correct")),
                  _paired_line("D: accepted false feedback (verify off -> on)", p.get("D_accepted_false"))]
        L += ["", "Pooled A-C:", "", "```", json.dumps(c2["aggregate"].get("pooled_A_to_C"), indent=1), "```", ""]
    jv = X.get("judge_validation")
    if jv:
        ha = jv.get("human_audit", {})
        L += ["## Judge validation", "",
              f"- Known-answer controls ({jv.get('controls_source', '')}): {jv['known_answer_controls']['correct']}/{jv['known_answer_controls']['n']} "
              f"(95% CI {jv['known_answer_controls']['accuracy_95ci']})",
              f"- Second judge ({jv['inter_judge']['judge2']}): raw agreement {jv['inter_judge']['raw_agreement']}, "
              f"kappa {jv['inter_judge']['kappa_overall']}, n {jv['inter_judge']['n']}",
              f"- **Human audit: {ha.get('status')}** ({ha.get('n_reviewed', 0)}/{ha.get('n_items')} items labelled) `{ha.get('file')}`"
              + ("" if ha.get("status") == "reviewed" else ". Not counted as done until a person labels every item and it is imported."), ""]
    c = R["cost"]
    L += ["## Cost of this run", "", f"- Actual: ${c.get('this_run_usd')} · {c.get('api_calls')} API calls, {c.get('cache_hits')} cache hits",
          f"- By suite, all attempts: {c.get('by_suite_all_attempts', c.get('by_suite'))}",
          f"- {c.get('note', '')}", ""]
    if R.get("stopped"):
        L += [f"**Stopped early:** {R['stopped']}", ""]
    return "\n".join(L) + "\n"
