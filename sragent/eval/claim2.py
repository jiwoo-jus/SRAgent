"""Claim 2: a dependency-aware agent resolves feedback more consistently than a local-edit agent.

Gold by construction ("propagated corruption"):
  R0  the finished review (reference)
  R1  R0 with ONE planted error that has been propagated into dependent outputs the way a real
      upstream error would be (e.g. a wrong number that the synthesis then repeats)
  feedback  a realistic, non-leading user comment about the planted error
  R2  the review after the agent handles the feedback (auto-accepted)

The expected downstream set E = outputs that differ between R0 and R1 (computed, not annotated).

Case types
  A extraction_fix     a number in an extracted field is wrong -> fix + update statements
  B eligibility_fix    an eligible study was excluded -> re-include, restore extraction/RoB/synthesis
  C add_paper          an eligible study is missing entirely -> fetch, screen, extract, integrate
  D false_feedback     confident but wrong feedback on a correct value -> must be rejected
  E add_ineligible     user asks to add an ineligible paper -> must not be included

Conditions
  A-C : local_edit (propagate=False) vs dependency_aware (propagate=True), both verify=True
  D   : verify=True vs verify=False (both propagate=True)       [tests "checks feedback"]
  E   : dependency_aware only (eligibility screening is the check)

R0 is the review BEFORE the error was planted. It is not guaranteed to be correct, so two outcomes are
scored separately:
  r0_recovery         did the revision restore R0 (target + the outputs that differ between R0 and R1)?
  source_correctness  is the revised target right according to the source paper (judge reads the paper,
                      not R0); for eligibility cases: is the study included and a gold include?
When cases are built, agreement with the external gold standard is recorded automatically:
  B/C (re-include an eligible study) use gold includes with an available full text only;
  E uses gold excludes; A/D check R0's value against the source (no value-level external gold exists).
Cases whose R0 conflicts with the gold or the source, or cannot be checked, are flagged and kept out of
the primary aggregate (reported separately). Re-scoring always loads this experiment's own R0 snapshot.

Measures: target correction accuracy; proportion of necessary downstream updates completed;
remaining contradictions (judge audit restricted to the case's study); new errors introduced
into previously correct content; acceptance of unsupported feedback; USD, calls, seconds per revision.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import random
import re

from .. import feedback, prompts
from ..context import Ctx
from ..stages import extraction
from .common import Judge, save, short, wilson
from .inject import NUM, alter_number


_RUNS_DIR = None   # set by run(): <eval run>/claim2_runs


def _sub_ctx(ctx: Ctx, store, sub: str) -> Ctx:
    rd = (_RUNS_DIR or ctx.run_dir / "eval" / "claim2_runs") / sub
    rd.mkdir(parents=True, exist_ok=True)
    s = store.clone()
    s.run_dir = rd
    return dataclasses.replace(ctx, store=s, run_dir=rd)


def _view(n: dict | None):
    if n is None:
        return None
    if n["status"] != "active":
        return {"status": n["status"]}
    return feedback.editable(n)


def _cites(st, rid):
    return [n["id"] for n in st.by_type("synthesis", active_only=True) if rid in n["data"]["cites"]]


def _propagate_into_synthesis(c: Ctx, src_id: str, before, after, rid: str):
    cs = {"id": "corruption", "changes": []}
    for sid in _cites(c.store, rid):
        feedback._revise_dependent(c, cs, sid, src_id, before, after, tp="corrupt")
    return cs


# ------------------------------------------------------------------ case builders
def build_A(ctx: Ctx, rnd: random.Random, rid: str) -> dict | None:
    st = ctx.store
    n = st.get(f"ext:{rid}")
    if not n:
        return None
    stmts = " ".join(st.get(s)["data"]["text"] for s in _cites(st, rid))
    best = None
    for f in ("pk_results", "sample_size", "efficacy_safety", "exposure_response", "intervention"):
        v = n["data"]["fields"].get(f, {}).get("value", "NR")
        if v.upper().startswith("NR"):
            continue
        shared = [m.group(1) for m in NUM.finditer(v) if m.group(1) in stmts and m.group(1) not in ("1", "2", "3")]
        if shared:
            best = (f, v, shared)
            break
    if not best:
        return None
    f, v, shared = best
    num = shared[0]
    new_num = alter_number(num, rnd)
    if not new_num:
        return None
    new_v = v.replace(num, new_num, 1)
    c = _sub_ctx(ctx, st, f"A_{rid}_build")
    data = copy.deepcopy(c.store.get(f"ext:{rid}")["data"])
    before = copy.deepcopy(data)
    data["fields"][f]["value"] = new_v
    c.store.upsert(f"ext:{rid}", "extraction", data, depends_on=n["depends_on"], evidence=n["evidence"],
                   actor="injector", reason="planted error")
    _propagate_into_synthesis(c, f"ext:{rid}", before, data, rid)
    label = f.replace("_", " ")
    return {"type": "A_extraction_fix", "rid": rid, "target": f"ext:{rid}", "field": f, "R1": c.store,
            "gold_value": v, "corrupted_value": new_v,
            "error": f"In study {rid}, field '{f}': the number {num} was changed to {new_num} (gold value: {v}).",
            "feedback": f"The {label} extracted for study {rid} does not match what the paper reports. "
                        f"Please re-check it against the source."}


def build_B(ctx: Ctx, rid: str) -> dict | None:
    from .inject import inject_eligibility
    c = _sub_ctx(ctx, ctx.store, f"B_{rid}_build")
    e = inject_eligibility(c, rid, structural=True)
    if not e:
        return None
    _propagate_into_synthesis(c, f"screen:{rid}", {"included": True, "study": rid}, {"included": False, "study": rid}, rid)
    return {"type": "B_eligibility_fix", "rid": rid, "target": f"screen:{rid}", "R1": c.store,
            "error": f"Study {rid} is eligible (meets I1-I3) but was excluded; its data were removed from the synthesis.",
            "feedback": f"Study {rid} should not have been excluded: it reports original emicizumab PK data in humans. "
                        f"Please reconsider its eligibility."}


def build_C(ctx: Ctx, rid: str) -> dict | None:
    st = ctx.store
    rec = st.get(f"rec:{rid}")
    if not rec or not rec["data"].get("pmid"):
        return None
    c = _sub_ctx(ctx, st, f"C_{rid}_build")
    _propagate_into_synthesis(c, f"screen:{rid}", {"included": True, "study": rid}, {"included": False, "study": rid}, rid)
    for kind in ("rec", "screen", "ft", "ext", "rob"):
        c.store.nodes.pop(f"{kind}:{rid}", None)
    for n in c.store.by_type("synthesis"):
        n["depends_on"] = [d for d in n["depends_on"] if not d.endswith(f":{rid}")]
    st.reindex()
    return {"type": "C_add_paper", "rid": rid, "pmid": rec["data"]["pmid"], "target": f"screen:{rid}", "R1": c.store,
            "error": f"Eligible study {rid} (PMID {rec['data']['pmid']}) is missing from the review.",
            "feedback": f"It looks like PMID {rec['data']['pmid']} was missed. Please add it to the review."}


def build_D(ctx: Ctx, rnd: random.Random, rid: str) -> dict | None:
    st = ctx.store
    n = st.get(f"ext:{rid}")
    if not n:
        return None
    cands = [(k, v) for k, v in n["data"]["fields"].items()
             if not v["value"].upper().startswith("NR") and v.get("check", {}).get("status") in ("located", "exact")
             and NUM.search(v["value"])]
    if not cands:
        return None
    f, v = rnd.choice(cands)
    out = ctx.agent.chat(ctx.msgs(prompts.INJECT_FALSE_FEEDBACK.format(node_id=f"ext:{rid}", field=f, value=v["value"],
                                                                       quote=v.get("quote", ""))), tag="inject")
    return {"type": "D_false_feedback", "rid": rid, "target": f"ext:{rid}", "field": f, "R1": st.clone(),
            "gold_value": v["value"], "false_claim": out.get("false_claim"), "feedback": out.get("feedback"),
            "error": "none: the feedback is wrong; the correct behaviour is to reject it and change nothing."}


def build_E(ctx: Ctx, rnd: random.Random, labels: dict, used: set) -> dict | None:
    st = ctx.store
    cands = [n["id"].split(":", 1)[1] for n in st.by_type("screen")
             if n["data"]["decision"] == "exclude" and st.get(f"rec:{n['id'].split(':', 1)[1]}")["data"].get("pmid")
             and labels.get(n["id"].split(":", 1)[1]) == 0 and n["id"].split(":", 1)[1] not in used]
    if not cands:
        return None
    with_ft = [r for r in cands if (st.get(f"ft:{r}") or {}).get("data", {}).get("kind") == "fulltext"]
    rid = rnd.choice(sorted(with_ft) or sorted(cands))
    c = _sub_ctx(ctx, st, f"E_{rid}_build")
    pmid = st.get(f"rec:{rid}")["data"]["pmid"]
    for kind in ("rec", "screen", "ft", "ext", "rob"):
        c.store.nodes.pop(f"{kind}:{rid}", None)
    return {"type": "E_add_ineligible", "rid": rid, "pmid": pmid, "target": None, "R1": c.store,
            "error": "none: the paper is ineligible; it must not be included.",
            "feedback": f"Please add PMID {pmid}; it seems relevant to this review."}


# ------------------------------------------------------------------ gold agreement / source checks
def source_check(ctx: Ctx, judge: Judge, store, rid: str, field: str, value: str, meta: dict) -> dict:
    from ..evidence import packed
    text, kind = store.text_of(rid)
    desc = next((f["description"] for f in ctx.topic["extraction_fields"] if f["name"] == field), "")
    j = judge("source_check", prompts.SOURCE_CHECK.format(rid=rid, field=field, description=desc, value=value,
                                                         kind=kind, source=packed(text, 40000)), "label", meta)
    return {"label": j.get("label"), "rationale": short(j.get("rationale", ""), 300), "source_kind": kind}


def gold_agreement(ctx: Ctx, judge: Judge, case: dict, R0, labels: dict) -> None:
    """Record, at build time, whether the case's reference (R0) agrees with external gold / the source."""
    t, rid = case["type"][0], case["rid"]
    lab = labels.get(rid)
    if t in "BC":
        has_ft = (R0.get(f"ft:{rid}") or {}).get("data", {}).get("kind") == "fulltext"
        case["external_gold"] = {1: "consistent: gold include", 0: "conflict: gold exclude"}.get(lab, "unverified: no gold label")
        case["r0_full_text"] = has_ft
        case["primary"] = lab == 1 and has_ft
    elif t == "E":
        case["external_gold"] = {0: "consistent: gold exclude", 1: "conflict: gold include"}.get(lab, "unverified: no gold label")
        case["primary"] = lab == 0
    else:   # A, D: there is no external gold for extracted values; check R0's value against the paper
        case["external_gold"] = "unverified: no value-level gold; R0 value checked against the source"
        chk = source_check(ctx, judge, R0, rid, case["field"], case["gold_value"], {"claim": 2, "case": case["id"], "check": "R0_value"})
        case["r0_source_check"] = chk
        case["primary"] = chk["label"] == "supported"
    if not case["primary"]:
        case["flag"] = "R0 conflicts with the external gold or the source, or could not be verified: reported separately"


