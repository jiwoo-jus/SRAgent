"""Characterization (golden) tests: what the review looks like after each scenario, frozen before refactoring.

Each scenario runs the pipeline offline (deterministic mock LLM, no network) on a small record set built to
exercise every path (title/abstract exclusion, full-text exclusion, a record waiting for its full text, a rule
exclusion), then applies one operation (feedback, a person's decision, a protocol edit, a re-run, a PDF, ...).
The outcome is compared with tests/golden/<scenario>.json as a *semantic* view: decisions, included studies,
extracted values, risk-of-bias judgments, synthesis statements and their citations, open flags, provenance and
the change sets. Store internals (node versions, edges, timestamps, costs) are not part of it, so a refactor
that keeps the behaviour keeps these tests green.

A scenario named *_known_gap freezes a current limitation (e.g. an added criterion is not assessed for records
already screened). A refactor that fixes it is expected to change that golden file, on purpose.

Update the golden files after an intended change:   SRAGENT_UPDATE_GOLDEN=1 pytest -q tests/test_golden.py
then review the diff of tests/golden/ before committing it.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))

from mock_llm import mock_fn  # noqa: E402

from sragent.config import load_config  # noqa: E402
from sragent.context import Ctx  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.environ.get("SRAGENT_UPDATE_GOLDEN") == "1"

# ---------------------------------------------------------------- records: one per path through the pipeline
RECORDS = [
    {"rid": "G01", "pmid": "G01", "title": "Pharmacokinetics of emicizumab in adults with haemophilia A",
     "abstract": "Emicizumab was given to 12 participants. Trough concentrations were 52 ug/mL. Half-life was 27 days."},
    {"rid": "G02", "pmid": "G02", "title": "Population pharmacokinetics of emicizumab in children",
     "abstract": "A population PK model in 60 children with haemophilia A. Clearance was 0.27 L/day."},
    {"rid": "G03", "pmid": "G03", "title": "Emicizumab exposure and bleeding in a registry",
     "abstract": "Emicizumab exposure was described in 140 patients. Annualised bleeding rates fell."},       # I3 unclear
    {"rid": "G04", "pmid": "G04", "title": "Emicizumab prophylaxis and joint bleeds: a case series",
     "abstract": "Emicizumab prophylaxis in 30 patients. Bleeding rate fell to 1.5 per year."},           # I3 not met
    {"rid": "G05", "pmid": "G05", "title": "Factor VIII gene therapy outcomes",
     "abstract": "Valoctocogene roxaparvovec in 134 men."},                                                # I1 not met
    {"rid": "G06", "pmid": "G06", "title": "Emicizumab pharmacokinetics in a non-clinical study",
     "abstract": "Emicizumab pharmacokinetics and trough concentrations were characterised."},            # excluded at full text
    {"rid": "G07", "pmid": "G07", "title": "Emicizumab pharmacokinetics: oral presentation abstracts",
     "abstract": "", "pub_types": ["Congress"]},                                                          # rule exclusion
]
FULLTEXTS = {   # the full texts a person supplied (no network in tests); G03 has none: it waits for one
    "G01": "Emicizumab was given to 12 adult participants. Trough concentrations were 52 ug/mL. Half-life was 27 days.",
    "G02": "Emicizumab population PK model in 60 children with haemophilia A. Clearance was 0.27 L/day and half-life 30 days.",
    "G04": "Emicizumab prophylaxis in 30 patients. Bleeding rate fell to 1.5 per year. No drug levels were measured.",
    "G06": "Emicizumab pharmacokinetics were characterised in 8 cynomolgus monkeys; trough concentrations were 40 ug/mL.",
}

# ---------------------------------------------------------------- a mock that judges each criterion on its own
_PK = re.compile(r"pharmacokinetic|\bpk\b|concentration|clearance|half-life|trough", re.I)
_UNCLEAR_PK = re.compile(r"\bexposure\b", re.I)
_ANIMAL = re.compile(r"monkey|cynomolgus|\bmice\b|\brats?\b|animal", re.I)
_DRUG = re.compile(r"emicizumab|ace910", re.I)
_TOPIC_WORDS = {"children": re.compile(r"child|paediatric|pediatric", re.I),
                "adults": re.compile(r"adult|\bmen\b|volunteer", re.I),
                "bleeding": re.compile(r"bleed", re.I)}


def golden_mock(messages, mode):
    """Like tests/mock_llm.py, but screening judges each criterion separately (so a protocol change can flip
    one criterion), criteria added later are judged from their wording, and extracted values carry the
    study's own numbers (so statements and corrections differ per study)."""
    u = messages[-1]["content"]
    if "Assess this record against each eligibility criterion" in u:
        crit_part = u.split("CRITERIA:", 1)[1].split("RECORD (", 1)[0]
        rec = u.split("RECORD (", 1)[1]
        out = []
        for cid, ctype, text in re.findall(r"^- (\w+) \((\w+)\): (.*)$", crit_part, flags=re.M):
            if cid == "I1":
                a = "met" if _DRUG.search(rec) else "not_met"
            elif cid == "I2":
                a = "not_met" if _ANIMAL.search(rec) else "met"
            elif cid == "I3":
                a = "met" if _PK.search(rec) else ("unclear" if _UNCLEAR_PK.search(rec) else "not_met")
            else:   # a criterion added later: judged from its wording
                hit = next((rx for w, rx in _TOPIC_WORDS.items() if w in text.lower()), None)
                applies = bool(hit and hit.search(rec))
                a = "met" if applies else ("not_met" if ctype == "exclusion" or hit else "met")
            out.append({"id": cid, "assessment": a, "evidence": ["s1"]})
        return json.dumps({"criteria": out, "decision": "include", "reason": "mock", "confidence": 0.9})
    text = mock_fn(messages, mode)
    if "Extract the following items" in u:
        src = u.split('"""', 1)[1]
        m = re.search(r"(\d+) (?:adult )?(participants|children|patients|men|monkeys)", src)
        if m:
            text = text.replace("12 participants", f"{m.group(1)} {m.group(2)}")
        if "REVIEWER NOTE" in u:   # a re-run with a person's note reads the source again: different values
            text = text.replace(' value ', ' value (re-read) ')
    return text