# ------------------------------------------------------------------ scoring
def score_case(ctx: Ctx, judge: Judge, case: dict, R0, R1, R2, cs: dict, cond: str) -> dict:
    t = case["type"]
    rid = case["rid"]
    res = {"case": case["id"], "type": t, "condition": cond, "verdict": cs.get("verdict"),
           "n_changes": len(cs["changes"]), "usd": cs.get("cost_usd_uncached", cs.get("cost_usd")), "seconds": cs.get("seconds")}
    meta = {"claim": 2, "case": case["id"], "condition": cond}
    res["primary"] = case.get("primary", True)
    if t in ("D_false_feedback", "E_add_ineligible"):
        if t == "D_false_feedback":
            changed = _view(R2.get(case["target"])) != _view(R1.get(case["target"]))
            res["accepted_false_feedback"] = changed
            if changed:
                new_v = (R2.get(case["target"]) or {}).get("data", {}).get("fields", {}).get(case["field"], {}).get("value")
                res["source_correctness"] = source_check(ctx, judge, R2, rid, case["field"], new_v or "", meta)["label"]
            else:
                res["source_correctness"] = (case.get("r0_source_check") or {}).get("label")
        else:
            res["accepted_false_feedback"] = rid in R2.included_ids()
            sc = (R2.get(f"screen:{rid}") or {}).get("data", {})
            res["e_outcome"] = ("included" if res["accepted_false_feedback"] else
                                "rejected as ineligible" if sc.get("decision") == "exclude" else
                                f"not included: {sc.get('decision', 'not screened')}")
        res["correct_behaviour"] = not res["accepted_false_feedback"]
        return res

    # expected downstream set: outputs that differ R0 vs R1 (excluding the target)
    keys = {k for k in set(R0.nodes) | set(R1.nodes)
            if (R0.get(k) or R1.get(k))["type"] in ("synthesis", "extraction", "rob", "screen")}
    expected = sorted(k for k in keys if k != case["target"] and _view(R0.get(k)) != _view(R1.get(k)))
    res["expected_downstream"] = expected

    # target
    if t == "A_extraction_fix":
        rev = (R2.get(case["target"]) or {}).get("data", {}).get("fields", {}).get(case["field"], {}).get("value")
        if rev == case["corrupted_value"]:
            res["target_fixed"] = "no"
        else:
            j = judge("revision", prompts.JUDGE_REVISION.format(node_id=f"{case['target']} {case['field']}",
                                                               gold=case["gold_value"], corrupted=case["corrupted_value"],
                                                               revised=rev, error=case["error"]), "fixed", meta)
            res["target_fixed"] = j.get("fixed")
            res["target_new_error"] = j.get("introduced_new_error")
        res["source_correctness"] = (source_check(ctx, judge, R2, rid, case["field"], rev or "", meta)["label"]
                                     if rev != case["gold_value"] else (case.get("r0_source_check") or {}).get("label"))
        res["source_correct"] = res["source_correctness"] == "supported"
    else:
        res["target_fixed"] = "yes" if rid in R2.included_ids() else "no"
        res["source_correctness"] = "correct (included; gold include)" if (rid in R2.included_ids() and case.get("primary")) \
            else ("not included: " + (R2.get(f"screen:{rid}") or {}).get("data", {}).get("decision", "missing"))
        res["source_correct"] = rid in R2.included_ids() and bool(case.get("primary"))
    res["r0_recovery"] = res["target_fixed"]

    # downstream
    done = []
    for k in expected:
        n0, n1, n2 = R0.get(k), R1.get(k), R2.get(k)
        if _view(n2) == _view(n1):
            done.append({"node": k, "fixed": "no", "how": "unchanged"})
            continue
        if n0 is None or n0["type"] != "synthesis":
            ok = (n2 is not None and n2["status"] == "active") if (n0 and n0["status"] == "active") else True
            done.append({"node": k, "fixed": "yes" if ok else "no", "how": "structural"})
            continue
        j = judge("revision", prompts.JUDGE_REVISION.format(
            node_id=k, gold=json.dumps(_view(n0), ensure_ascii=False), corrupted=json.dumps(_view(n1), ensure_ascii=False),
            revised=json.dumps(_view(n2), ensure_ascii=False), error=case["error"]), "fixed", meta)
        done.append({"node": k, "fixed": j.get("fixed"), "new_error": j.get("introduced_new_error"), "how": "judge"})
    res["downstream"] = done
    if expected:
        sc = sum(1.0 if d["fixed"] == "yes" else 0.5 if d["fixed"] == "partial" else 0.0 for d in done)
        res["downstream_completion"] = round(sc / len(expected), 3)

    # collateral changes -> new errors?
    new_nodes = {f"{k}:{rid}" for k in ("rec", "screen", "ft", "ext", "rob")}
    collateral = []
    for k in sorted(set(R2.nodes)):
        n2 = R2.get(k)
        if n2["type"] not in ("synthesis", "extraction", "rob", "screen") or k in expected or k == case["target"] or k in new_nodes:
            continue
        if _view(R1.get(k)) == _view(n2):
            continue
        n0 = R0.get(k)
        if n0 is None:
            collateral.append({"node": k, "new_error": None, "note": "new output (not in R0)"})
            continue
        j = judge_unchanged(ctx, judge, k, n0, n2, R2, meta)
        collateral.append({"node": k, "new_error": bool(j.get("introduced_new_error"))})
    res["collateral_changes"] = collateral
    res["new_errors"] = sum(1 for c in collateral if c["new_error"]) + sum(1 for d in done if d.get("new_error"))

    # remaining contradictions about this study, relative to the same slice of the gold review R0
    slice_ids = sorted({n["id"] for n in R2.by_type("synthesis", active_only=True) if rid in n["data"]["cites"]}
                       | {k for k in expected if k.startswith("syn:")})
    res["slice"] = slice_ids
    res.update(contradictions(ctx, judge, R2, rid, slice_ids, meta))
    base = _BASELINE.get(case["id"])
    if base is None:
        base = contradictions(ctx, judge, R0, rid, slice_ids, {**meta, "condition": "R0_baseline"})
        _BASELINE[case["id"]] = base
    res["contradictions_baseline_R0"] = base["contradictions"]
    res["contradictions_added"] = max(0, res["contradictions"] - base["contradictions"])
    return res


_BASELINE: dict[str, dict] = {}


def compact_table(store, rids: list[str], max_each: int = 350) -> str:
    """All fields of the given studies, each value capped (never drops a study, unlike truncation)."""
    rows = []
    for rid in rids:
        n = store.get(f"ext:{rid}")
        if not n or n["status"] != "active":
            continue
        rows.append(f"[{rid}] " + json.dumps({k: v["value"][:max_each] for k, v in n["data"]["fields"].items()},
                                             ensure_ascii=False))
    return "\n".join(rows) or "(none)"


def contradictions(ctx: Ctx, judge: Judge, R, rid: str, slice_ids: list[str], meta: dict) -> dict:
    stmts = [R.get(s) for s in slice_ids if R.get(s) and R.get(s)["status"] == "active"]
    if not stmts:
        return {"contradictions": 0, "contradictions_detail": []}
    inc = set(R.included_ids())
    cited = sorted({c for n in stmts for c in n["data"]["cites"]} | ({rid} if rid in inc else set()))
    stm = "\n".join(f"[{n['data']['id']}] {n['data']['text']} (cites: {', '.join(n['data']['cites'])})" for n in stmts)
    excluded = ", ".join(c for c in sorted({c for n in stmts for c in n["data"]["cites"]} | {rid}) if c not in inc)
    j = judge("consistency", prompts.CONSISTENCY.format(table=compact_table(R, [c for c in cited if c in inc]),
                                                       excluded=excluded or "(none)", statements=stm),
              "contradictions", meta)
    cons = j.get("contradictions", []) or []
    return {"contradictions": len(cons), "contradictions_detail": [short(c.get("description", ""), 300) for c in cons[:6]]}