# ---------------------------------------------------------------- setup
def build(tmp_path, fulltexts=FULLTEXTS, **cfg_over) -> Ctx:
    recf = tmp_path / "recs.jsonl"
    recf.write_text("\n".join(json.dumps(r) for r in RECORDS))
    fdir = tmp_path / "run" / "fulltext"
    fdir.mkdir(parents=True, exist_ok=True)
    for rid, body in fulltexts.items():
        (fdir / f"{rid}.txt").write_text(f"## Title\n{rid}\n## Methods\n" + (body + " ") * 25)
    over = {"run_dir": str(tmp_path / "run"), "cache_dir": str(tmp_path / "cache"), "budget_usd": 1.0,
            "llm_defaults": {"provider": "mock", "model": "mock", "concurrency": 1},
            "pipeline": {"fulltext": False, "validate_terms": False, "citation_chasing": False},
            "topic": {"records": {"source": "file", "file": str(recf)}}}
    cfg = load_config(ROOT / "configs/benchmark/emicizumab_pk/topic.yaml", _merge(over, cfg_over))
    ctx = Ctx.create(cfg, mock_fn=golden_mock)
    ctx.quiet = True
    return ctx


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def reviewed(tmp_path, fulltexts=FULLTEXTS, **cfg_over) -> Ctx:
    from sragent import pipeline
    ctx = build(tmp_path, fulltexts, **cfg_over)
    pipeline.run(ctx)
    return ctx


# ---------------------------------------------------------------- the semantic view
def view(ctx) -> dict:
    from sragent import provenance
    from sragent.stages.report import prisma
    st = ctx.store
    rid = lambda nid: nid.split(":", 1)[1]
    records = {}
    for n in st.by_type("screen"):
        d = n["data"]
        records[rid(n["id"])] = {
            "decision": d.get("decision"), "stage": d.get("stage"), "because": sorted(d.get("because") or []),
            "criteria": {a["id"]: a.get("assessment") for a in d.get("criteria") or [] if isinstance(a, dict)},
            "by": "rule" if d.get("rule") else "person" if d.get("human_decision") else "agent",
            "status": n["status"]}
    ext = {rid(n["id"]): {"status": n["status"], "fields": {k: (v or {}).get("value") for k, v in sorted(n["data"].get("fields", {}).items())}}
           for n in st.by_type("extraction")}
    rob = {rid(n["id"]): {"status": n["status"], "overall": n["data"].get("overall"),
                          "domains": {x.get("domain"): x.get("judgment") for x in n["data"].get("domains", [])}}
           for n in st.by_type("rob")}
    syn = {rid(n["id"]): {"status": n["status"], "text": n["data"].get("text"), "cites": sorted(n["data"].get("cites") or [])}
           for n in st.by_type("synthesis")}
    flags = {n["id"]: sum(1 for f in n["flags"] if f["status"] == "open") for n in st.nodes.values()
             if any(f["status"] == "open" for f in n["flags"])}
    prov = {}
    for e in provenance.events(ctx.run_dir):
        k = f"{e['category']}:{e['action']}"
        prov[k] = prov.get(k, 0) + 1
    crit = {c["id"]: {"type": c["type"], "stage": c.get("stage", "title_abstract"), "text": c["text"]}
            for c in (st.get("protocol") or {}).get("data", {}).get("criteria", [])}
    return {"criteria": crit, "records": dict(sorted(records.items())), "included": st.included_ids(),
            "prisma": prisma(ctx), "extraction": dict(sorted(ext.items())), "rob": dict(sorted(rob.items())),
            "synthesis": dict(sorted(syn.items())), "open_flags": dict(sorted(flags.items())), "provenance": dict(sorted(prov.items()))}


def cs_view(cs: dict) -> dict:
    return {"kind": cs.get("kind"), "status": cs.get("status"), "verdict": cs.get("verdict"),
            "changes": sorted({(c["node"], c.get("role", "")) for c in cs.get("changes", [])})}


def check(name: str, data: dict):
    """Compare with tests/golden/<name>.json (write it with SRAGENT_UPDATE_GOLDEN=1)."""
    data = json.loads(json.dumps(data, default=list, sort_keys=True))
    p = GOLDEN / f"{name}.json"
    if UPDATE or not p.exists():
        if not UPDATE:
            pytest.fail(f"no golden file {p.name}; create it with SRAGENT_UPDATE_GOLDEN=1 and review it")
        GOLDEN.mkdir(exist_ok=True)
        p.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        return
    want = json.loads(p.read_text())
    diffs = list(_diff(want, data))
    assert not diffs, f"{name}: behaviour changed ({len(diffs)} difference(s)):\n  " + "\n  ".join(diffs[:25]) + \
        "\nIf intended, update with SRAGENT_UPDATE_GOLDEN=1 and review the diff of tests/golden/."


def _diff(a, b, path="$"):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in b:
                yield f"{path}.{k}: missing now (was {json.dumps(a[k])[:120]})"
            elif k not in a:
                yield f"{path}.{k}: new {json.dumps(b[k])[:120]}"
            else:
                yield from _diff(a[k], b[k], f"{path}.{k}")
    elif a != b:
        yield f"{path}: {json.dumps(a)[:120]} -> {json.dumps(b)[:120]}"