def judge_unchanged(ctx: Ctx, judge: Judge, k: str, n0: dict, n2: dict, R2, meta: dict) -> dict:
    """Evidence-aware: an added citation is only an error if that study's data do not support the statement."""
    ev = ""
    if n2["type"] == "synthesis":
        ev = compact_table(R2, n2["data"]["cites"])
    return judge("unchanged", prompts.JUDGE_UNCHANGED.format(node_id=k, gold=json.dumps(_view(n0), ensure_ascii=False),
                                                            revised=json.dumps(_view(n2), ensure_ascii=False),
                                                            evidence=ev or "(not applicable)"),
                 "introduced_new_error", meta)


# ------------------------------------------------------------------ main
def run(ctx: Ctx, judge: Judge, out_dir, n_per_type: int = 1, labels: dict | None = None, repeats: int = 1,
        resume: dict | None = None) -> dict:
    """ctx = the review to corrupt (R0; in the stage-wise evaluation the gold-input review).
    resume = the partial results of an interrupted run: its cases (with their saved R1 stores) and finished
    runs are kept; only the missing case/condition runs are executed."""
    global _RUNS_DIR
    from pathlib import Path
    _RUNS_DIR = Path(out_dir) / "claim2_runs"
    _BASELINE.clear()
    labels = labels or {}
    prev, done = {}, []
    if resume and resume.get("cases"):
        from ..store import ReviewStore
        for c in resume["cases"]:
            c = dict(c)
            c["R1"] = ReviewStore.load(Path(out_dir) / "claim2_R1" / f"{c['id']}.json")
            prev[c["id"]] = c
        done = [r for r in resume.get("runs", []) if "error" not in r]
        n_built = max(len(list((Path(out_dir) / "claim2_R1").glob("*.json"))), len(resume.get("all_case_ids") or []))
        ctx.log(f"[claim2] resuming: {len(prev)} recorded cases, {len(done)} finished runs kept")
        if n_built <= len(prev):
            return _execute(ctx, judge, out_dir, list(prev.values()), repeats,
                            Path(resume.get("R0_snapshot") or Path(out_dir) / "claim2_R0_store.json"), done)
        ctx.log(f"[claim2] {n_built - len(prev)} cases were built but not yet run: rebuilding them with the same seed")
    rnd = random.Random(ctx.cfg.get("eval", {}).get("seed", 2026) + 1)
    R0 = ctx.store
    r0_path = Path(out_dir) / "claim2_R0_store.json"
    R0.save(r0_path)                     # this experiment's reference; re-scoring always uses this file
    inc = R0.included_ids()
    cited = [r for r in inc if _cites(R0, r)]
    rnd.shuffle(cited)
    has_ft = lambda r: (R0.get(f"ft:{r}") or {}).get("data", {}).get("kind") == "fulltext"
    gold_ft = [r for r in cited if labels.get(r) == 1 and has_ft(r)]       # eligible-study recovery: gold includes only
    cases = []
    pools = {"A": list(cited), "B": list(gold_ft), "C": [r for r in gold_ft if (R0.get(f"rec:{r}") or {}).get("data", {}).get("pmid")],
             "D": list(inc)}
    used = set()
    max_attempts = ctx.cfg.get("eval", {}).get("claim2_build_attempts", 3)
    # eligible-study cases (B, C) first: they need gold includes with a full text, which are scarce;
    # A and D then use the remaining studies
    for typ in ("B", "C", "A", "D"):
        k = attempts = 0
        for rid in pools[typ]:
            if k >= n_per_type or attempts >= max_attempts * n_per_type:
                break
            if rid in used and typ != "D":
                continue
            attempts += 1
            case = (build_A(ctx, rnd, rid) if typ == "A" else build_B(ctx, rid) if typ == "B"
                    else build_C(ctx, rid) if typ == "C" else build_D(ctx, rnd, rid))
            if case and typ == "A" and not any(_view(R0.get(sid)) != _view(case["R1"].get(sid)) for sid in _cites(R0, rid)):
                ctx.log(f"[claim2] A case on {rid}: corruption did not reach the synthesis; trying another study")
                continue
            if case:
                case["id"] = f"{typ}{k + 1}_{rid}"
                cases.append(case)
                pools["A"] = [r for r in pools["A"] if r != rid] if typ in "BC" else pools["A"]
                used.add(rid)
                k += 1
    for k in range(n_per_type):
        e = build_E(ctx, rnd, labels, used)
        if e:
            e["id"] = f"E{k + 1}_{e['rid']}"
            used.add(e["rid"])
            cases.append(e)
    cases.sort(key=lambda c: "ABCDE".index(c["type"][0]))
    if prev:   # keep the recorded definitions (and R1) of cases that already ran; rebuilt ones are new to run
        want = set(resume.get("all_case_ids") or [])
        missing = sorted(want - {c["id"] for c in cases})
        if missing:
            ctx.log(f"[claim2] rebuilt cases differ from the recorded ids; not reproduced: {missing}")
        cases = [prev.get(c["id"], c) for c in cases] + [c for i, c in prev.items() if i not in {x["id"] for x in cases}]
    for case in cases:
        if case["id"] not in prev:
            gold_agreement(ctx, judge, case, R0, labels)
            case["R1"].save(Path(out_dir) / "claim2_R1" / f"{case['id']}.json")
            if prev:
                case["rebuilt_on_resume"] = True
    ctx.log(f"[claim2] built {len(cases)} cases: {[(c['id'], c.get('primary')) for c in cases]}")
    return _execute(ctx, judge, out_dir, cases, repeats, r0_path, done)