# ---------------------------------------------------------------- scenarios
def test_golden_pipeline(tmp_path):
    """The whole pipeline: every path (rule, title/abstract, full text, waiting for a PDF) and the outputs."""
    ctx = reviewed(tmp_path)
    check("pipeline", view(ctx))


def test_golden_feedback_dependency_aware(tmp_path):
    """Corrected extraction value, propagated to the statements that cite it."""
    from sragent import feedback
    ctx = reviewed(tmp_path)
    cs = feedback.handle_feedback(ctx, "The sample size of G01 is wrong", target="ext:G01", propagate=True, auto_accept=True)
    check("feedback_dependency_aware", {"change_set": cs_view(cs), "review": view(ctx)})


def test_golden_feedback_local_edit(tmp_path):
    """The same correction without propagation: only the target changes."""
    from sragent import feedback
    ctx = reviewed(tmp_path)
    cs = feedback.handle_feedback(ctx, "The sample size of G01 is wrong", target="ext:G01", propagate=False, auto_accept=True)
    check("feedback_local_edit", {"change_set": cs_view(cs), "review": view(ctx)})


def test_golden_feedback_unsupported(tmp_path):
    """Feedback the source contradicts: refused, nothing changes, the output stays flagged."""
    from sragent import feedback
    ctx = reviewed(tmp_path)
    cs = feedback.handle_feedback(ctx, "WRONG: the sample size is 999", target="ext:G02", auto_accept=True)
    check("feedback_unsupported", {"change_set": cs_view(cs), "review": view(ctx)})


def test_golden_feedback_proposal_accept_reject(tmp_path):
    """A proposal waits for the reviewer; one is accepted, one rejected."""
    from sragent import feedback
    ctx = reviewed(tmp_path)
    a = feedback.handle_feedback(ctx, "Population of G02 is wrong", target="ext:G02", auto_accept=False)
    b = feedback.handle_feedback(ctx, "Population of G01 is wrong", target="ext:G01", auto_accept=False)
    feedback.accept(ctx, a["id"])
    feedback.reject(ctx, b["id"], note="keep it")
    check("feedback_proposal_accept_reject", {"proposed": [cs_view(a), cs_view(b)], "review": view(ctx)})


def test_golden_feedback_eligibility_flip(tmp_path):
    """Feedback that a title/abstract exclusion was wrong: re-assessed and forwarded; its full text is
    screened by the next run (not right away), and the outputs follow."""
    from sragent import feedback, pipeline
    ctx = reviewed(tmp_path)
    cs = feedback.handle_feedback(ctx, "G04 should not have been excluded: it reports drug levels.", target="screen:G04",
                                  propagate=True, auto_accept=True)
    after_feedback = view(ctx)
    pipeline.run(ctx)
    check("feedback_eligibility_flip", {"change_set": cs_view(cs), "after_feedback": after_feedback, "after_next_run": view(ctx)})


def test_golden_person_excludes_then_sync(tmp_path):
    """A person excludes an included study; 'Update now' retires its outputs and revises the statements."""
    from sragent import actions, sync
    ctx = reviewed(tmp_path)
    r = actions.edit(ctx, {"kind": "screen", "rid": "G02", "decision": "exclude", "criterion": "I2", "note": "paediatric only"})
    pending = sync.pending_changes(ctx)
    sync.sync_dependents(ctx)
    check("person_excludes_then_sync", {"edit_ok": r["ok"], "pending": pending, "review": view(ctx)})


def test_golden_protocol_remove_criterion(tmp_path):
    """Protocol edit that drops a criterion: decisions are re-derived; a newly forwarded record is screened
    at full text by the next run."""
    from sragent import pipeline, todos
    ctx = reviewed(tmp_path)
    p = yaml.safe_load(open(ctx.run_dir / "protocol.yaml"))
    p["criteria"] = [c for c in p["criteria"] if c["id"] != "I3"]
    (ctx.run_dir / "protocol.yaml").write_text(yaml.safe_dump(p, sort_keys=False))
    r = todos.apply(ctx, {"todo_id": "protocol", "option": "edited"})
    after_rescreen = view(ctx)
    pipeline.run(ctx)
    check("protocol_remove_criterion", {"message": r["message"], "after_rescreen": after_rescreen, "after_next_run": view(ctx)})