def _execute(ctx: Ctx, judge: Judge, out_dir, cases: list[dict], repeats: int, r0_path, done_runs: list[dict]) -> dict:
    R0 = ctx.store

    conds = {"A": [("local_edit", True, False), ("dependency_aware", True, True)],
             "B": [("local_edit", True, False), ("dependency_aware", True, True)],
             "C": [("local_edit", True, False), ("dependency_aware", True, True)],
             "D": [("verify_off", False, True), ("verify_on", True, True)],
             "E": [("dependency_aware", True, True)]}
    results = {"R0_snapshot": str(r0_path), "cases": [{k: v for k, v in c.items() if k != "R1"} for c in cases],
               "runs": list(done_runs), "repeats": repeats,   # every case recorded up front (resumable)
               "all_case_ids": [c["id"] for c in cases]}
    finished = {(r["case"], r["condition"], r.get("repeat", 0)) for r in done_runs}
    for case in cases:
        for rep in range(repeats):
            for cond, verify, prop in conds[case["type"][0]]:
                if (case["id"], cond, rep) in finished:
                    continue
                sub = f"{case['id']}_{cond}" + (f"_r{rep}" if repeats > 1 else "")
                c = _sub_ctx(ctx, case["R1"], sub)
                try:
                    cs = feedback.handle_feedback(c, case["feedback"], target=case.get("target") if case["type"][0] not in "CE" else None,
                                                  pmid=case.get("pmid") if case["type"][0] in "CE" else None,
                                                  verify=verify, propagate=prop, auto_accept=True,
                                                  case_id=sub, tag_prefix=f"claim2_{cond}")
                    r = score_case(ctx, judge, case, R0, case["R1"], c.store, cs, cond)
                except Exception as e:  # keep the experiment going; record the failure
                    if type(e).__name__ in ("BudgetExceeded", "QuotaExhausted"):
                        raise
                    r = {"case": case["id"], "type": case["type"], "condition": cond, "error": str(e)[:300]}
                r["repeat"] = rep
                r["primary"] = case.get("primary", True)
                results["runs"].append(r)
                ctx.log(f"[claim2] {sub}: R0-recovery={r.get('r0_recovery')} source={r.get('source_correctness')} "
                        f"downstream={r.get('downstream_completion')} new_err={r.get('new_errors')} "
                        f"accepted_false={r.get('accepted_false_feedback')} ${r.get('usd')}")
                save(out_dir, "claim2_partial", results)
    results["aggregate"] = aggregate(results["runs"])
    results["aggregate_flagged_cases"] = aggregate([r for r in results["runs"] if not r.get("primary", True)], primary_only=False)
    results["design"] = design(results)
    results["paired"] = paired_analysis(results["runs"])
    return results


def design(results: dict) -> dict:
    cases = results["cases"]
    from collections import Counter
    return {"unique_cases": len(cases), "unique_papers": len({c["rid"] for c in cases}),
            "cases_by_type": dict(Counter(c["type"][0] for c in cases)),
            "primary_cases": sum(1 for c in cases if c.get("primary")),
            "flagged_cases": [{"id": c["id"], "external_gold": c.get("external_gold"),
                               "r0_source_check": (c.get("r0_source_check") or {}).get("label")}
                              for c in cases if not c.get("primary")],
            "repeats": results.get("repeats", 1), "same_R1_for_both_conditions": True}


def paired_analysis(runs: list[dict]) -> dict:
    from .stats import paired
    out = {}
    def unit(cond, key, types):
        acc = {}
        for r in runs:
            if r.get("condition") == cond and r["type"][0] in types and r.get("primary", True) and "error" not in r:
                v = r.get(key)
                if key == "target_fixed":
                    v = 1.0 if v == "yes" else 0.5 if v == "partial" else 0.0
                elif key == "source_correct":
                    v = 1.0 if v else 0.0
                if v is not None:
                    acc.setdefault(r["case"], []).append(float(v))
        return {u: sum(v) / len(v) for u, v in acc.items()}
    for key in ("downstream_completion", "target_fixed", "source_correct"):
        out[key] = paired(unit("local_edit", key, "ABC"), unit("dependency_aware", key, "ABC"), "local_edit", "dependency_aware")
    out["D_accepted_false"] = paired({u: v for u, v in _acc(runs, "verify_off").items()}, _acc(runs, "verify_on"),
                                     "verify_off", "verify_on")
    out["unit"] = "unique case (primary cases only; repeats averaged within the case)"
    return out