def test_golden_protocol_add_criterion_known_gap(tmp_path):
    """Protocol edit that adds an exclusion criterion. Today records already screened are not assessed on it
    (a missing assessment counts as passed): nothing changes for them. Refactor step 2 is meant to change this."""
    from sragent import pipeline, todos
    ctx = reviewed(tmp_path)
    p = yaml.safe_load(open(ctx.run_dir / "protocol.yaml"))
    p["criteria"].append({"id": "E1", "type": "exclusion", "text": "studies only in children", "stage": "title_abstract"})
    (ctx.run_dir / "protocol.yaml").write_text(yaml.safe_dump(p, sort_keys=False))
    r = todos.apply(ctx, {"todo_id": "protocol", "option": "edited"})
    pipeline.run(ctx)
    check("protocol_add_criterion_known_gap", {"message": r["message"], "review": view(ctx)})


def test_golden_manual_edit_then_propagate(tmp_path):
    """A person edits an extracted value; the agent proposes updates to what depends on it."""
    from sragent import actions, feedback
    ctx = reviewed(tmp_path)
    r = actions.edit(ctx, {"kind": "ext", "rid": "G01", "fields": {"sample_size": "14 participants"}, "note": "Table 1"})
    cs = actions.propagate_from(ctx, "ext:G01")
    if cs["status"] == "proposed":
        feedback.accept(ctx, cs["id"])
    check("manual_edit_then_propagate", {"edit_ok": r["ok"], "change_set": cs_view(cs), "review": view(ctx)})


def test_golden_rerun_extraction(tmp_path):
    """A person asks for a fresh extraction of one study with a note; the proposal is accepted."""
    from sragent import actions, feedback
    ctx = reviewed(tmp_path)
    cs = actions.rerun(ctx, "G02", "extract", note="use table 2")
    if cs["status"] == "proposed":
        feedback.accept(ctx, cs["id"])
    check("rerun_extraction", {"change_set": cs_view(cs), "review": view(ctx)})


def test_golden_pdf_supplied_then_sync(tmp_path):
    """The waiting record's full text arrives: screened on it, then extracted, assessed and added to the synthesis."""
    from sragent import sync
    from sragent.stages import fulltext
    ctx = reviewed(tmp_path)
    (fulltext.fulltext_dir(ctx) / "G03.txt").write_text(
        "## Methods\n" + "Emicizumab pharmacokinetics in 140 patients: trough concentrations were 48 ug/mL. " * 30)
    res = sync.process_pdfs(ctx)
    pending = sync.pending_changes(ctx)
    sync.sync_dependents(ctx)
    check("pdf_supplied_then_sync", {"processed": res.get("processed"), "pending": pending, "review": view(ctx)})


def test_golden_agent_only(tmp_path):
    """Agent-only condition: a record still unclear after its full text is included and flagged; a person's
    judgment is refused."""
    from sragent import actions
    unclear = {**FULLTEXTS, "G03": "Emicizumab exposure was described in 140 patients; bleeding rates fell."}
    ctx = reviewed(tmp_path, unclear, pipeline={"human_mode": "agent_only", "fulltext_unclear": "include"})
    try:
        r = actions.edit(ctx, {"kind": "screen", "rid": "G02", "decision": "exclude", "criterion": "I2", "note": "x"})
        person = {"raised": None, "ok": r.get("ok")}
    except Exception as e:  # provenance.NotAllowed
        person = {"raised": type(e).__name__, "ok": False}
    check("agent_only", {"person_decision": person, "review": view(ctx)})