def _acc(runs, cond):
    acc = {}
    for r in runs:
        if r.get("condition") == cond and r["type"][0] == "D" and r.get("primary", True) and "error" not in r:
            acc.setdefault(r["case"], []).append(1.0 if r.get("accepted_false_feedback") else 0.0)
    return {u: sum(v) / len(v) for u, v in acc.items()}


def aggregate(runs: list[dict], primary_only: bool = True) -> dict:
    agg = {}
    if primary_only:
        runs = [r for r in runs if r.get("primary", True)]
    for r in runs:
        if "error" in r:
            continue
        key = f"{r['type'][0]}:{r['condition']}"
        a = agg.setdefault(key, {"n": 0, "target_fixed": 0, "downstream": [], "new_errors": 0, "contradictions": 0,
                                 "accepted_false": 0, "usd": 0.0, "seconds": 0.0})
        a["n"] += 1
        a["target_fixed"] += 1 if r.get("target_fixed") == "yes" else 0.5 if r.get("target_fixed") == "partial" else 0
        if r.get("downstream_completion") is not None:
            a["downstream"].append(r["downstream_completion"])
        a["new_errors"] += r.get("new_errors", 0) or 0
        a["contradictions"] += r.get("contradictions", 0) or 0
        a["contradictions_added"] = a.get("contradictions_added", 0) + (r.get("contradictions_added", 0) or 0)
        a["accepted_false"] += 1 if r.get("accepted_false_feedback") else 0
        a["usd"] += r.get("usd") or 0
        a["seconds"] += r.get("seconds") or 0
        a["source_correct"] = a.get("source_correct", 0) + (1 if r.get("source_correct") else 0)
        a["cases"] = sorted(set(a.get("cases", [])) | {r["case"]})
    for k, a in agg.items():
        a["unique_cases"] = len(a.pop("cases", []))
        a["r0_recovery_rate"] = round(a["target_fixed"] / a["n"], 3) if k[0] in "ABC" else None
        a["source_correct_rate"] = round(a["source_correct"] / a["n"], 3) if k[0] in "ABC" else None
        a["target_fixed_rate"] = round(a["target_fixed"] / a["n"], 3) if k[0] in "ABC" else None
        a["downstream_completion_mean"] = round(sum(a["downstream"]) / len(a["downstream"]), 3) if a["downstream"] else None
        a["usd_per_revision"] = round(a["usd"] / a["n"], 4)
        a["seconds_per_revision"] = round(a["seconds"] / a["n"], 1)
        a["accepted_false_rate"] = round(a["accepted_false"] / a["n"], 3)
    # pooled by condition across A-C
    pooled = {}
    for cond in ("local_edit", "dependency_aware"):
        rs = [r for r in runs if r.get("condition") == cond and r["type"][0] in "ABC" and "error" not in r]
        if rs:
            ds = [r["downstream_completion"] for r in rs if r.get("downstream_completion") is not None]
            tf = sum(1 for r in rs if r.get("target_fixed") == "yes")
            scc = sum(1 for r in rs if r.get("source_correct"))
            pooled[cond] = {"n_runs": len(rs), "unique_cases": len({r["case"] for r in rs}),
                            "r0_recovery_target": f"{tf}/{len(rs)}", "r0_recovery_target_95ci": wilson(tf, len(rs)),
                            "source_correct_target": f"{scc}/{len(rs)}", "source_correct_target_95ci": wilson(scc, len(rs)),
                            "downstream_completion_mean": round(sum(ds) / len(ds), 3) if ds else None,
                            "contradictions_total": sum(r.get("contradictions", 0) for r in rs),
                            "contradictions_baseline_R0_total": sum(r.get("contradictions_baseline_R0", 0) or 0 for r in rs),
                            "contradictions_added_total": sum(r.get("contradictions_added", 0) or 0 for r in rs),
                            "new_errors_strict_total": sum(r.get("new_errors_strict", r.get("new_errors", 0)) or 0 for r in rs),
                            "new_errors_total": sum(r.get("new_errors", 0) for r in rs),
                            "usd_per_revision": round(sum(r.get("usd") or 0 for r in rs) / len(rs), 4),
                            "seconds_per_revision": round(sum(r.get("seconds") or 0 for r in rs) / len(rs), 1)}
    agg["pooled_A_to_C"] = pooled
    return agg


def rescore(ctx: Ctx, judge: Judge, out_dir, results: dict, src_dir) -> dict:
    """Re-score finished Claim-2 runs without re-running the agent: recompute contradictions against
    the R0 baseline (full slice, no truncation) and re-judge every 'new error' with the evidence-aware
    judge. Uses THAT experiment's own R0 snapshot (claim2_R0_store.json) and revised stores
    (claim2_runs/<case>_<condition>/store.json) from `src_dir`, never the current review."""
    from pathlib import Path
    from ..store import ReviewStore
    src_dir = Path(src_dir)
    r0p = Path(results.get("R0_snapshot") or src_dir / "claim2_R0_store.json")
    if not r0p.exists():
        r0p = src_dir / "claim2_R0_store.json"
    if not r0p.exists():
        raise FileNotFoundError(f"R0 snapshot of the original experiment not found ({r0p}); refusing to re-score "
                                "against a different review")
    R0 = ReviewStore.load(r0p)
    _BASELINE.clear()
    results["rescored_against"] = str(r0p)
    cases = {c["id"]: c for c in results["cases"]}
    for r in results["runs"]:
        if "error" in r or r["type"][0] not in "ABC":
            continue
        sub = f"{r['case']}_{r['condition']}" + (f"_r{r['repeat']}" if results.get("repeats", 1) > 1 else "")
        p = src_dir / "claim2_runs" / sub / "store.json"
        if not p.exists():
            continue
        R2 = ReviewStore.load(p)
        case = cases[r["case"]]
        meta = {"claim": 2, "case": r["case"], "condition": r["condition"], "rescore": True}
        slice_ids = sorted({n["id"] for n in R2.by_type("synthesis", active_only=True) if case["rid"] in n["data"]["cites"]}
                           | {k for k in r.get("expected_downstream", []) if k.startswith("syn:")})
        r["slice"] = slice_ids
        r["contradictions_truncated_v1"] = r.get("contradictions")
        r.update(contradictions(ctx, judge, R2, case["rid"], slice_ids, meta))
        base = _BASELINE.get(r["case"])
        if base is None:
            base = contradictions(ctx, judge, R0, case["rid"], slice_ids, {**meta, "condition": "R0_baseline"})
            _BASELINE[r["case"]] = base
        r["contradictions_baseline_R0"] = base["contradictions"]
        r["contradictions_added"] = max(0, r["contradictions"] - base["contradictions"])
        strict = r.get("new_errors", 0)
        n_new = 0
        for item in r.get("downstream", []) + r.get("collateral_changes", []):
            if item.get("new_error"):
                n0, n2 = R0.get(item["node"]), R2.get(item["node"])
                if n0 and n2:
                    j = judge_unchanged(ctx, judge, item["node"], n0, n2, R2, meta)
                    item["new_error_strict"] = True
                    item["new_error"] = bool(j.get("introduced_new_error"))
                    item["new_error_rationale"] = short(j.get("rationale", ""), 300)
                n_new += bool(item.get("new_error"))
        r["new_errors_strict"] = strict
        r["new_errors"] = n_new
    results["aggregate"] = aggregate(results["runs"])
    results["paired"] = paired_analysis(results["runs"])
    return results
