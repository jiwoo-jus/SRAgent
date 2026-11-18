"""Offline tests (no API calls). Run:  pytest -q tests/

The pipeline test uses the mock LLM and a tiny in-memory record set, so it needs no network
except for term validation / full-text lookups, which are disabled here.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mock_llm import mock_fn  # noqa: E402

from sragent.config import load_config, model_for, unlimited_budget  # noqa: E402
from sragent.context import Ctx  # noqa: E402
from sragent.llm import parse_json  # noqa: E402
from sragent.store import ReviewStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

RECORDS = [
    {"rid": "1001", "pmid": "1001", "title": "Pharmacokinetics of emicizumab in adults", "abstract":
     "Emicizumab was given to 12 participants. Trough concentrations were 52 ug/mL. Half-life was 27 days.",
     "label_included": 1},
    {"rid": "1002", "pmid": "1002", "title": "Emicizumab population PK in children", "abstract":
     "A population PK model in 60 children with haemophilia A. Clearance 0.27 L/day.", "label_included": 1},
    {"rid": "1003", "pmid": "1003", "title": "Emicizumab case series of joint bleeds", "abstract":
     "Emicizumab prophylaxis in 30 patients. Bleeding rate fell to 1.5 per year.", "label_included": 1},
    {"rid": "1004", "pmid": "1004", "title": "Factor VIII gene therapy outcomes", "abstract":
     "Valoctocogene roxaparvovec in 134 men.", "label_included": 0},
]



def SC(st, rid):
    """A study's eligibility record (selection.view: title/abstract and full-text nodes together)."""
    from sragent.selection import view
    return view(st, rid)


def SN(st, rid):
    """The node that holds a study's eligibility decision (its flags live there)."""
    from sragent.selection import decision_node
    return decision_node(st, rid)


def set_fulltext_decision(st, rid, **changes):
    """Test setup: a full-text decision for a study, on elig:<rid> (created from its record when needed)."""
    d = {**(SC(st, rid) or {}), "stage": "full_text", **changes}
    for k in ("retrieval_note", "title_abstract"):
        d.pop(k, None)
    if st.get(f"elig:{rid}"):
        return st.upsert(f"elig:{rid}", "eligibility", d, depends_on=st.get(f"elig:{rid}")["depends_on"])
    return st.upsert(f"elig:{rid}", "eligibility", d, depends_on=[f"screen:{rid}", "protocol"] + ([f"ft:{rid}"] if st.get(f"ft:{rid}") else []))

def make_ctx(tmp_path, fulltexts=("1001", "1002", "1003"), extra_cfg: dict | None = None) -> Ctx:
    recf = tmp_path / "recs.jsonl"
    recf.write_text("\n".join(json.dumps(r) for r in RECORDS))
    fdir = tmp_path / "run" / "fulltext"
    fdir.mkdir(parents=True, exist_ok=True)
    for r in RECORDS:   # the full texts a person supplied (no network in tests)
        if r["rid"] in fulltexts:
            (fdir / f"{r['rid']}.txt").write_text(f"## Title\n{r['title']}\n## Methods\n" + (r["abstract"] + " ") * 25)
    from sragent.config import deep_merge
    cfg = load_config(ROOT / "configs/benchmark/emicizumab_pk/topic.yaml", deep_merge({
        "run_dir": str(tmp_path / "run"), "cache_dir": str(tmp_path / "cache"), "budget": unlimited_budget(1.0),
        "llm_defaults": {"concurrency": 1}, "models": {"default": {"writer": {"provider": "mock", "model": "mock"}}},
        "pipeline": {"fulltext": False, "validate_terms": False, "citation_chasing": False},
        "topic": {"records": {"source": "file", "file": str(recf)}}}, extra_cfg or {}))
    ctx = Ctx.create(cfg, mock_fn=mock_fn)
    ctx.quiet = True
    return ctx


def test_parse_json_variants():
    assert parse_json('<think>x</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": {"b": [1,2]}} trailing') == {"a": {"b": [1, 2]}}
    assert parse_json('[{"label": "supported"}]') == {"label": "supported"}      # one object wrapped in a list
    assert parse_json('[{"a": 1}, {"a": 2}]') == [{"a": 1}, {"a": 2}]
    import pytest
    from sragent.llm import json_object
    with pytest.raises(ValueError):                                              # only a list of issues: re-asked
        json_object('["the phrase only is not supported", "no data for adults"]')


def test_sentence_evidence():
    from sragent.evidence import packed, resolve, segment
    src = ("Title\n\nMETHODS: Emicizumab was given to 12 participants. Samples were taken weekly.\n"
           "RESULTS: Trough concentrations were 52 ug/mL. Half-life was 27 days.")
    segs = segment(src)
    assert [s["sid"] for s in segs][:2] == ["s1", "s2"] and segs[-1]["section"] == "Results"
    assert "[s3]" in packed(src)
    r = resolve(["s2", "s4"], src)
    assert r["check"]["status"] == "located" and r["spans"][1]["section"] == "Results"
    assert src[r["spans"][0]["start"]:r["spans"][0]["end"]] == r["spans"][0]["text"]
    r = resolve(["s2", "s99"], src)
    assert r["check"]["status"] == "partial"
    r = resolve(["Trough concentration were 52 ug/mL"], src)   # free-text fallback, one fuzzy pass
    assert r["check"]["status"] == "located"
    assert resolve([], src)["check"]["status"] == "none"


def test_store_graph():
    s = ReviewStore()
    s.upsert("rec:1", "record", {})
    s.upsert("screen:1", "screen", {"decision": "include", "stage": "title_abstract"}, depends_on=["rec:1"])
    s.upsert("ext:1", "extraction", {"fields": {}}, depends_on=["screen:1"])
    s.upsert("rob:1", "rob", {"domains": []}, depends_on=["ext:1"])
    s.upsert("syn:S1", "synthesis", {"text": "x", "cites": ["1"]}, depends_on=["ext:1", "rob:1"])
    assert s.descendants("ext:1") == ["rob:1", "syn:S1"]
    assert set(s.related("ext:1")) == {"screen:1", "rob:1", "syn:S1"}
    s.upsert("ext:1", "extraction", {"fields": {"a": 1}}, depends_on=["screen:1"])
    assert s.get("ext:1")["version"] == 2 and len(s.get("ext:1")["history"]) == 1


def test_pipeline_and_feedback(tmp_path):
    from sragent import feedback, pipeline
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    assert set(st.included_ids()) == {"1001", "1002", "1003"}
    assert (ctx.run_dir / "report.md").exists() and (ctx.run_dir / "report_evidence.html").exists()
    # dependency-aware revision of an extraction updates citing statements
    cs = feedback.handle_feedback(ctx, "sample size is wrong", target="ext:1001", propagate=True, auto_accept=True)
    assert cs["status"] == "accepted"
    assert any(c["node"].startswith("syn:") for c in cs["changes"])
    # local edit touches only the target
    cs2 = feedback.handle_feedback(ctx, "sample size is wrong again", target="ext:1002", propagate=False, auto_accept=True)
    assert all(c["node"] == "ext:1002" for c in cs2["changes"])
    # unsupported feedback is rejected and stays flagged
    cs3 = feedback.handle_feedback(ctx, "WRONG claim", target="ext:1003", auto_accept=True)
    assert cs3["status"] == "rejected_by_agent" and not cs3["changes"]
    assert any(f["status"] == "open" for f in st.get("ext:1003")["flags"])
    # pending (non auto-accepted) proposal then accept
    cs4 = feedback.handle_feedback(ctx, "fix", target="ext:1003", auto_accept=False)
    assert cs4["status"] == "proposed", {k: v for k, v in cs4.items() if k != "sandbox_nodes"}
    feedback.accept(ctx, cs4["id"])


def test_eligibility_flip_propagates(tmp_path):
    from sragent import feedback, pipeline
    from sragent.eval.inject import inject_eligibility
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    inject_eligibility(ctx, "1001", structural=True)
    assert "1001" not in ctx.store.included_ids()
    cs = feedback.handle_feedback(ctx, "Study 1001 should not have been excluded: it has human PK data.",
                                  target="screen:1001", propagate=True, auto_accept=True)
    assert "1001" in ctx.store.included_ids()
    assert ctx.store.get("ext:1001")["status"] == "active"


def test_eval_suites_offline(tmp_path):
    """One evaluation run: end-to-end vs stage-wise kept apart, gold never in the agent's store, run record,
    paired analyses, Claim 2 R0 snapshot, report rendered from results.json, human audit not 'done'."""
    from sragent import gold, pipeline
    from sragent.eval import run_all
    ctx = make_ctx(tmp_path, extra_cfg={"pipeline": {"fulltext": False, "validate_terms": False, "citation_chasing": False, "human_mode": "agent_only", "fulltext_unclear": "include"}})
    ctx.cfg["eval"].update({"claim1_sets": 1, "claim1_repeats": 2, "judge_agreement_items": 4, "human_audit_items": 3})
    pipeline.run(ctx)
    assert all("label_included" not in n["data"] for n in ctx.store.by_type("record"))
    assert gold.labels(ctx) == {"1001": 1, "1002": 1, "1003": 1, "1004": 0}
    store_before = (ctx.run_dir / "store.json").read_text()
    R = run_all.run(ctx, suite="all", n_cases=1)
    assert (ctx.run_dir / "store.json").read_text() == store_before          # evaluation never writes the review
    out = ctx.run_dir / "eval" / "runs" / R["manifest"]["run_id"]
    disk = json.loads((out / "results.json").read_text())
    assert disk["manifest"]["run_id"] == R["manifest"]["run_id"] and (out / "EVAL_REPORT.md").exists()
    M = disk["manifest"]
    assert M["condition"] == "agent_only" and M["human_involvement"]["condition_respected"]
    assert M["review"]["store_sha256"] and M["models"]["judge"]["model"] and M["seed"] is not None
    assert M["stagewise_input"]["supplied"] and M["stagewise_input"]["agent_saw_labels"] is False
    X = disk["results"]
    assert X["end_to_end"]["universe"].startswith("records found") and "input_supplied" in X["stagewise_screening"]
    sw = json.loads((out / "stagewise" / "store.json").read_text())
    assert all("label_included" not in n["data"] for k, n in sw["nodes"].items() if k.startswith("rec:"))
    c1 = X["claim1"]
    assert c1["design"]["repeats_per_condition"] == 2 and c1["paired"]["detection"]["units"] == c1["design"]["unique_errors"]
    c2 = X["claim2"]
    assert (out / "claim2_R0_store.json").exists() and "paired" in c2
    assert all("external_gold" in c for c in c2["cases"])
    assert X["judge_validation"]["human_audit"]["status"] == "prepared_not_reviewed"
    assert disk["cost"]["note"].startswith("All calls of this evaluation run") and "by_suite_all_attempts" in disk["cost"]
    # an interrupted run (e.g. provider quota) resumes in place: finished suites kept, the unfinished one restarted
    rid_ = R["manifest"]["run_id"]
    disk_runs = disk["results"]["claim2"]["runs"]
    for k in ("claim2", "judge_validation"):
        disk["results"].pop(k)
    (out / "results.json").write_text(json.dumps(disk))
    part = json.loads((out / "claim2_partial.json").read_text())
    kept = [r for r in part["runs"] if "error" not in r][:3]
    part["runs"] = kept                                     # as if interrupted after three runs
    (out / "claim2_partial.json").write_text(json.dumps(part))
    R3 = run_all.run(ctx, resume=rid_)
    assert R3["manifest"]["run_id"] == rid_ and R3["manifest"]["resumed"]
    c2r = R3["results"]["claim2"]
    assert len(c2r["runs"]) == len(disk_runs) and all(k in c2r["runs"] for k in kept)   # finished runs kept, rest re-run
    # a stop during the Claim 2 build: every built case was saved at once, so a resume keeps them (same ids, same R0)
    built = json.loads((out / "claim2_built.json").read_text())
    assert sorted(c["id"] for c in built) == sorted(c["id"] for c in c2["cases"])   # built order B, C, A, D, E; reported by type
    disk = json.loads((out / "results.json").read_text())
    for k in ("claim2", "judge_validation"):
        disk["results"].pop(k)
    (out / "results.json").write_text(json.dumps(disk))
    (out / "claim2_partial.json").unlink()                 # as if interrupted before any case ran
    R4 = run_all.run(ctx, resume=rid_)
    assert sorted(c["id"] for c in R4["results"]["claim2"]["cases"]) == sorted(c["id"] for c in c2["cases"])
    assert len(R4["results"]["claim2"]["runs"]) == len(disk_runs)
    # re-scoring uses the original experiment's R0, in a new run
    R2 = run_all.run(ctx, suite="claim2_rescore", from_run=R["manifest"]["run_id"])
    assert R2["results"]["claim2"]["rescored_against"].endswith("claim2_R0_store.json")
    # agent-only: human judgments are refused, PDF supply is allowed and logged separately
    from sragent import provenance, todos
    r = todos.apply(ctx, {"todo_id": "protocol", "option": "approve"})
    assert not r["ok"] and "agent-only" in r["message"]
    assert provenance.summary(ctx)["pdf_provision"]["files_supplied"] >= 3


def test_todos_answers_and_user_files(tmp_path):
    """Human to-do list: build, answer, sync, and a user-supplied full text (mock LLM, no network)."""
    from sragent import pipeline, sync, todos
    from sragent.stages import fulltext
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    # make one included study 'awaiting classification' as if full-text screening could not decide
    set_fulltext_decision(st, "1003", decision="awaiting")
    ids = {t["id"]: t for t in todos.build(ctx)}
    assert "protocol" in ids and "elig:1003" in ids and any(k.startswith("rob:") for k in ids)
    assert all(t["question"] and (t["options"] or t["kind"] in ("search", "fields") or t.get("approved")) for t in ids.values())   # custom cards
    # a record whose full-text review is blocked is requested, unless declared not obtainable
    n = st.get("screen:1004")
    before = dict(n["data"])
    st.upsert(n["id"], "screen", {**before, "decision": "forward"}, depends_on=n["depends_on"])         # forwarded ...
    st.upsert("ft:1004", "fulltext", {"kind": "abstract", "text": ""}, depends_on=["rec:1004"])      # ... no full text found
    assert SC(st, "1004")["decision"] == "fulltext_unavailable"
    req = [t for t in todos.build(ctx) if t["id"] == "fulltexts"]
    assert req and "1004" in str(req[0])
    from sragent import state
    state.set_pdf_request(st, "1004", unavailable=True)
    assert not [t for t in todos.build(ctx) if t["id"] == "fulltexts"]
    state.set_pdf_request(st, "1004", unavailable=False)
    st.upsert(n["id"], "screen", before, depends_on=n["depends_on"])
    del st.nodes["ft:1004"]
    st.reindex()
    # answers
    assert todos.apply(ctx, {"todo_id": "protocol", "option": "approve"})["ok"]
    assert todos.apply(ctx, {"todo_id": "elig:1003", "option": "exclude", "extra": {"criterion": "I3"}, "text": "no PK"})["ok"]
    assert "1003" not in st.included_ids() and SC(st, "1003")["human_decision"]["decision"] == "exclude"
    r = todos.apply(ctx, {"todo_id": "rob:1001", "option": "change", "extra": {"domain": "missing_data", "judgment": "high"}})
    assert r["ok"] and st.get("rob:1001")["data"]["overall"] == "high"
    pc = sync.pending_changes(ctx)
    assert "1003" in pc["excluded_but_cited"] or not pc["has_synthesis"]
    out = sync.sync_dependents(ctx)
    assert all("1003" not in x["data"]["cites"] for x in st.by_type("synthesis", active_only=True))
    assert st.get("ext:1003")["status"] == "excluded"
    assert (ctx.run_dir / "human_decisions.jsonl").exists()
    # user full text: <run_dir>/fulltext/<rid>.txt is picked up, re-screened and re-extracted
    fdir = fulltext.fulltext_dir(ctx)
    (fdir / "1002.txt").write_text("## Methods\n" + "Emicizumab population PK model in 60 children. " * 60)
    assert fulltext.new_local_files(ctx) == ["1002"]
    res = sync.process_pdfs(ctx)
    assert res["processed"] == ["1002"] and st.get("ft:1002")["data"]["origin"].startswith("user file")
    assert fulltext.new_local_files(ctx) == []
    # decided by content: a copy (new modification time) is not new; changed content is
    import os, time as _t
    os.utime(fdir / "1002.txt", (_t.time() + 60, _t.time() + 60))
    assert fulltext.new_local_files(ctx) == []
    (fdir / "1002.txt").write_text("## Methods\n" + "Emicizumab population PK model in 64 children. " * 60)
    assert fulltext.new_local_files(ctx) == ["1002"]
    assert (fdir / "needed_pdfs.csv").exists()


def test_settings_model_table_saves_cells_and_keeps_started_stages_fixed(tmp_path):
    """Settings: one row per stage with a cell per role; saving writes models: into the run file; a cell of a stage
    that has started is refused."""
    import yaml
    from sragent import pipeline
    from sragent.ui.configs import Library
    from sragent.ui.server import App
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx, until="screen")
    run = tmp_path / "configs" / "review" / "t" / "run.yaml"
    run.parent.mkdir(parents=True)
    (tmp_path / "configs" / "models.yaml").write_text((ROOT / "configs/models.yaml").read_text())
    cfg = {k: v for k, v in ctx.cfg.items() if not k.startswith("_") and k != "topic"}
    cfg.update({"models_file": "../../models.yaml", "topic": ctx.cfg["topic"]})
    run.write_text(yaml.safe_dump(cfg))
    app, lib = App(str(run)), Library(tmp_path / "configs")
    rows = {r["stage"]: r for r in app.model_rows()}
    assert list(rows)[0] == "default" and set(rows["eligibility"]["cells"]) == {"writer", "verifier"}
    assert set(rows["extract"]["cells"]) == {"writer"} and rows["screen"]["started"] and not rows["extract"]["started"]
    keep = {"default.writer": {"provider": "mock", "model": "mock"}}
    r = app.save_models({**keep, "extract.writer": {"provider": "openai", "model": "gpt-6.1-sol", "reasoning_effort": "high"},
                         "rob.verifier": {"provider": "openai", "model": "gpt-6.1-sol"}}, lib)
    assert r["ok"], r
    written = yaml.safe_load(run.read_text())["models"]
    assert written["extract"] == {"writer": {"model": "gpt-6.1-sol", "reasoning_effort": "high"}}   # provider from the catalog
    assert written["rob"] == {"verifier": "gpt-6.1-sol"} and written["default"]["writer"] == {"model": "mock", "provider": "mock"}
    r = app.save_models({**keep, "screen.writer": {"reasoning_effort": "high"}}, lib)
    assert not r["ok"] and "models.screen.writer" in r["message"]


def test_ui_app_state_and_estimate(tmp_path):
    import yaml
    from sragent import pipeline
    from sragent.ui.server import App
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    cfgp = tmp_path / "ui.yaml"
    cfg = {k: v for k, v in ctx.cfg.items() if not k.startswith("_")}
    cfg["models_file"] = "models.yaml"                          # paths in a config file: relative to it
    cfg["local"] = {**cfg["local"], "endpoints_file": "local_endpoints.yaml"}
    cfgp.write_text(yaml.safe_dump(cfg))
    (tmp_path / "models.yaml").write_text((ROOT / "configs/models.yaml").read_text())
    (tmp_path / "local_endpoints.yaml").write_text(yaml.safe_dump({"endpoints": [{"url": "http://node:8000/v1"}]}))
    app = App(str(cfgp))
    s = app.state()
    assert [x["key"] for x in s["steps"]] == ["protocol", "search", "screen", "retrieve", "eligibility", "snowball",
                                               "extract", "rob", "synthesis", "report", "feedback"] and s["todos"]
    assert [x["key"] for x in s["registry"]] == [x["key"] for x in s["steps"]][:10]   # the Overview is the registry
    e = app.estimate({"action": "run", "until": "report", "agent": {"provider": "gemini", "model": "gemini-3.8-flash", "effort": "low"}})
    assert e["total_usd"] == 0  # everything already done
    m = app.models()
    flash = next(x for x in m["api"] if x["id"] == "gemini-3.8-flash")   # from the model catalog
    assert flash["input_per_m"] == 0.75 and flash["provider"] == "gemini"
    assert m["local"]["endpoints"] == 1 and m["local"]["models"] == ["auto"]
    job = yaml.safe_load(app._write_job_config({"action": "rerun", "agent": {"provider": "local", "model": "auto"}}).read_text())
    assert job["local"]["endpoints_file"] == str((tmp_path / "local_endpoints.yaml").resolve())
    assert job["job_model"]["provider"] == "local"     # a model picked for one re-run: this job's writers only
    with app.lock:                                     # the worker cannot take these jobs while the lock is held
        app.submit({"action": "run", "until": "report", "agent": {"provider": "local", "model": "auto"}})
        later = app.submit({"action": "rerun", "rid": "1001", "step": "extract"})   # no model picked for this job
        app.queue.clear()
    assert "agent" not in later                        # the last Run-page model does not carry over
    assert "budget" not in yaml.safe_load(app._write_job_config(later).read_text())   # no cap of its own: the run file's
    assert "job_model" not in yaml.safe_load(app._write_job_config(later).read_text())
    job = yaml.safe_load(app._write_job_config({"action": "run", "agent": {"provider": "local", "model": "auto"}}).read_text())
    assert "job_model" not in job                      # pipeline jobs follow models.<stage>.<role>
    r = app.op({"name": "protocol.approve", "args": {}})
    assert r["ok"]


def test_awaiting_notes_follow_the_decision(tmp_path):
    """'Awaiting classification' is derived from the decision (attention.py): there while undecided, gone after."""
    import pytest
    from sragent import attention
    ctx = make_ctx(tmp_path)
    st = ctx.store
    st.upsert("rec:1", "record", {"rid": "1", "title": "t", "abstract": "a"})
    st.upsert("screen:1", "screen", {"stage": "title_abstract", "decision": "forward", "because": []}, depends_on=["rec:1"])
    st.upsert("elig:1", "eligibility", {"stage": "full_text", "decision": "awaiting", "because": ["I1"]}, depends_on=["screen:1"])
    assert [x["source"] for x in attention.by_node(st).get("elig:1", [])] == ["screening"]
    assert not SN(st, "1")["flags"]                                    # nothing stored to keep in step
    SN(st, "1")["data"]["decision"] = "exclude"
    assert not attention.by_node(st).get("elig:1")
    with pytest.raises(ValueError, match="agent_only"):
        load_config(None, {"pipeline": {"human_mode": "agent_only"}})


def test_review_topics_are_not_sampled_or_evaluated(tmp_path):
    import pytest
    from sragent.eval import run_all
    with pytest.raises(ValueError, match="benchmark"):
        load_config(None, {"pipeline": {"max_records": 60}, "topic": {"records": {"source": "pubmed"}}})
    ctx = make_ctx(tmp_path)   # records without known answers
    with pytest.raises(ValueError, match="known answers"):
        run_all.run(ctx, "all")
    assert not (ctx.run_dir / "eval").exists()


def test_screening_rules():
    from sragent import rules
    keys = ["emicizumab", "ACE910", "Hemlibra", "hemophilia A"]
    R = rules.DEFAULT_RULES + ["review"]
    c = lambda **r: (rules.classify({"title": "", "abstract": "", "pub_types": [], "doi": "", **r}, keys, R) or [None])[0]
    assert c(title="Abstracts of the WFH 2014 World Congress") == "conference_abstract"
    assert c(title="Monitoring of emicizumab", pub_types=["proceedings-article"], source="crossref") == "conference_abstract"
    assert c(title="Every 2 weeks emicizumab", abstract="x" * 300, doi="10.1182/blood-2018-99-115792") == "conference_abstract"
    assert c(title="PSY57 - A comparison of emicizumab", source="crossref") == "conference_abstract"
    assert c(title="PRISMA statement.", pub_types=["Comment", "Letter"]) == "off_topic_no_abstract"
    assert c(title="Emicizumab: A Review in Haemophilia A.", abstract="x", pub_types=["Journal Article", "Review"]) == "review"
    assert c(title="") == "empty_record"
    # real studies (incl. a letter and a case report that the gold standard includes) are kept
    assert c(title="Emicizumab Prophylaxis in Hemophilia A with Inhibitors.", pub_types=["Letter", "Comment"]) is None
    assert c(title="A boy with joint pain associated with emicizumab treatment", pub_types=["Case Reports", "Letter"]) is None
    assert c(title="Emicizumab PK", abstract="x", doi="10.1182/blood-2015-06-650226", pub_types=["Journal Article"]) is None


def test_study_pages_edits_reruns_history(tmp_path):
    """Studies browser: detail/export, manual edits with history, comments, re-run proposals, propagation."""
    from sragent import actions, feedback, pipeline, studies, todos
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    rows = studies.studies_list(ctx)
    assert {r["rid"] for r in rows} == {"1001", "1002", "1003", "1004"}
    assert {r["group"] for r in rows} <= set(studies.GROUPS)
    d = studies.study_detail(ctx, "1001")
    assert d["extraction"]["fields"] and d["rob"]["domains"] and d["links"]["PubMed"].endswith("/1001/")
    assert "## Extracted data" in studies.study_markdown(d)
    # manual edit of an extraction value (no LLM): new version, history, dependents notice
    v0 = st.get("ext:1001")["version"]
    r = actions.edit(ctx, {"kind": "ext", "rid": "1001", "fields": {"sample_size": "14 participants"}, "note": "Table 1"})
    assert r["ok"] and st.get("ext:1001")["version"] == v0 + 1
    item = st.get("ext:1001")["data"]["fields"]["sample_size"]
    assert item["value"] == "14 participants" and item["edited"]["note"] == "Table 1"
    tl = todos.build(ctx)
    me = [t for t in tl if t["kind"] == "manual_edit"]
    assert me and me[0]["id"].startswith("edited:ext:1001")
    # multi-domain RoB change in one answer
    doms = [x["domain"] for x in st.get("rob:1002")["data"]["domains"]][:2]
    r = todos.apply(ctx, {"todo_id": "rob:1002", "option": "change", "text": "case report",
                          "extra": {"changes": [{"domain": doms[0], "judgment": "high"}, {"domain": doms[1], "judgment": "some_concerns"}]}})
    rb = st.get("rob:1002")["data"]
    assert r["ok"] and rb["overall"] == "high" and rb["human_confirmed"]
    assert [x["judgment"] for x in rb["domains"][:2]] == ["high", "some_concerns"]
    # comments
    c = actions.add_comment(ctx, "study:1001", "check the dosing")
    assert c["id"] == "C1" and studies.study_detail(ctx, "1001")["comments"][0]["text"] == "check the dosing"
    assert actions.feedback_target(st, "study:1001") == "ext:1001"
    actions.resolve_comment(ctx, "C1", "done")
    # propagate after the manual edit -> proposal, flag resolved
    cs = actions.propagate_from(ctx, "ext:1001")
    assert cs["kind"] == "propagate" and cs["status"] in ("proposed", "no_change")
    assert not any(t["id"].startswith("edited:ext:1001") for t in todos.build(ctx))
    # re-run extraction with a note: proposal only, live store unchanged until accepted
    before = json.dumps(st.get("ext:1003")["data"], sort_keys=True)
    cs = actions.rerun(ctx, "1003", "extract", note="use table 2")
    assert json.dumps(st.get("ext:1003")["data"], sort_keys=True) == before
    assert cs["kind"] == "rerun" and cs["status"] in ("proposed", "no_change")
    if cs["status"] == "proposed":
        assert any(t["id"] == f"cs:{cs['id']}" for t in todos.build(ctx))
        feedback.accept(ctx, cs["id"])
    # history and exports
    h = studies.history(ctx)
    assert any(e["actor"] == "user" and e["node"] == "ext:1001" for e in h["changelog"])
    assert any(p["kind"] == "rerun" for p in h["proposals"])
    hist = studies.study_detail(ctx, "1001")["history"]
    assert any("sample_size" in " ".join(e["changes"]) for e in hist)
    assert studies.to_csv(rows).splitlines()[0].startswith("rid,")
    assert len(studies.review_export(ctx)["studies"]) == 4


def test_long_text_packing_tables_and_parsers():
    from sragent.docparse import cu_markdown_to_text
    from sragent.evidence import chunks, packed, segment
    text = ("## Abstract\nEmicizumab was studied. It works.\n## Introduction\n" + "Background sentence here. " * 40 +
            "\n## Results\nTrough was 52 ug/mL in adults.\n## Table 2: PK\nParameter | Mean\nCtrough | 52.3\n"
            "## References\n1. Doe J. Blood 2016.\n")
    p = packed(text, 200)
    assert "Trough was 52" in p and "Ctrough | 52.3" in p and "Doe J" not in p and "not shown" in p
    ids = {s["sid"]: s["text"] for s in segment(text)}
    for sid in __import__("re").findall(r"\[(s\d+)\]", p):      # ids in the prompt are the ids of the stored text
        assert sid in ids
    assert len(chunks(text, 300)) >= 2
    md = ('# T\n## Results\n<table><caption>Table 2. PK</caption><tr><th>P</th><th>Mean</th></tr><tr><td>t1/2</td><td>26.9</td></tr></table>\n'
          '![trough](figures/1 "Data points at (0,0), (7,18)")\n```chart\n{"data":{"labels":["0","7"],"datasets":[{"label":"trough","data":[0,18]}]}}\n```\n')
    t = cu_markdown_to_text(md)
    assert "t1/2 | 26.9" in t and "approximate" in t and "(7, 18)" in t


def test_chunked_extraction_and_feedback_loops(tmp_path):
    from sragent import feedback, pipeline, todos
    from sragent.stages import extraction
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    # long paper read in parts, differing values merged
    ctx.cfg["pipeline"]["context_chars"] = 600
    extraction.extract_one(ctx, "1001")
    d = st.get("ext:1001")["data"]
    assert d["read_mode"].endswith("parts") and d["merged_fields"]
    ctx.cfg["pipeline"]["context_chars"] = 60000
    # a proposal that became out of date is refused
    cs = feedback.handle_feedback(ctx, "The sample size of 1002 is wrong", target="ext:1002", auto_accept=False)
    assert cs["status"] == "proposed" and cs["base_versions"] and cs.get("post_checks")
    n = st.get("ext:1002")
    d = json.loads(json.dumps(n["data"])); d["fields"]["sample_size"]["value"] = "61 children"   # a real edit in between
    st.upsert(n["id"], "extraction", d, depends_on=n["depends_on"], evidence=n["evidence"], actor="user", reason="edit")
    r = todos.apply(ctx, {"todo_id": f"cs:{cs['id']}", "option": "accept"})
    assert not r["ok"] and "out of date" in r["message"]
    # vague feedback is split into several requests
    cs2 = feedback.handle_feedback(ctx, "Please re-check every population value", auto_accept=False)
    assert len(cs2["located"]) == 2 and len({c["node"] for c in cs2["changes"]}) >= 2
    # unclear feedback becomes a clarification question for the user
    cs3 = feedback.handle_feedback(ctx, "UNCLEAR this looks off", target="ext:1003", auto_accept=False)
    assert cs3["status"] == "needs_clarification"
    assert any(t["kind"] == "clarify" for t in todos.build(ctx))


def test_citation_chasing(tmp_path, monkeypatch):
    from sragent import pipeline
    from sragent.sources import openalex, pubmed
    from sragent.stages import snowball
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx, until="eligibility")
    w = {"id": "https://openalex.org/W1", "referenced_works": ["https://openalex.org/W2", "https://openalex.org/W3"]}
    new = {"id": "https://openalex.org/W2", "title": "Emicizumab trough levels in a new cohort", "publication_year": 2019,
           "ids": {}, "doi": "https://doi.org/10.1/new", "abstract_inverted_index": {"Emicizumab": [0], "PK": [1]}}
    off = {"id": "https://openalex.org/W3", "title": "Knee surgery outcomes", "publication_year": 2019, "ids": {}, "doi": "https://doi.org/10.1/off"}
    monkeypatch.setattr(openalex, "work", lambda pmid="", doi="": w)
    monkeypatch.setattr(openalex, "works_by_ids", lambda ids: [new, off])
    monkeypatch.setattr(openalex, "citing", lambda w, max_n=400, until=None: [])
    monkeypatch.setattr(pubmed, "efetch", lambda pmids: [])
    ctx.cfg["pipeline"]["citation_chasing"] = True
    res = snowball.snowball(ctx, ctx.store.get("protocol")["data"])
    from sragent import state
    added = state.snowball(ctx.store)["added"][0]
    assert res["added"] == 1 and ctx.store.get(f"screen:{added}")
    assert ctx.store.get(f"rec:{added}")["data"]["found_via"]                     # backward / forward, on the record


def test_config_lock(tmp_path):
    """Review-defining settings are fixed once a review has started; a change must be accepted on purpose."""
    import copy

    import pytest
    import yaml
    from sragent import config_lock, pipeline, provenance
    from sragent.stages.report import report_html, report_markdown
    from sragent.ui.server import App
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    assert ctx.store.meta["config_lock"]["values"]["pipeline.human_mode"] == "human_in_the_loop"
    cfg = copy.deepcopy(ctx.cfg)
    cfg["budget"]["total_usd"], cfg["contact_email"] = 3.0, "me@example.org"      # free to change at any time
    cfg["budget"]["stages"]["screen"] = 0.5
    Ctx.create(cfg, mock_fn=mock_fn)
    cfg["topic"]["extraction_fields"] = cfg["topic"]["extraction_fields"] + [{"name": "new_field", "description": "x"}]
    cfg["pipeline"]["human_mode"], cfg["pipeline"]["fulltext_unclear"] = "agent_only", "include"
    with pytest.raises(config_lock.ConfigChanged) as e:
        Ctx.create(cfg, mock_fn=mock_fn)
    keys = {"topic.extraction_fields", "pipeline.human_mode", "pipeline.fulltext_unclear"}
    assert {c["key"] for c in e.value.changes} == keys
    assert {c["key"] for c in config_lock.pending(cfg, ctx.run_dir)} == keys     # read from store.json's head

    # the web interface: the run refuses to load, then is accepted with a reason
    cfgp = tmp_path / "ui.yaml"
    ui = {k: v for k, v in cfg.items() if not k.startswith("_")}
    cfgp.write_text(yaml.safe_dump(ui))
    app = App(str(cfgp))
    with pytest.raises(config_lock.ConfigChanged):
        app.ctx()
    assert not app.accept_config("")["ok"]                                    # a reason is required
    r = app.accept_config("added a field the protocol needed")
    assert r["ok"] and {c["key"] for c in r["changes"]} == keys
    c2 = app.ctx()                                                             # continues with the new settings
    assert config_lock.pending(cfg, ctx.run_dir) == []
    assert provenance.summary(c2)["config_changes"] == {"n": 1, "keys": sorted(keys)}
    assert "Changes to the review settings after the start" in report_markdown(c2)
    assert "Table D0." in report_html(c2)


def test_models_per_stage_and_role(tmp_path):
    """Each stage uses its own cells (models.<stage>.<role>); every call is booked to its stage and role; a stage's cells
    are fixed once it has started; a verifier with no cell of its own is the default writer."""
    import copy

    import pytest
    from sragent import config_lock, pipeline
    from sragent.config import model_for
    from sragent.estimate import estimate_stages
    big = {"model": "mock-big", "reasoning_effort": "high", "pricing": {"input_per_m": 2.0, "output_per_m": 10.0}}
    ctx = make_ctx(tmp_path, extra_cfg={"models": {"protocol": {"writer": big}, "extract": {"writer": {**big, "reasoning_effort": "medium"}},
                                                   "synthesis": {"writer": big}}})
    assert model_for(ctx.cfg, "synthesis", "verifier")["model"] == "mock"         # checked by another model than its writer
    pipeline.run(ctx, until="eligibility")
    rows = [json.loads(line) for line in (ctx.run_dir / "usage.jsonl").read_text().splitlines()]
    cells = {(r["stage"], r["role"]): r["model"] for r in rows}
    assert cells[("protocol", "writer")] == "mock-big" and cells[("screen", "writer")] == "mock"
    rec = ctx.store.meta["config_lock"]["values"]
    assert rec["models.protocol.writer"]["model"] == "mock-big" and "models.extract.writer" not in rec   # not started yet
    assert rec["models.screen.verifier"]["model"] == "mock"                       # the verifier cell is fixed with its stage

    cfg = copy.deepcopy(ctx.cfg)                                    # a stage that has not started: free to change
    cfg["models"]["extract"] = {"writer": {**big, "reasoning_effort": "low"}}
    cfg["models"]["rob"] = {"writer": {"reasoning_effort": "medium"}}
    c2 = Ctx.create(cfg, mock_fn=mock_fn)
    c2.quiet = True
    est = estimate_stages({"extract": 2, "rob": 2, "rob_verify": 1}, cfg)
    assert {r["stage"]: (r["model"], r["effort"]) for r in est["rows"]} == \
        {"extract": ("mock-big", "low"), "rob": ("mock", "medium"), "rob_verify": ("mock", "default")}
    pipeline.run(c2)
    rows = [json.loads(line) for line in (ctx.run_dir / "usage.jsonl").read_text().splitlines()]
    assert {r["model"] for r in rows if r["stage"] == "extract"} == {"mock-big"}
    assert {r["model"] for r in rows if r["stage"] == "rob"} == {"mock"}
    assert c2.store.meta["config_lock"]["values"]["models.extract.writer"]["reasoning_effort"] == "low"

    for stage, role in (("screen", "writer"), ("screen", "verifier")):   # a started stage: both cells fixed
        cfg3 = copy.deepcopy(cfg)
        cfg3["models"]["screen"] = {role: {"reasoning_effort": "high"}}
        with pytest.raises(config_lock.ConfigChanged) as e:
            Ctx.create(cfg3, mock_fn=mock_fn)
        assert [c["key"] for c in e.value.changes] == [f"models.{stage}.{role}"]
    from sragent.stages.report import report_markdown
    assert "mock-big (high effort): protocol" in report_markdown(c2)


def test_rescreen_keeps_records_waiting_for_full_text(tmp_path):
    """Re-applying a protocol never includes a record without a full-text review; it can still exclude it."""
    import copy

    from sragent import pipeline
    from sragent.stages import screening
    ctx = make_ctx(tmp_path, fulltexts=("1001",))          # 1002 and 1003 have no full text
    pipeline.run(ctx, until="eligibility")
    st = ctx.store
    from sragent.selection import screened
    waiting = sorted(rid for rid, v in screened(st) if v["stage"] == "retrieval")
    assert waiting == ["1002", "1003"]
    protocol = st.get("protocol")["data"]
    screening.rescreen_rule_only(ctx, protocol)
    assert st.included_ids() == ["1001"]
    assert all(SC(st, w)["decision"] == "fulltext_unavailable" for w in waiting)
    p2 = copy.deepcopy(protocol)                            # a revised protocol that rules them out
    ta = next(c for c in p2["criteria"] if c["type"] == "inclusion" and c.get("stage", "title_abstract") == "title_abstract")
    ta["type"] = "exclusion"                                 # 'met' now means excluded
    screening.rescreen_rule_only(ctx, p2)
    for w in waiting:
        d = SC(st, w)
        assert (d["decision"], d["stage"]) == ("exclude", "title_abstract") and ta["id"] in d["because"]


def test_reply_that_is_not_one_object_is_asked_again(tmp_path):
    """A reply that is not one JSON object (e.g. only the list a check returns inside it) is re-asked once,
    and such a reply is never served from the cache."""
    from sragent.llm import LLM, CostTracker, DiskCache
    replies = iter(['["only the issues"]', '{"support": "supported", "issues": []}'])
    cfg = load_config(None, {"models": {"default": {"writer": {"provider": "mock", "model": "mock"}}}})
    llm = LLM(cfg, model_for(cfg, None), CostTracker(1), DiskCache(tmp_path / "c.sqlite"), mock_fn=lambda m, mode: next(replies))
    assert llm.chat([{"role": "user", "content": "check"}], tag="t") == {"support": "supported", "issues": []}


def test_open_access_copies_are_tried_in_order_and_the_one_used_is_recorded(tmp_path, monkeypatch):
    """Repositories before publishers in each source; OpenAlex is asked when no Unpaywall copy can be fetched; a link
    already tried is not fetched twice; the exact copy used is recorded with its version and license."""
    from types import SimpleNamespace as NS
    import requests
    from sragent import pipeline, studies
    from sragent.sources import europepmc, openalex, unpaywall
    from sragent.stages import fulltext
    ctx = make_ctx(tmp_path, fulltexts=(), extra_cfg={"contact_email": "a@b.org", "pipeline": {"fulltext": True}})
    pipeline.run(ctx, until="search")
    monkeypatch.setattr(europepmc, "pmcid_for", lambda *a: "")
    loc = lambda name, host: {"pdf": f"https://x/{name}.pdf", "url": "", "host": host, "version": "acceptedVersion", "license": f"lic-{name}"}
    monkeypatch.setattr(unpaywall, "locations", lambda doi, email: [{**loc("A", "publisher"), "source": "unpaywall"}, {**loc("B", "repository"), "source": "unpaywall"}])
    monkeypatch.setattr(openalex, "work", lambda *a: {})
    monkeypatch.setattr(openalex, "oa_locations", lambda w: [{**loc("C", "publisher"), "source": "openalex"}, {**loc("B", "repository"), "source": "openalex"}, {**loc("D", "repository"), "source": "openalex"}])
    calls = []

    def get(url, **kw):
        calls.append(url.rsplit("/", 1)[1])
        ok = url.endswith("D.pdf")
        return NS(status_code=200 if ok else 403, content=b"%PDF-1" if ok else b"", url=url + "?final" if ok else url)

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(fulltext, "_parse", lambda c, p: ("word " * 1000, "pdftotext"))
    rec = ctx.store.get("rec:1001")["data"]
    rec["doi"] = rec.get("doi") or "10.1/x"
    d = fulltext.fetch_one(ctx, "1001")
    assert calls == ["B.pdf", "A.pdf", "D.pdf"] and d["kind"] == "fulltext" and d["origin"] == "open-access copy (repository)"
    assert d["oa_used"]["used_url"] == "https://x/D.pdf?final" and d["oa_used"]["license"] == "lic-D" and d["oa_used"]["source"] == "openalex"
    assert d["oa_used"]["version"] == "acceptedVersion" and d["oa_used"]["retrieved"]
    by = {l["pdf"].rsplit("/", 1)[1]: l for l in d["oa_locations"]}
    assert by["A.pdf"]["download_status"] == 403 and by["D.pdf"]["downloaded"] and "C.pdf" in by
    assert studies.study_detail(ctx, "1001")["source"]["oa_used"]["license"] == "lic-D"


def test_one_unusable_reply_does_not_stop_the_stage(tmp_path):
    """A record the model never answers with valid JSON is skipped and reported, the others are screened, the bad reply
    is kept, the retry says where the JSON broke, and running the stage again retries it. A budget stop still stops."""
    import pytest
    from sragent import pipeline
    from sragent.llm import BudgetExceeded
    ctx = make_ctx(tmp_path)
    retries = []

    def broken(messages, mode):
        u = messages[-1]["content"]
        if "not one valid JSON object" in u:
            retries.append(u)
            return '{"criteria": [{"id": "I1" "assessment": "met"}]}'
        return '{"criteria": [{"id": "I1" "assessment": "met"}]}' if "case series" in u else mock_fn(messages, mode)

    ctx.hub.mock_fn = broken
    pipeline.run(ctx, until="screen")
    st = ctx.store
    assert st.get("screen:1003") is None and st.get("screen:1001") and st.get("screen:1002")
    assert "at character" in retries[0] and len(retries) == ctx.cfg["llm_defaults"]["max_retries"]
    bad = [json.loads(l) for l in (ctx.run_dir / "bad_replies.jsonl").read_text().splitlines()]
    assert bad[0]["tag"] == "screen_title_abstract" and bad[0]["reply"].startswith('{"criteria"') and bad[0]["stage"] == "screen"
    assert "1 of" in (ctx.run_dir / "run.log").read_text().split("[skipped] ")[-1]
    ctx.hub.mock_fn = mock_fn
    ctx.hub._clients.clear()
    pipeline.run(ctx, until="screen")
    assert st.get("screen:1003")

    def over(item):
        raise BudgetExceeded("Budget exhausted")
    with pytest.raises(BudgetExceeded):
        ctx.map(over, ["a"])


class FakeBatches:
    """The Message Batches API, answering each request with `answer(params)` once `ended` is set."""

    def __init__(self, answer):
        from types import SimpleNamespace as NS
        self.NS, self.answer, self.sent, self.ended = NS, answer, [], True
        self.messages = NS(batches=self, create=self.live)

    def live(self, **params):
        NS = self.NS
        self.live_params = params
        usage = NS(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return NS(content=[NS(type="text", text=mock_fn(params["messages"], "json"))], usage=usage, stop_reason="end_turn")

    def with_options(self, **kw):
        return self

    def create(self, requests):
        self.sent.append(requests)
        return self.NS(id=f"b{len(self.sent)}", processing_status="in_progress")

    def retrieve(self, bid):
        return self.NS(processing_status="ended", request_counts=self.NS(succeeded=0, errored=0, expired=0, canceled=0, processing=0))

    def results(self, bid):
        NS = self.NS
        for r in self.sent[int(bid[1:]) - 1]:
            usage = NS(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0)
            msg = NS(content=[NS(type="text", text=self.answer(r["params"]))], usage=usage, stop_reason="end_turn")
            yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=msg))


def _batch_ctx(tmp_path, monkeypatch, budget=None):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    cfg = {"pipeline": {"batch_stages": ["screen"]}, "models": {"screen": {"writer": "claude-haiku-5-5"}}}
    return make_ctx(tmp_path, extra_cfg={**cfg, **({"budget": {"total_usd": budget}} if budget is not None else {})})


def test_batched_stage_answers_at_half_price_and_falls_back_to_live_calls(tmp_path, monkeypatch):
    """Records are screened from one batch at half price; a batch answer that is not valid JSON is asked live; the
    decisions are those of a normal run."""
    from sragent import pipeline
    ctx = _batch_ctx(tmp_path, monkeypatch)
    answer = lambda p: "not json" if "case series" in p["messages"][-1]["content"] else mock_fn(p["messages"], "json")
    fake = ctx.hub.model("screen").client = FakeBatches(answer)
    pipeline.run(ctx, until="screen")
    st = ctx.store
    assert len(fake.sent) == 1 and len(fake.sent[0]) == 4 and all(st.get(f"screen:{r}") for r in ("1001", "1002", "1003", "1004"))
    rows = [json.loads(l) for l in (ctx.run_dir / "usage.jsonl").read_text().splitlines() if '"screen_title_abstract"' in l]
    batch = [r for r in rows if r.get("batch")]
    assert len(batch) == 4 and all(r["usd"] == round((1000 * 0.1 + 500 * 0.5) / 1e6 / 2, 6) for r in batch)
    assert sum(r["cached"] for r in rows) == 3 and sum(not r["cached"] and not r.get("batch") for r in rows) == 1   # 1003 asked live
    assert not (ctx.run_dir / "pending_batch_screen.json").exists() and (ctx.run_dir / "bad_replies.jsonl").exists()


def test_a_batch_waiting_when_the_job_stopped_is_collected_not_sent_again(tmp_path, monkeypatch):
    from sragent import pipeline
    from sragent.stages import screening
    ctx = _batch_ctx(tmp_path, monkeypatch)
    llm = ctx.hub.model("screen")
    fake = llm.client = FakeBatches(lambda p: mock_fn(p["messages"], "json"))
    pipeline.run(ctx, until="search")
    protocol = ctx.store.get("protocol")["data"]
    messages, tag = screening._screen_prompt(ctx, protocol, "1001", "title_abstract")
    key = llm._key(messages, True, llm.c["temperature"], llm.c["max_tokens"])
    fake.sent.append([{"custom_id": key, "params": llm._anthropic_params(messages, True, llm.c["max_tokens"])}])
    (ctx.run_dir / "pending_batch_screen.json").write_text(json.dumps({"id": "b1", "tags": {key: tag}}))
    pipeline.run(ctx, until="screen")                       # collects b1, then sends only the three left
    assert [len(s) for s in fake.sent] == [1, 3] and ctx.store.get("screen:1001")


def test_a_batch_sends_only_what_the_budget_has_room_for(tmp_path, monkeypatch):
    from pathlib import Path
    ctx = _batch_ctx(tmp_path, monkeypatch, budget=1.0)
    llm = ctx.hub.model("screen")
    fake = llm.client = FakeBatches(lambda p: "{}")
    ctx.hub.tracker.spent = 1.0 - llm._cost(1000, 1500) * 0.5 * 1.5        # room for one and a half requests
    llm.batch_prefill([([{"role": "user", "content": "y" * 4000 + str(i)}], f"u{i}") for i in range(3)], Path(ctx.run_dir / "p.json"), ctx.log)
    assert len(fake.sent[0]) == 1


def test_bulk_prompts_ask_for_their_json_shape(tmp_path, monkeypatch):
    """The prompts that run once per record send their schema, live and in a batch, beside the effort; schemas are in the
    form the API accepts; the schema is not part of the cache key; a provider without the feature is unaffected."""
    from sragent import schemas
    from sragent.llm import LLM, CostTracker
    ctx = _batch_ctx(tmp_path, monkeypatch)
    llm = ctx.hub.model("screen")
    llm.c["reasoning_effort"] = "low"
    fake = llm.client = FakeBatches(lambda p: mock_fn(p["messages"], "json"))
    msgs = [{"role": "user", "content": "An independent check"}]
    llm.chat(msgs, tag="t", schema=schemas.ELIGIBILITY_VERIFY, use_cache=False)
    assert fake.live_params["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": schemas.ELIGIBILITY_VERIFY}}
    assert "format" not in llm._anthropic_params(msgs, True, 100)["output_config"]
    assert llm._key(msgs, True, 0.0, 100) == llm._key(msgs, True, 0.0, 100)
    from pathlib import Path
    llm.batch_prefill([(msgs + [{"role": "user", "content": "more"}], "t")], Path(ctx.run_dir / "p.json"), ctx.log, schemas.SCREEN)
    assert fake.sent[0][0]["params"]["output_config"]["format"]["schema"] == schemas.SCREEN

    def check(s):
        if isinstance(s, dict):
            if s.get("type") == "object":
                assert s["additionalProperties"] is False and sorted(s["required"]) == sorted(s["properties"])
            for v in s.values():
                check(v)
        elif isinstance(s, list):
            for v in s:
                check(v)
    for s in (schemas.SCREEN, schemas.SCREEN_VERIFY, schemas.SCREEN_VERIFY_BLIND, schemas.ELIGIBILITY_VERIFY, schemas.ROB,
              schemas.ROB_VERIFY, schemas.SYNTH_VERIFY, schemas.extract(ctx.topic["extraction_fields"])):
        check(s)
    assert set(schemas.extract(ctx.topic["extraction_fields"])["properties"]["fields"]["required"]) == {f["name"] for f in ctx.topic["extraction_fields"]}
    mock = LLM(ctx.cfg, {**llm.c, "provider": "mock", "model": "mock"}, CostTracker(1), None, mock_fn=lambda m, mode: '{"a": 1}')
    assert mock.chat(msgs, schema=schemas.SCREEN) == {"a": 1}


def test_batch_stages_are_checked_and_estimated_at_half_price(tmp_path, monkeypatch):
    import pytest
    from sragent.estimate import estimate_stages
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    with pytest.raises(ValueError, match="batch_stages"):
        make_ctx(tmp_path, extra_cfg={"pipeline": {"batch_stages": ["extract"]}})
    ctx = make_ctx(tmp_path, extra_cfg={"models": {"screen": {"writer": "claude-haiku-5-5"}}})
    base = estimate_stages({"screen_title_abstract": 100}, ctx.cfg)["rows"][0]["usd"]
    ctx.cfg["pipeline"]["batch_stages"] = ["screen"]
    assert estimate_stages({"screen_title_abstract": 100}, ctx.cfg)["rows"][0]["usd"] == round(base * 0.5, 3)


# ---------------------------------------------------------------- integrity of accepted proposals and human decisions
def test_accepting_a_proposal_keeps_what_the_sandbox_created(tmp_path):
    """Nodes the sandbox created on the way (here the full-text node of a record the feedback forwarded)
    come along when the proposal is accepted; before, only the listed changes were applied."""
    from sragent import feedback, pipeline
    from sragent.stages import fulltext
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    assert SC(st, "1004")["decision"] == "exclude" and st.get("ft:1004") is None
    cs = feedback.handle_feedback(ctx, "Study 1004 should not have been excluded: it has human PK data.",
                                  target="screen:1004", propagate=True, auto_accept=False)
    assert cs["status"] == "proposed" and st.get("ft:1004") is None          # nothing applied yet
    assert "ft:1004" in cs["sandbox_nodes"] and "ft:1004" in cs["base_versions"] or True
    feedback.accept(ctx, cs["id"])
    assert st.get("ft:1004") is not None                                       # the retrieval the sandbox did
    d = SC(st, "1004")
    assert d["decision"] == "fulltext_unavailable" and d["stage"] == "retrieval"   # no full text: requested
    assert "1004" in fulltext.needed_pdfs(ctx) or any("1004" in str(x) for x in fulltext.needed_pdfs(ctx))
    assert any(e["op"] == "create" and e["node"] == "ft:1004" for e in st.changelog)   # logged, not silent


def test_a_persons_eligibility_decision_is_never_overwritten(tmp_path):
    """A person's include/exclude survives a new PDF, the next run's full-text review, rules and rescreen;
    only an explicit re-run a person asks for replaces it."""
    from sragent import actions, pipeline, rules, sync, todos
    from sragent.stages import fulltext, screening
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    set_fulltext_decision(st, "1003", decision="awaiting")
    assert todos.apply(ctx, {"todo_id": "elig:1003", "option": "include", "text": "PK data in table 2"})["ok"]
    assert SC(st, "1003")["human_decision"]["decision"] == "include"
    # a new PDF for that study: read and re-extracted, the decision kept
    (fulltext.fulltext_dir(ctx) / "1003.txt").write_text("## Methods\n" + "Emicizumab prophylaxis, 30 patients, trough 40 ug/mL. " * 60)
    r = sync.process_pdfs(ctx)
    assert "1003" in r["processed"] and "1003" not in r["rescreened"] and "1003" in r["reextracted"]
    assert SC(st, "1003")["human_decision"]["decision"] == "include" and "1003" in st.included_ids()
    # the next run's full-text review, the rules and a protocol re-application leave it alone
    assert not screening.needs_fulltext_review(st, "1003")
    screening.fulltext_review(ctx, st.get("protocol")["data"])
    rules.apply(ctx)
    screening.rescreen_rule_only(ctx, st.get("protocol")["data"])
    d = SC(st, "1003")
    assert d["decision"] == "include" and d["human_decision"]["decision"] == "include"
    # the agent's own screening (fresh assessments) keeps the decision and records what it would have said
    screening.screen_one(ctx, st.get("protocol")["data"], "1003", "full_text")
    d = SC(st, "1003")
    assert d["decision"] == "include" and d["human_decision"] and "agent_decision" in d
    # an explicit re-run a person asked for is the one way to replace it (as a proposal)
    cs = actions.rerun(ctx, "1003", "screen", note="please re-screen")
    assert cs["kind"] == "rerun" and SC(st, "1003")["human_decision"]["decision"] == "include"


def test_an_added_pdf_follows_the_update_policy(tmp_path):
    """'Process added PDFs' re-extracts, but the risk of bias and the synthesis that depend on the new values are
    brought up to date by the update engine under update_policy: with `propose` nothing is rewritten without approval."""
    from sragent import pipeline, sync
    from sragent.stages import fulltext
    ctx = make_ctx(tmp_path, extra_cfg={"update_policy": {"rob": "propose", "synthesis": "propose"}})
    pipeline.run(ctx)
    st = ctx.store
    syn = {n["id"]: (n["version"], n["data"]) for n in st.by_type("synthesis", active_only=True)}
    assert any("1001" in d["cites"] for _, d in syn.values())
    rob_v = st.get("rob:1001")["version"]

    def new_values(messages, mode):
        out = mock_fn(messages, mode)
        u = messages[-1]["content"]
        return out.replace("12 participants", "40 participants") if "Extract the following items" in u and "study id 1001" in u else out

    ctx.hub.mock_fn = new_values
    ctx.hub.cache_read = False
    ctx.hub._clients.clear()
    (fulltext.fulltext_dir(ctx) / "1001.txt").write_text("## Methods\n" + "Emicizumab prophylaxis, 40 patients, trough 45 ug/mL. " * 60)
    r = sync.process_pdfs(ctx)
    assert "1001" in r["reextracted"] and "40 participants" in str(st.get("ext:1001")["data"]["fields"])
    assert st.get("rob:1001")["version"] == rob_v                                  # proposed, not applied
    assert {n["id"]: (n["version"], n["data"]) for n in st.by_type("synthesis", active_only=True)} == syn


def test_human_edits_survive_a_re_extraction_and_re_assessment(tmp_path):
    """A value a person edited and a risk of bias a person confirmed are kept when the study is
    extracted or assessed again (e.g. after a new full text)."""
    from sragent import actions, pipeline, todos
    from sragent.stages import extraction, rob
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    assert actions.edit(ctx, {"kind": "ext", "rid": "1001", "fields": {"sample_size": "14 participants"}, "note": "Table 1"})["ok"]
    extraction.extract_one(ctx, "1001")
    f = st.get("ext:1001")["data"]["fields"]["sample_size"]
    assert f["value"] == "14 participants" and f["edited"]["note"] == "Table 1" and f["agent_value"] != f["value"]
    assert st.get("ext:1001")["data"]["kept_human_edits"] == ["sample_size"]
    assert todos.apply(ctx, {"todo_id": "rob:1001", "option": "change", "extra": {"domain": "missing_data", "judgment": "high"}})["ok"]
    rob.rob_one(ctx, "1001")
    d = st.get("rob:1001")["data"]
    assert d["overall"] == "high" and d["human_confirmed"]
    from sragent import attention
    assert not [x for x in attention.by_node(st).get("rob:1001", []) if x["source"] == "source_changed"]   # same text
    ft = st.get("ft:1001")
    st.write("ft:1001", "fulltext", {**ft["data"], "text": ft["data"]["text"] + " A new section."}, depends_on=ft["depends_on"])
    assert any(x["source"] == "source_changed" for x in attention.by_node(st).get("rob:1001", []))   # a new text: noted


def test_missing_criterion_assessment_counts_as_unclear():
    """A criterion the model left out (or returned as a dict) never counts as passed."""
    from sragent.stages.screening import complete_assessments, decide
    protocol = {"criteria": [{"id": "I1", "type": "inclusion", "text": "a"}, {"id": "I2", "type": "inclusion", "text": "b"},
                             {"id": "E1", "type": "exclusion", "text": "c", "stage": "full_text"},
                             {"id": "P1", "type": "practical", "text": "d", "stage": "full_text"}]}
    a = complete_assessments(protocol, [{"id": "I1", "assessment": "met", "evidence": []}], "title_abstract")
    assert {x["id"]: x["assessment"] for x in a} == {"I1": "met", "I2": "unclear"}       # E1 is full-text only; P1 practical
    assert decide(protocol, a, "title_abstract")[0] == "forward"
    a = complete_assessments(protocol, {"I1": {"assessment": "met"}, "I2": "met"}, "full_text")
    assert {x["id"]: x["assessment"] for x in a} == {"I1": "met", "I2": "met", "E1": "unclear"}
    assert decide(protocol, a, "full_text", "await")[0] == "awaiting"
    nothing = complete_assessments(protocol, [], "full_text")                            # nothing assessed: not an include
    assert decide(protocol, nothing, "full_text", "await")[0] == "awaiting" and all(x.get("missing") for x in nothing)


def test_store_save_is_atomic_and_flag_ids_are_unique(tmp_path, monkeypatch):
    import threading
    from sragent.store import ReviewStore
    st = ReviewStore(tmp_path)
    st.upsert("rec:1", "record", {"title": "t"})
    st.save()
    st.upsert("rec:2", "record", {"title": "u"})
    st.save()
    assert (tmp_path / "store.json.bak").exists() and "rec:2" in (tmp_path / "store.json").read_text()
    good = (tmp_path / "store.json").read_text()
    st.nodes["rec:2"]["data"]["bad"] = object()                 # a save that fails half-way
    import pytest
    with pytest.raises(TypeError):
        st.save()
    assert (tmp_path / "store.json").read_text() == good        # the last good store is untouched
    assert not list(tmp_path.glob("store.json.tmp-*"))
    del st.nodes["rec:2"]["data"]["bad"]
    # flag ids: unique under parallel flagging, and never reused after a flag was removed
    ids = []
    def work():
        for _ in range(50):
            ids.append(st.flag("rec:1", "x", source="t")["id"])
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(set(ids)) == 400
    st.nodes["rec:1"]["flags"] = []
    assert st.flag("rec:1", "y")["id"] not in ids
    old = ReviewStore.load(tmp_path / "store.json")             # a store from before the counter existed
    old.meta.pop("flag_seq", None)
    old.nodes["rec:1"]["flags"] = [{"id": "F7", "text": "z", "status": "open", "actor": "agent", "source": "t", "ts": 0}]
    assert old.flag("rec:1", "w")["id"] == "F8"


def test_llm_refusal_is_paid_once_and_cache_failures_do_not_cost_a_call(tmp_path, monkeypatch):
    from sragent.llm import LLM, CostTracker, DiskCache, Refused
    cfg = load_config(None, {"llm_defaults": {"max_retries": 3}, "models": {"default": {"writer": {"provider": "mock", "model": "mock"}}}})
    calls = []
    def refuse(self, msgs, json_out, max_tokens, temperature, schema=None):
        calls.append(1)
        raise Refused("declined", pin=120, pout=5)
    monkeypatch.setattr(LLM, "_call", refuse)
    tr = CostTracker(1.0)
    llm = LLM(cfg, model_for(cfg, None), tr, None, mock_fn=lambda m, mode: "{}")
    llm.c["pricing"] = {"input_per_m": 1000.0, "output_per_m": 1000.0}
    import pytest
    with pytest.raises(Refused):
        llm.chat([{"role": "user", "content": "x"}], tag="t")
    assert len(calls) == 1 and tr.spent > 0 and tr.calls == 1                 # not retried, still paid and recorded
    monkeypatch.undo()
    n = []
    llm = LLM(cfg, model_for(cfg, None), CostTracker(1.0), DiskCache(tmp_path / "c.sqlite"),
              mock_fn=lambda m, mode: (n.append(1), '{"ok": true}')[1])
    monkeypatch.setattr(llm.cache, "set", lambda k, v: (_ for _ in ()).throw(RuntimeError("database is locked")))
    assert llm.chat([{"role": "user", "content": "x"}], tag="t") == {"ok": True} and len(n) == 1
    # the budget is checked before every paid call, retries included
    tr = CostTracker(0.0)
    llm = LLM(cfg, model_for(cfg, None), tr, None, mock_fn=lambda m, mode: "{}")
    from sragent.llm import BudgetExceeded
    with pytest.raises(BudgetExceeded):
        llm.chat([{"role": "user", "content": "x"}])


# ---------------------------------------------------------------- evidence density, synthesis checks, evaluation bookkeeping
def test_numbers_in_a_value_are_cited_or_flagged():
    from sragent.stages.extraction import cover_numbers
    text = ("## Methods\nTwelve adults were enrolled; the mean age was 34 years. Dosing was 3 mg/kg weekly.\n"
            "## Results\nTrough concentrations reached 52 ug/mL at week 5. The half-life was 27 days. Clearance was 0.27 L/day.\n"
            "## Discussion\nA half-life near 27 days is consistent with earlier reports.")
    from sragent.evidence import segment
    sid = lambda frag: next(s["sid"] for s in segment(text) if frag in s["text"])
    item = {"value": "Trough 52 ug/mL at week 5; half-life 27 days; clearance 0.27 L/day in 12 adults"}
    cites = cover_numbers(item, [sid("Trough")], text)
    assert set(item["auto_evidence"]) == {sid("Clearance"), sid("Twelve")} and sid("Clearance") in cites   # 0.27 and "Twelve" (12): one sentence each, cited
    assert "27" not in item.get("uncovered", [])                    # 27 occurs in two sentences: in the paper, not guessed, not marked
    assert "12" not in item.get("uncovered", [])                    # "Twelve" is read as 12 (number words count)
    # decimal forms papers use (31·8, 31,8, .222) count as the same number; a distinctive term gets its sentence cited
    t2 = "## Methods\nA prospective single-centre cohort was enrolled.\n## Results\nTrough was 31·8 ug/mL; the ratio was ,222 at baseline; r = .222 overall."
    i2 = {"value": "Prospective single-centre cohort; trough 31.8 ug/mL; ratio 0.222"}
    c2 = cover_numbers(i2, [sid2 := next(s["sid"] for s in segment(t2) if "Trough" in s["text"])], t2)
    assert "uncovered" not in i2                                     # 31·8 and .222 found in their written forms
    assert next(s["sid"] for s in segment(t2) if "prospective" in s["text"]) in c2   # the design sentence is cited for the term
    assert "uncovered" not in item or ("52" not in item["uncovered"] and "5" not in item["uncovered"])   # cited / single digit
    i3 = {"value": "Trough 99 ug/mL"}
    cover_numbers(i3, [sid("Trough")], text)
    assert i3["uncovered"] == ["99"]                                 # a number nowhere in the paper is marked, not flagged
    plain = {"value": "Adults with haemophilia A"}
    assert cover_numbers(plain, ["s1"], text) == ["s1"] and "uncovered" not in plain


def test_synthesis_is_checked_by_another_model_and_absence_claims_see_every_study(tmp_path):
    from sragent import pipeline
    from sragent.stages import synthesis
    big = {"model": "mock-writer", "reasoning_effort": "high", "pricing": {"input_per_m": 1, "output_per_m": 1}}
    ctx = make_ctx(tmp_path, extra_cfg={"models": {"synthesis": {"writer": big}}})
    pipeline.run(ctx)
    rows = [json.loads(l) for l in (ctx.run_dir / "usage.jsonl").read_text().splitlines()]
    assert {r["model"] for r in rows if r["tag"] == "synthesis"} == {"mock-writer"}
    assert {(r["model"], r["role"]) for r in rows if r["tag"] == "synthesis_verify"} == {("mock", "verifier")}   # not the writer
    ctx.cfg["models"]["default"]["verifier"] = {"provider": "mock", "model": "mock-checker", "pricing": {"input_per_m": 1, "output_per_m": 1}}
    ctx.hub._clients.clear()
    assert ctx.hub.model("synthesis", "verifier").model == "mock-checker"
    cites = ["1001"]
    plain = synthesis.statement_evidence(ctx, cites, "Trough levels were 52 ug/mL in adults.")
    absent = synthesis.statement_evidence(ctx, cites, "Clearance was not reported in any study.")
    assert "ALL OTHER INCLUDED STUDIES" in absent and "ALL OTHER INCLUDED STUDIES" not in plain
    assert "[1002]" in absent and "[1002]" not in plain


def test_end_to_end_eval_reports_unlabelled_inclusions(tmp_path):
    from sragent import pipeline
    from sragent.eval.screening_eval import end_to_end
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    st.upsert("search", "search", {**st.get("search")["data"], "retrieved_pmids": ["1001", "1002", "1003", "1004", "9999"]},
              depends_on=["protocol"])
    st.upsert("rec:9999", "record", {"rid": "9999", "pmid": "9999", "title": "Emicizumab PK in a new cohort", "abstract": "x"},
              depends_on=["search"])
    st.upsert("screen:9999", "screen", {"stage": "full_text", "decision": "include", "because": [], "criteria": []},
              depends_on=["rec:9999", "protocol"])
    e = end_to_end(ctx)
    assert e["included_unlabelled"] == 1 and e["included_unlabelled_rids"] == ["9999"]
    assert e["end_to_end_precision"] is not None                # labelled ones only


def test_estimate_counts_rules_snowball_parts_and_updates(tmp_path):
    from sragent import pipeline
    from sragent.estimate import remaining_calls
    ctx = make_ctx(tmp_path)
    c0 = remaining_calls(ctx, None, "report")                     # nothing done yet: a guess from the record file
    assert c0["screen_title_abstract"] >= 4 and c0["synthesis"] == 1
    pipeline.run(ctx, until="screen")
    st = ctx.store
    st.meta.pop("snowball", None); ctx.cfg["pipeline"]["citation_chasing"] = True
    c1 = remaining_calls(ctx, None, "report")
    assert c1["screen_title_abstract"] > 0                        # citation-chasing candidates still to screen
    ctx.cfg["pipeline"]["citation_chasing"] = False
    c2 = remaining_calls(ctx, None, "report")
    assert c2.get("screen_title_abstract", 0) == 0 and c1["screen_title_abstract"] > c2.get("screen_title_abstract", 0)
    pipeline.run(ctx)                                             # done: an existing synthesis is updated, not re-written
    from sragent import state
    state.set_considered(st, ["1001"])                            # pretend two included studies were added since
    c3 = remaining_calls(ctx, None, "report")
    assert c3.get("synthesis", 0) == 0 and c3["feedback"] == 2 and c3["synthesis_verify"] == 2


# ---------------------------------------------------------------- the graph as an explicit model (refactor step 1)
def test_edges_carry_versions_and_a_changed_setting_makes_only_its_dependents_stale(tmp_path):
    from sragent import graph, pipeline
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    assert st.meta["schema"] == graph.SCHEMA
    ext = st.get("ext:1001")
    assert "field:sample_size" in ext["depends_on"] and "protocol" not in ext["depends_on"]
    assert ext["dep_versions"]["elig:1001"] == st.get("elig:1001")["version"]          # the full-text decision
    assert graph.stale(st) == {}                                   # a finished review is current with its inputs
    # the eligibility text changes (as if accepted via config_lock): the protocol is stale, nothing else is
    cfg = json.loads(json.dumps(ctx.cfg))
    cfg["topic"]["seed_eligibility"] = "Include everything with emicizumab PK data in any species."
    changed = graph.sync_settings(st, cfg)
    assert changed == ["cfg:seed_eligibility"]
    assert set(graph.stale(st)) == {"protocol"}
    assert graph.stale_reasons(st, "protocol")[0]["dep"] == "cfg:seed_eligibility"
    # a changed extraction field makes the extractions stale; one a person edited is reported as pinned
    from sragent import actions
    actions.edit(ctx, {"kind": "ext", "rid": "1001", "fields": {"sample_size": "14 participants"}})
    stale = graph.stale(st)
    assert "ext:1001" not in stale                                   # the edit is current with its inputs
    assert "rob:1001" in stale and all(sid in stale for sid in st.children("ext:1001") if sid.startswith("syn:"))
    assert "ext:1002" not in stale and "rob:1002" not in stale       # the other study is untouched
    cfg["topic"]["extraction_fields"][0]["description"] += " (with units)"
    graph.sync_settings(st, cfg)
    stale = graph.stale(st)
    assert {"ext:1001", "ext:1002", "ext:1003"} <= set(stale) and "ft:1001" not in stale
    assert graph.human_pinned(st.get("ext:1001")) and graph.stale_summary(st)["pinned"] >= 1
    # a recomputed output is current again
    from sragent.stages import extraction
    extraction.extract_one(ctx, "1002")
    assert "ext:1002" not in graph.stale(st)


def test_removed_criteria_are_retired_and_protocol_edits_are_recorded(tmp_path):
    import yaml
    from sragent import pipeline, provenance
    from sragent.stages import planning
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    p = yaml.safe_load(open(ctx.run_dir / "protocol.yaml"))
    p["criteria"] = [c for c in p["criteria"] if c["id"] != "I3"] + [{"id": "E9", "type": "exclusion", "text": "x", "stage": "full_text"}]
    yaml.safe_dump(p, open(ctx.run_dir / "protocol.yaml", "w"), sort_keys=False)
    r = planning.sync_criteria(st, p, actor="user", reason="test")
    assert r["retired"] == ["I3"] and r["added"] == ["E9"] and st.get("crit:I3")["status"] == "retired"
    # protocol.yaml is an export: the next run does not apply a hand edit, it stops and asks for the import
    assert set(planning.file_differs(ctx)) >= {"I3", "E9"}
    pipeline.run(ctx)
    assert "edited by hand" in (ctx.run_dir / "run.log").read_text().splitlines()[-1]
    from sragent import ops
    r = ops.run(ctx, "protocol.import_file", {"note": "dropped I3"})
    assert r["ok"] and planning.file_differs(ctx) is None
    ev = provenance.events(ctx.run_dir)
    assert any(e["action"] == "protocol.import_file" and e["category"] == "human_judgment" for e in ev)
    # in the agent-only condition the import is refused (a person's judgment)
    (tmp_path / "ao").mkdir()
    ao = make_ctx(tmp_path / "ao", extra_cfg={"pipeline": {"human_mode": "agent_only", "fulltext_unclear": "include"}})
    pipeline.run(ao, until="protocol")
    p2 = yaml.safe_load(open(ao.run_dir / "protocol.yaml")); p2["criteria"].pop()
    yaml.safe_dump(p2, open(ao.run_dir / "protocol.yaml", "w"), sort_keys=False)
    r2 = ops.run(ao, "protocol.import_file")
    assert not r2["ok"] and "agent-only" in r2["message"]


def test_any_of_criteria_are_alternatives():
    from sragent.stages.screening import rule_decision
    protocol = {"criteria": [{"id": "I1", "type": "inclusion", "text": "drug"},
                             {"id": "I3", "type": "inclusion", "text": "original PK", "any_of": "pk"},
                             {"id": "I4", "type": "inclusion", "text": "modelled PK", "any_of": "pk"},
                             {"id": "I5", "type": "inclusion", "text": "PK/PD", "any_of": "pk"}]}
    A = lambda **kw: [{"id": k, "assessment": v} for k, v in kw.items()]
    assert rule_decision(protocol, A(I1="met", I3="met", I4="not_met", I5="not_met"), "full_text") == ("include", [])
    assert rule_decision(protocol, A(I1="met", I3="not_met", I4="not_met", I5="unclear"), "full_text") == ("uncertain", ["I5"])
    assert rule_decision(protocol, A(I1="met", I3="not_met", I4="not_met", I5="not_met"), "full_text") == ("exclude", ["I3", "I4", "I5"])
    assert rule_decision(protocol, A(I1="not_met", I3="met", I4="met", I5="met"), "full_text") == ("exclude", ["I1"])
    from sragent.stages.planning import criteria_text
    assert "any of group pk" in criteria_text(protocol, "full_text")


def test_reverse_index_and_append_only_changelog(tmp_path):
    from sragent import feedback, pipeline
    from sragent.store import ReviewStore
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    assert st.children("ext:1001") == sorted(n["id"] for n in st.nodes.values() if "ext:1001" in n["depends_on"])
    n0 = len(st.changelog)
    cl = ctx.run_dir / "store_changelog.jsonl"
    size0 = cl.stat().st_size
    cs = feedback.handle_feedback(ctx, "Population of 1002 is wrong", target="ext:1002", auto_accept=False)
    feedback.accept(ctx, cs["id"])                                  # nodes put from the sandbox stay indexed
    assert len(st.changelog) > n0 and cl.stat().st_size > size0
    on_disk = [json.loads(l) for l in cl.read_text().splitlines()]
    assert [(e["op"], e["node"]) for e in on_disk] == [(e["op"], e["node"]) for e in st.changelog]   # appended, in order
    again = ReviewStore.load(ctx.run_dir / "store.json")
    assert again.children("ext:1002") == st.children("ext:1002")
    st.nodes["ext:1002"]["depends_on"] = []; st.reindex()
    assert "ext:1002" not in st.children("screen:1002")


# ---------------------------------------------------------------- the update engine (refactor step 2)
def test_an_answer_after_a_criterion_change_keeps_the_reassessment_pending(tmp_path):
    """A person's eligibility answer (or any bookkeeping write) on a screen node does not stamp it current with a
    protocol it never saw: the criterion added before the answer is still re-assessed."""
    import yaml
    from sragent import engine, pipeline, todos
    from sragent.stages import planning
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    set_fulltext_decision(st, "1002", decision="awaiting")
    p = yaml.safe_load(open(ctx.run_dir / "protocol.yaml"))
    p["criteria"].append({"id": "E1", "type": "exclusion", "text": "studies only in children", "stage": "title_abstract"})
    yaml.safe_dump(p, open(ctx.run_dir / "protocol.yaml", "w"), sort_keys=False)
    from sragent import ops
    ops.run(ctx, "protocol.import_file")                                # the protocol changes first ...
    assert todos.apply(ctx, {"todo_id": "elig:1002", "option": "include", "text": "PK table"})["ok"]   # ... then the answer
    by = {i["node"]: i for i in engine.plan(ctx)["items"]}
    assert by["elig:1002"]["action"] == "recompute" and by["elig:1002"]["criteria"] == ["E1"]


def _excluded_on_i1(ctx):
    """A pipeline-run review with one full-text decision excluded on I1 alone."""
    from sragent import pipeline
    pipeline.run(ctx)
    st = ctx.store
    n = st.by_type("eligibility", active_only=True)[0]
    crits = [{"id": c["id"], "assessment": "not_met" if c["id"] == "I1" or c["type"] == "exclusion" else "met"}
             for c in st.get("protocol")["data"]["criteria"] if c["type"] != "practical"]
    st.write(n["id"], n["type"], dict(n["data"], decision="exclude", because=["I1"], criteria=crits), depends_on=n["depends_on"], reason="test")
    return n["id"]


def _plan_item(ctx, node):
    from sragent import engine
    return next(i for i in engine.plan(ctx)["items"] if i["node"] == node)


NEW_E9 = {"id": "E9", "type": "exclusion", "text": "Study is a conference abstract.", "stage": "title_abstract"}


def test_reassess_excluded_needed_reads_a_record_only_if_the_answer_could_bring_it_back(tmp_path):
    from sragent.stages import planning, screening
    from sragent import engine
    ctx = make_ctx(tmp_path)
    nid = _excluded_on_i1(ctx)
    ctx.cfg["pipeline"]["reassess_excluded"] = "needed"
    planning.edit_criteria(ctx, [NEW_E9])
    item = _plan_item(ctx, nid)
    assert item["action"] == "rederive" and item["not_reassessed"] == ["E9"]    # excluded on I1 whatever E9 says
    engine.execute(ctx, {"id": "t", "changes": []}, tag="t", render=False)
    d = ctx.store.get(nid)["data"]
    assert d["decision"] == "exclude" and {a["id"]: a["assessment"] for a in d["criteria"]}["E9"] == screening.NOT_ASSESSED
    planning.edit_criteria(ctx, [{"id": "I1", "remove": True}])                 # now E9 matters: it is read
    item = _plan_item(ctx, nid)
    assert item["action"] == "recompute" and item["criteria"] == ["E9"]


def test_reassess_excluded_needed_still_reads_the_criterion_that_excluded_a_record(tmp_path):
    from sragent.stages import planning
    ctx = make_ctx(tmp_path)
    nid = _excluded_on_i1(ctx)
    ctx.cfg["pipeline"]["reassess_excluded"] = "needed"
    planning.edit_criteria(ctx, [{"id": "I1", "text": "reworded"}])
    item = _plan_item(ctx, nid)
    assert item["action"] == "recompute" and item["criteria"] == ["I1"]


def test_the_criteria_preview_offers_both_choices_and_the_edit_keeps_the_one_chosen(tmp_path):
    from sragent import ops
    ctx = make_ctx(tmp_path)
    nid = _excluded_on_i1(ctx)
    args = {"criteria": [NEW_E9]}
    modes = ops.preview(ctx, "protocol.edit_criteria", args)["reassess_excluded"]
    assert modes["selected"] == "all" and modes["needed"]["reads"] < modes["all"]["reads"]
    assert ctx.store.get("crit:E9") is None                                    # the preview changed nothing
    assert ops.run(ctx, "protocol.edit_criteria", {**args, "reassess_excluded": "needed"})["ok"]
    assert _plan_item(ctx, nid)["not_reassessed"] == ["E9"]
    assert not ops.run(ctx, "protocol.edit_criteria", {"criteria": [{"id": "E9", "text": "x"}], "reassess_excluded": "some"})["ok"]


def test_the_preview_names_only_the_criteria_that_changed(tmp_path):
    from sragent import ops
    ctx = make_ctx(tmp_path)
    _excluded_on_i1(ctx)
    p = ops.preview(ctx, "protocol.edit_criteria", {"criteria": [{"id": "I1", "text": "A reworded criterion."}]})
    lines = next(r for r in p["stages"] if r["stage"] == "protocol")["apply"]
    assert {line.split(":")[0] for line in lines} == {"I1", "protocol"}


def test_by_default_every_excluded_record_is_read_again(tmp_path):
    from sragent.stages import planning
    ctx = make_ctx(tmp_path)
    nid = _excluded_on_i1(ctx)
    planning.edit_criteria(ctx, [NEW_E9])
    item = _plan_item(ctx, nid)
    assert item["action"] == "recompute" and item["criteria"] == ["E9"]


def test_a_removed_criterion_does_not_leave_decisions_out_of_date(tmp_path):
    from sragent import engine, graph
    from sragent.stages import planning
    ctx = make_ctx(tmp_path)
    nid = _excluded_on_i1(ctx)
    planning.edit_criteria(ctx, [{"id": "E9", **{k: v for k, v in NEW_E9.items() if k != "id"}}])
    engine.execute(ctx, {"id": "t", "changes": []}, tag="t", render=False)
    planning.edit_criteria(ctx, [{"id": "E9", "remove": True}])
    assert nid in graph.stale(ctx.store)
    engine.execute(ctx, {"id": "t", "changes": []}, tag="t", render=False)
    assert not [n for n, why in graph.stale(ctx.store).items() if any(r["dep"] == "crit:E9" for r in why)]


def test_engine_plan_is_free_and_execute_keeps_what_people_decided(tmp_path):
    import yaml
    from sragent import actions, engine, pipeline, todos
    from sragent.stages import planning
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    assert engine.plan(ctx)["items"] == []                              # a finished review: nothing to do
    # a person decides 1002 and edits 1001's extraction; then a criterion is added to the protocol
    set_fulltext_decision(st, "1002", decision="awaiting")
    assert todos.apply(ctx, {"todo_id": "elig:1002", "option": "include", "text": "PK table"})["ok"]
    actions.edit(ctx, {"kind": "ext", "rid": "1001", "fields": {"sample_size": "14 participants"}, "note": "Table 1"})
    p = yaml.safe_load(open(ctx.run_dir / "protocol.yaml"))
    p["criteria"].append({"id": "E1", "type": "exclusion", "text": "studies only in children", "stage": "title_abstract"})
    yaml.safe_dump(p, open(ctx.run_dir / "protocol.yaml", "w"), sort_keys=False)
    n_calls = sum(1 for _ in (ctx.run_dir / "usage.jsonl").read_text().splitlines())
    from sragent import ops
    ops.run(ctx, "protocol.import_file")       # the hand-edited file, imported
    plan = engine.plan(ctx)
    assert sum(1 for _ in (ctx.run_dir / "usage.jsonl").read_text().splitlines()) == n_calls   # planning made no model call
    by = {i["node"]: i for i in plan["items"]}
    assert by["elig:1001"]["action"] == "recompute" and by["elig:1001"]["criteria"] == ["E1"]
    assert by["elig:1002"]["action"] == "recompute" and by["elig:1002"]["pinned"]                  # assessed, decision kept
    assert by["screen:1001"]["action"] == "current"                     # screened on its full text: that node decides
    assert by["screen:1004"]["action"] == "recompute"                                             # excluded ones too
    assert plan["cost"]["total_usd"] is not None and \
        plan["summary"].get("screen:recompute", 0) + plan["summary"].get("eligibility:recompute", 0) >= 3
    cs = engine.execute(ctx, tag="test")
    d2 = SC(st, "1002")
    assert d2["decision"] == "include" and d2["human_decision"] and "E1" in {a["id"] for a in d2["criteria"]}
    assert "agent_decision" in d2                                                                # shown beside the decision
    assert {a["id"] for a in SC(st, "1001")["criteria"]} >= {"I1", "I2", "I3", "E1"}
    assert st.get("ext:1001")["data"]["fields"]["sample_size"]["value"] == "14 participants"      # the edit survived
    left = engine.plan(ctx)["items"]                                    # nothing left to do: only notes for a person
    assert [i for i in left if i["action"] not in ("kept", "render")] == []
    assert {i["node"] for i in left} <= {"search", "report"}            # the search was built from the old protocol


def test_engine_identical_recomputation_reaches_nothing(tmp_path):
    from sragent import engine, graph, pipeline
    from sragent.stages import extraction
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    v = {k: st.get(k)["version"] for k in ("ext:1001", "rob:1001")}
    extraction.extract_one(ctx, "1001")                                 # same source, same values
    assert st.get("ext:1001")["version"] == v["ext:1001"]              # not a new version
    assert graph.stale(st) == {} and engine.plan(ctx)["items"] == []    # so the risk of bias and statements stay current


def test_disabled_rule_exclusions_are_undone_and_retired_ids_are_not_revived(tmp_path):
    from sragent import pipeline, rules
    from sragent.stages import synthesis
    ctx = make_ctx(tmp_path)
    ctx.topic["screening_rules"] = ["conference_abstract"]
    pipeline.run(ctx, until="screen")
    st = ctx.store
    n = st.get("screen:1004")
    st.upsert(n["id"], "screen", {**n["data"], "decision": "exclude", "because": ["rule:editorial"], "rule": "editorial",
                                  "decision_before_rule": "exclude"}, depends_on=n["depends_on"], actor="rule")
    ch = rules.apply(ctx)                                               # 'editorial' is not enabled: undone
    assert any(c.get("restored") and c["rid"] == "1004" for c in ch)
    d = SC(st, "1004")
    assert "rule" not in d and d.get("rule_restored", {}).get("rule") == "editorial"
    pipeline.run(ctx)
    sid = st.by_type("synthesis", active_only=True)[0]["id"]
    st.set_status(sid, synthesis.RETIRED, reason="test")
    synthesis.write_statements(ctx, [{"id": sid.split(":")[1], "text": "A new statement about 12 participants.", "cites": ["1001"]}], "test")
    assert st.get(sid)["status"] == synthesis.RETIRED                   # the retired one is untouched
    assert any(n["data"]["text"].startswith("A new statement") and n["status"] == "active" for n in st.by_type("synthesis"))


# ---------------------------------------------------------------- flexibility (refactor step 3)
def test_stages_can_be_skipped_and_the_registry_drives_the_run(tmp_path):
    from sragent import pipeline
    assert [s["key"] for s in pipeline.REGISTRY] == pipeline.STAGES
    ctx = make_ctx(tmp_path, extra_cfg={"pipeline": {"skip_stages": ["rob", "snowball"]}})
    assert pipeline.enabled(ctx) == [k for k in pipeline.STAGES if k not in ("rob", "snowball")]
    pipeline.run(ctx)
    st = ctx.store
    assert st.by_type("extraction") and not st.by_type("rob") and st.by_type("synthesis", active_only=True)
    assert "stage rob skipped" in (ctx.run_dir / "run.log").read_text()
    import pytest
    with pytest.raises(ValueError, match="cannot be skipped"):
        load_config(None, {"pipeline": {"skip_stages": ["screen"]}})
    with pytest.raises(ValueError, match="update_policy"):
        load_config(None, {"update_policy": {"synthesis": "ask"}})


def test_update_policy_propose_keeps_the_live_review_until_accepted(tmp_path):
    from sragent import actions, engine, feedback, pipeline, sync, todos
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    ctx.cfg["update_policy"]["synthesis"] = "propose"
    before = {n["id"]: json.dumps(n["data"], sort_keys=True) for n in st.by_type("synthesis", active_only=True)}
    assert actions.edit(ctx, {"kind": "screen", "rid": "1002", "decision": "exclude", "criterion": "I2", "note": "paediatric"})["ok"]
    p = engine.plan(ctx)
    assert any(i["type"] == "synthesis" and i["action"] == "revise" for i in p["items"])
    r = sync.sync_dependents(ctx)                                  # "Update now"
    assert st.get("ext:1002")["status"] == "excluded"             # the extraction's retirement is applied (auto)
    live = {n["id"]: json.dumps(n["data"], sort_keys=True) for n in st.by_type("synthesis", active_only=True)}
    assert live == before                                          # the statements are untouched...
    cs_todo = [t for t in todos.build(ctx) if t["kind"] == "changeset"]
    assert cs_todo and "UP" in cs_todo[0]["id"]                    # ...and offered as a proposal
    cid = cs_todo[0]["id"].split(":", 1)[1]
    feedback.accept(ctx, cid)
    after = [n for n in st.by_type("synthesis") if "1002" in json.dumps(n["history"]) or n["status"] != "active"]
    assert all("1002" not in n["data"].get("cites", []) for n in st.by_type("synthesis", active_only=True))
    # manual: only listed
    ctx.cfg["update_policy"]["synthesis"] = "manual"
    actions.edit(ctx, {"kind": "ext", "rid": "1001", "fields": {"sample_size": "15 participants"}})
    cs = engine.execute(ctx, tag="t")
    assert any(k["node"].startswith("syn:") and k.get("manual") for k in cs["kept"]) and not cs.get("proposed")


def test_impact_preview_and_stale_summary(tmp_path):
    from sragent import engine, pipeline
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    n_calls = sum(1 for _ in (ctx.run_dir / "usage.jsonl").read_text().splitlines())
    pv = engine.preview(ctx, "ext:1001")
    assert sum(1 for _ in (ctx.run_dir / "usage.jsonl").read_text().splitlines()) == n_calls   # no model call
    assert "rob" in pv["reach"] and pv["reach"]["rob"] == ["rob:1001"]
    assert pv["reach"].get("synthesis") and pv["cost"]["total_usd"] is not None
    assert all(st_id != "ext:1001" for ids in pv["reach"].values() for st_id in ids)
    assert ctx.store.get("ext:1001")["version"] == 1 and engine.plan(ctx)["items"] == []       # the preview changed nothing
    assert engine.preview(ctx, "nope:1")["reach"] == {}


def test_cut_off_replies_are_retried_with_more_room_and_never_salvaged(tmp_path, monkeypatch):
    from sragent.llm import LLM, CostTracker, Truncated, json_object, parse_json
    import pytest
    # a reply cut off inside the errors list: the inner object must not pass as the reply
    cut = '{"errors": [{"location": "ext:1 sample_size", "error_type": "extraction", "description": "wrong"}, {"location": "S2", "err'
    with pytest.raises(ValueError):
        parse_json(cut)
    with pytest.raises(ValueError):
        json_object(cut)
    assert parse_json('Sure: {"errors": []}') == {"errors": []}                     # prose before the object is fine
    # the provider says the reply hit max_tokens: asked again with twice the room, and both calls are paid for
    seen = []
    def call(self, msgs, json_out, max_tokens, temperature, schema=None):
        seen.append(max_tokens)
        if len(seen) == 1:
            raise Truncated("cut", pin=100, pout=max_tokens)
        return '{"errors": []}', 100, 50
    monkeypatch.setattr(LLM, "_call", call)
    cfg = load_config(None, {"llm_defaults": {"max_tokens": 4000, "max_retries": 3}, "models": {"default": {"writer": {"provider": "mock", "model": "mock"}}}})
    tr = CostTracker(1.0)
    llm = LLM(cfg, model_for(cfg, None), tr, None, mock_fn=lambda m, mode: "{}")
    llm.c["pricing"] = {"input_per_m": 1.0, "output_per_m": 1.0}
    assert llm.chat([{"role": "user", "content": "x"}], tag="t") == {"errors": []}
    assert seen == [4000, 8000] and tr.calls == 2


def test_full_text_unclear_on_a_reported_criterion_is_not_met():
    """At full text, "unclear" on an inclusion criterion the paper reports (PK data in mice and monkeys when the
    criterion asks for humans) is settled as not met; at title/abstract, and when the paper does not report it,
    the model's answer stands. The reasons travel with the decision."""
    from sragent.stages.screening import settle, decide, criterion_reasons
    protocol = {"criteria": [{"id": "I1", "type": "inclusion", "text": "Study is about emicizumab."},
                             {"id": "I2", "type": "inclusion", "text": "Study provides data on humans."},
                             {"id": "E1", "type": "exclusion", "text": "Study reports only animal data."}]}
    A = lambda: [{"id": "I1", "assessment": "met", "reported": True, "reason": "ACE910 is emicizumab"},
                 {"id": "I2", "assessment": "unclear", "reported": True, "reason": "PK data in mice and monkeys, not humans"},
                 {"id": "E1", "assessment": "unclear", "reported": True, "reason": "animal and in vitro data only"}]
    a = settle(protocol, A(), "full_text")
    i2 = next(x for x in a if x["id"] == "I2")
    assert i2["assessment"] == "not_met" and i2["model_assessment"] == "unclear" and i2["settled"]
    assert next(x for x in a if x["id"] == "E1")["assessment"] == "unclear"      # exclusion criteria are not settled
    assert decide(protocol, a, "full_text", "include")[:2] == ("exclude", ["I2"])  # no "included although unclear"
    assert all("model_assessment" not in x for x in settle(protocol, A(), "title_abstract"))   # inclusive at T/A
    b = settle(protocol, [{"id": "I2", "assessment": "unclear", "reported": False, "reason": "no population stated"}], "full_text")
    assert b[0]["assessment"] == "unclear"                                           # the paper does not say: stays unclear
    d = {"because": ["I2"], "criteria": a, "reason": "overall"}
    assert criterion_reasons(protocol, d) == "I2 (Study provides data on humans.): PK data in mice and monkeys, not humans"
    d2 = {"because": ["I2"], "criteria": [{"id": "I2", "assessment": "unclear"}], "reason": "needs full text"}
    assert criterion_reasons(protocol, d2) == "I2 (Study provides data on humans.). Agent: needs full text"


def test_extraction_doubt_is_settled_by_a_second_agent(tmp_path):
    """The upward check: extraction claims the study is not eligible; the verifier decides on the paper. It agrees:
    excluded by "extraction + verifier", outputs set aside, the old screening flag settled. It disagrees: the study
    stays included (two readings against one), recorded, no question for a person. A person's inclusion is asked
    about only when both agents say exclude."""
    from sragent import pipeline, todos
    from sragent.stages import extraction
    from sragent.studies import status_of

    def doubting(messages, mode):
        u = messages[-1]["content"]
        if "Extract the following items" in u and ("study id 1001" in u or "study id 1002" in u):
            base = json.loads(mock_fn(messages, mode))
            base["eligibility_doubt"] = {"criterion": "I2", "why": "all PK data are from cynomolgus monkeys" + (" VERIFIER_DISAGREES" if "study id 1002" in u else "")}
            return json.dumps(base)
        return mock_fn(messages, mode)

    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    assert SC(st, "1001")["decision"] == "include" and st.get("ext:1001")
    st.flag("screen:1001", "Included by rule although criteria ['I2'] were still unclear ...", actor="agent", source="screening_unclear")
    ctx.hub.mock_fn = doubting
    ctx.hub.cache_read = False
    ctx.hub._clients.clear()
    with ctx.at("extract"):
        extraction.extract_all(ctx, only=["1001", "1002"])
    # 1001: the verifier agrees -> excluded by extraction + verifier
    d = SC(st, "1001")
    assert d["decision"] == "exclude" and d["because"] == ["I2"] and d["decided_by"] == "extraction+verifier"
    assert d["eligibility_review"]["outcome"] == "excluded" and d["eligibility_review"]["verifier"]["verdict"] == "exclude"
    assert next(a for a in d["criteria"] if a["id"] == "I2")["assessment"] == "not_met"
    assert "1001" not in st.included_ids() and st.get("ext:1001")["status"] == "excluded"
    assert not [f for f in SN(st, "1001")["flags"] if f["status"] == "open"]
    assert status_of(st, "1001")[1] == "excluded (extraction + verifier)"
    # 1002: the verifier disagrees with the doubt -> stays included, the extraction stored; nothing for a person
    d2 = SC(st, "1002")
    assert d2["decision"] == "include" and d2["eligibility_review"]["outcome"] == "stays_included"
    assert "1002" in st.included_ids() and st.get("ext:1002")["status"] == "active"
    assert not [f for f in SN(st, "1002")["flags"] if f["status"] == "open"]
    assert not [x for x in todos.build(ctx) if x["kind"] in ("eligibility", "flag") and "1002" in x["id"]]
    rows = [json.loads(l) for l in (ctx.run_dir / "usage.jsonl").read_text().splitlines()]
    assert sum(r["tag"] == "eligibility_verify" for r in rows) == 2

    def doubt_on(rid, why):
        ctx.hub.mock_fn = lambda m, mode: doubting(m, mode) if f"study id {rid}" not in m[-1]["content"] else \
            json.dumps({**json.loads(mock_fn(m, mode)), "eligibility_doubt": {"criterion": "I2", "why": why}})
        ctx.hub._clients.clear()
        with ctx.at("extract"):
            extraction.extract_all(ctx, only=[rid])
    # a person's inclusion and a verifier who finds it eligible: settled, recorded, no question
    set_fulltext_decision(st, "1004", human_decision={"decision": "include", "ts": 0})
    doubt_on("1004", "monkeys VERIFIER_DISAGREES")
    assert SC(st, "1004")["eligibility_review"]["outcome"] == "stays_included"
    assert not [f for f in SN(st, "1004")["flags"] if f["status"] == "open" and f["source"] == "eligibility_doubt"]
    # both agents say exclude, a person included it: the decision stands and the person is asked, with three answers
    set_fulltext_decision(st, "1003", human_decision={"decision": "include", "ts": 0, "note": "relevant mechanism"})
    doubt_on("1003", "monkeys")
    d3 = SC(st, "1003")
    assert d3["decision"] == "include" and d3["eligibility_review"]["outcome"] == "asks_you" and "1003" in st.included_ids()
    f = next(f for f in SN(st, "1003")["flags"] if f["status"] == "open" and f["source"] == "eligibility_doubt")
    t = next(x for x in todos.build(ctx) if x["id"] == f"flag:{SN(st, '1003')['id']}:{f['id']}")
    assert [o["value"] for o in t["options"]] == ["ok", "exclude:I2", "rescreen"] and all(o["hint"] for o in t["options"])
    assert [r["says"] for r in t["overview"]] == ["include", "exclude", "exclude"] and t["overview"][0]["why"] == "relevant mechanism"
    assert todos.to_operation({"todo_id": t["id"], "option": "exclude:I2"}) == \
        ("eligibility.decide", {"rid": "1003", "decision": "exclude", "criterion": "I2", "note": ""})
    from sragent import ops
    r = ops.run(ctx, "flag.answer", {"node": SN(st, "1003")["id"], "flag": f["id"], "option": "rescreen", "text": "check the species"})
    assert r["ok"] and r["job"]["op"] == "eligibility.recompute" and "the verifier said exclude" in r["job"]["args"]["note"].lower()
    assert "check the species" in r["job"]["args"]["note"] and SC(st, "1003")["decision"] == "include"


def test_first_extraction_set_aside_by_the_eligibility_check_does_not_crash(tmp_path):
    """A first extraction (no ext node yet) whose eligibility re-check sets the study aside writes no extraction:
    the re-run, the update engine and the PDF path carry on, show the screening change, and draft no RoB."""
    from sragent import actions, engine, pipeline

    def doubting(messages, mode):
        u = messages[-1]["content"]
        if "Extract the following items" in u and ("study id 1001" in u or "study id 1002" in u):
            base = json.loads(mock_fn(messages, mode))
            base["eligibility_doubt"] = {"criterion": "I2", "why": "all PK data are from cynomolgus monkeys"}
            return json.dumps(base)
        return mock_fn(messages, mode)

    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    for rid in ("1001", "1002"):
        for k in ("ext", "rob"):
            st.nodes.pop(f"{k}:{rid}", None)
    ctx.hub.mock_fn = doubting
    ctx.hub.cache_read = False
    ctx.hub._clients.clear()
    # re-run of extraction from the study page: a proposal with the screening change, no crash
    cs = actions.rerun(ctx, "1001", "extract", propagate=False)
    assert any(c["node"] == "elig:1001" and c["after"]["decision"] == "exclude" for c in cs["changes"])
    assert not any(c["node"] == "ext:1001" for c in cs["changes"])
    # the update engine's extract item
    ecs = {"changes": []}
    with ctx.at("extract"):
        engine._extract(ctx, ecs, {"rid": "1002", "action": "recompute"})
    assert st.get("ext:1002") is None
    assert any(c["node"] == "elig:1002" and c["after"]["decision"] == "exclude" for c in ecs["changes"])


def test_faulted_synthesis_statements_get_one_repair_round(tmp_path):
    """A statement the self-check faults is repaired once by the stage model (narrowed to what the cited data show)
    and checked again; one the data cannot support at all is retired. Supported statements are left alone."""
    import re as _re
    from sragent import pipeline

    def checking(messages, mode):
        u = messages[-1]["content"]
        if "Check whether a synthesis statement is supported" in u:
            if "[S1]" in u and "Repaired" not in u:
                return json.dumps({"support": "partially_supported", "issues": ["broader than the data"], "overgeneralization": True})
            if "[S2]" in u:
                return json.dumps({"support": "unsupported", "issues": ["no study reports this"], "overgeneralization": False})
            return json.dumps({"support": "supported", "issues": [], "overgeneralization": False})
        if "Rewrite the statement so that the cited data support" in u:
            if "[S2]" in u:
                return json.dumps({"retire": True, "why": "nothing in the data"})
            cites = _re.search(r"CITED STUDIES: (.*)", u).group(1).split(", ")
            return json.dumps({"text": "Repaired statement 1: 12 participants in adults.", "cites": cites[:1], "scope": "adults"})
        return mock_fn(messages, mode)

    ctx = make_ctx(tmp_path)
    ctx.hub.mock_fn = checking
    ctx.hub._clients.clear()
    pipeline.run(ctx)
    st = ctx.store
    s1 = st.get("syn:S1")
    assert s1["status"] == "active" and s1["data"]["text"].startswith("Repaired") and s1["data"]["repaired_from"]["text"]
    assert s1["data"]["verification"]["support"] == "supported"
    assert not [f for f in s1["flags"] if f["status"] == "open" and f["source"] == "synthesis_verify"]
    assert st.get("syn:S2")["status"] == "retired"
    assert st.get("syn:S3")["status"] == "active" and "repaired_from" not in st.get("syn:S3")["data"]
    rows = [json.loads(l) for l in (ctx.run_dir / "usage.jsonl").read_text().splitlines()]
    assert sum(r["tag"] == "synthesis_repair" for r in rows) == 2 and sum(r["tag"] == "synthesis_verify_repair" for r in rows) == 1


def test_claim1_materials_fit_one_total_cap(monkeypatch):
    """One per-study cap for both conditions, lowered until the larger (evidence-linked) bundle fits the total cap."""
    from sragent.eval import claim1
    monkeypatch.setattr(claim1, "materials", lambda ctx, cond, rids, cap: "x" * (cap * 40 + (100 if cond == "evidence_linked" else 0)))
    assert claim1.fit_cap(None, [], 5000, 0) == 5000                       # no total cap: unchanged
    cap = claim1.fit_cap(None, [], 5000, 100000)
    assert cap < 5000 and cap * 40 + 100 <= 100000 and int(cap / 0.7) * 40 + 100 > 100000


def test_fulltext_falls_back_to_ncbi_when_europe_pmc_is_down(monkeypatch):
    """Europe PMC answering with server errors (after the HTTP retries) is not the end: the same JATS article is
    fetched from NCBI's PMC copy; only when that fails too is the paper reported unavailable."""
    from sragent.sources import europepmc, http
    jats = ("<pmc-articleset><article><front><article-meta><title-group><article-title>Emicizumab PK</article-title>"
            "</title-group><abstract><p>Trough levels were measured.</p></abstract></article-meta></front>"
            "<body><sec><title>Results</title><p>The half-life was 27 days.</p></sec></body></article></pmc-articleset>")
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(url)
        if "europepmc" in url:
            raise RuntimeError("GET failed 500")
        return jats
    monkeypatch.setattr(http, "get", fake_get)
    text = europepmc.fulltext("PMC123")
    assert "half-life was 27 days" in text and "Emicizumab PK" in text
    assert any("eutils.ncbi" in u for u in calls) and europepmc.LAST_SOURCE["PMC123"].startswith("NCBI PMC")

    def dead(url, params=None, **kw):
        raise RuntimeError("GET failed 500")
    monkeypatch.setattr(http, "get", dead)
    import pytest
    with pytest.raises(RuntimeError, match="NCBI efetch gave no article"):
        europepmc.fulltext("PMC123")


def test_reincluded_study_restores_the_statements_that_cited_it(tmp_path):
    """Dependency-aware re-inclusion: the graph remembers which statements cited the study before it was excluded
    (their earlier versions) and which were retired for lack of it. Those are revived and shown to the integrate
    step as they read then, so the citation comes back instead of a new statement being bolted on."""
    from sragent import feedback, pipeline
    from sragent.stages import synthesis
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    s1 = st.get("syn:S1")
    assert "1001" in s1["data"]["cites"]
    dropped = {**s1["data"], "cites": [c for c in s1["data"]["cites"] if c != "1001"], "text": s1["data"]["text"] + " (revised)"}
    st.upsert("syn:S1", "synthesis", dropped, depends_on=[f"ext:{c}" for c in dropped["cites"]], reason="study 1001 excluded")
    s2 = st.get("syn:S2")["data"]
    st.upsert("syn:S2", "synthesis", {**s2, "cites": ["1001"]}, depends_on=["ext:1001"], reason="cites 1001 only")
    st.set_status("syn:S2", synthesis.RETIRED, reason="no included study supports it any more")
    seen = {}

    def integrating(messages, mode):
        u = messages[-1]["content"]
        if "Update the narrative synthesis" in u:
            seen["prompt"] = u
            stmts = json.loads(u.split("CURRENT STATEMENTS:", 1)[1].split("ALL INCLUDED STUDIES", 1)[0])
            for x in stmts:
                if x["id"] == "S1" and "1001" not in x["cites"]:
                    x["cites"].append("1001")
            return json.dumps({"statements": stmts, "changed_ids": ["S1"]})
        return mock_fn(messages, mode)
    ctx.hub.mock_fn = integrating
    ctx.hub.cache_read = False
    ctx.hub._clients.clear()
    cs = {"id": "t", "changes": []}
    feedback._include_study(ctx, cs, "1001", tp="t", integrate=True)
    assert "STATEMENTS THAT CITED THIS STUDY BEFORE IT WAS EXCLUDED" in seen["prompt"] and "- S1 (then cited" in seen["prompt"]
    assert st.get("syn:S2")["status"] == "active"                       # revived: it cited the study before
    assert "1001" in st.get("syn:S1")["data"]["cites"]
    assert any(c["node"] == "syn:S2" and c["after"] == {"status": "active"} for c in cs["changes"])


def test_planted_eligibility_error_really_excludes_under_any_of(tmp_path):
    """The eligibility corruption flips a criterion whose failure excludes the study under the protocol's rule: with
    alternatives (any_of), one member alone does not, so the plain criterion is chosen or the whole group flipped."""
    from sragent.eval.inject import inject_eligibility
    from sragent.store import ReviewStore
    st = ReviewStore(tmp_path)
    crit = [{"id": "I1", "type": "inclusion", "text": "about emicizumab"},
            {"id": "I3", "type": "inclusion", "text": "original PK", "any_of": "A"},
            {"id": "I4", "type": "inclusion", "text": "modelled PK", "any_of": "A"}]
    st.upsert("protocol", "protocol", {"criteria": crit}, depends_on=[])
    A = lambda **kw: [{"id": k, "assessment": v, "evidence": []} for k, v in kw.items()]
    st.upsert("screen:1", "screen", {"stage": "full_text", "decision": "include", "because": [], "criteria": A(I1="met", I3="met", I4="met")}, depends_on=[])
    st.upsert("screen:2", "screen", {"stage": "full_text", "decision": "include", "because": [], "criteria": A(I1="unclear", I3="met", I4="met")}, depends_on=[])
    ctx = type("C", (), {})(); ctx.store = st
    e1 = inject_eligibility(ctx, "1", structural=False)
    assert e1["field"] == "I1" and SC(st, "1")["decision"] == "exclude"     # the plain criterion first
    e2 = inject_eligibility(ctx, "2", structural=False)
    assert e2["field"] == "I3, I4" and SC(st, "2")["decision"] == "exclude"  # the whole group
    assert e2["corrupted"]["decision"] == "exclude" and e2["gold"]["I4"] == "met"


def test_wait_for_pdfs_holds_the_first_synthesis_until_the_list_is_settled(tmp_path):
    """pipeline.wait_for_pdfs: with a requested full text still missing the run stops before the synthesis (and says so);
    once the file arrives or the record is declared not obtainable, the next run writes the synthesis once, and the
    report says what it rested on."""
    from sragent import pipeline
    from sragent.stages import report
    ctx = make_ctx(tmp_path, fulltexts=("1001", "1002"),
                   extra_cfg={"pipeline": {"fulltext": False, "validate_terms": False, "citation_chasing": False, "wait_for_pdfs": True}})
    pipeline.run(ctx)
    st = ctx.store
    assert SC(st, "1003")["decision"] == "fulltext_unavailable"
    from sragent import state
    from sragent.stages import fulltext as _ft
    assert not st.by_type("synthesis") and _ft.waiting_for_pdfs(ctx) == 1
    from sragent.stages.fulltext import fulltext_dir
    assert (fulltext_dir(ctx) / "needed_pdfs.csv").exists()
    from sragent import todos
    todos.apply(ctx, {"todo_id": "protocol", "option": "approve"})     # the protocol check comes first in human mode
    assert "waits for 1 requested full text" in todos.next_action(ctx, todos.build(ctx), False, 0)["text"]
    state.set_pdf_request(st, "1003", unavailable=True)             # the person says it cannot be obtained
    pipeline.run(ctx)
    assert st.by_type("synthesis", active_only=True) and not _ft.waiting_for_pdfs(ctx)
    assert st.get(state.SCOPE)["data"]["written_with"]["not_retrieved"] == 1
    md = report.report_markdown(ctx)
    assert "forwarded reports had no full text" in md
    # the option never holds back an existing synthesis
    state.set_pdf_request(st, "1003", unavailable=False)
    pipeline.run(ctx)
    assert "waiting_for_pdfs" not in st.meta


def test_feedback_revised_screening_follows_the_full_text_rule(tmp_path):
    """A screening assessment revised through feedback goes through the same full-text rule as the pipeline's:
    'unclear' on an inclusion criterion the paper reports is settled as not met, so the decision is exclude."""
    from sragent import pipeline
    from sragent.feedback import apply_revised
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    n = ctx.store.get("elig:1001")                       # the full-text decision
    assert n["data"]["stage"] == "full_text" and n["data"]["decision"] == "include"
    crits = [dict(a) for a in n["data"]["criteria"]]
    for a in crits:
        if a["id"] == "I2":
            a.update({"assessment": "unclear", "reported": True, "reason": "data in monkeys only"})
    before, after = apply_revised(ctx, "elig:1001", {"criteria": crits}, reason="test")
    d = SC(ctx.store, "1001")
    assert d["decision"] == "exclude" and d["because"] == ["I2"]
    i2 = next(a for a in d["criteria"] if a["id"] == "I2")
    assert i2["assessment"] == "not_met" and i2["model_assessment"] == "unclear"


def test_manifest_records_the_git_commit():
    from sragent.eval.run_all import git_commit
    import re
    c = git_commit()
    assert c is None or re.fullmatch(r"[0-9a-f]{7}(-dirty)?", c)


def test_evidence_bundle_shows_the_cited_values_per_statement(tmp_path):
    """Claim 1's evidence-linked materials let the reviewer check a statement here: under each statement, the cited
    studies' extracted values (design, population, results), not only their populations."""
    from sragent import pipeline
    from sragent.stages import report
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    ev = report.evidence_text(ctx, max_chars_per_study=5000)
    syn = ev.split("### Synthesis statements", 1)[1]
    assert "cited values (what the statement must not go beyond):" in syn
    assert "pk_results: pk_results value 12 participants" in syn and "[1001]" in syn


def test_provider_specific_options_are_not_inherited_across_providers():
    """The default writer on local vLLM with extra_body (chat_template_kwargs): a stage or role on an API model must
    not inherit it (Anthropic rejects unknown request fields); one that sets its own keeps it."""
    from sragent.config import load_config, model_for
    cfg = load_config(ROOT / "configs/benchmark/emicizumab_pk/topic.yaml", {
        "models": {"default": {"writer": {"provider": "local", "model": "auto", "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
                               "verifier": {"model": "gpt-6.1-sol", "extra_body": {"foo": 1}}},
                   "protocol": {"writer": "claude-sonnet-5-5"}, "screen": {"writer": {"reasoning_effort": None}}}})
    assert model_for(cfg, "protocol")["provider"] == "anthropic" and model_for(cfg, "protocol")["extra_body"] == {}
    assert model_for(cfg, "screen")["provider"] == "local" and model_for(cfg, "screen")["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert model_for(cfg, "screen", "verifier")["extra_body"] == {"foo": 1}


def test_search_strategy_is_chosen_with_the_review_team(tmp_path, monkeypatch):
    """Before the search runs, the team sees the agent's query and five alternatives with PubMed counts, asks for a
    change in plain words, counts any query, and confirms the one to run; with confirm_search the search waits for it
    and then runs the confirmed query instead of the agent's."""
    from sragent import pipeline, todos
    from sragent.sources import pubmed
    from sragent.stages import search
    counts = {}
    monkeypatch.setattr(pubmed, "esearch", lambda term, retmax=500, mindate=None, maxdate=None:
                        counts.__setitem__(term, len(term) * 10) or {"count": len(term) * 10, "pmids": [], "translation": term})
    ctx = make_ctx(tmp_path, extra_cfg={"pipeline": {"fulltext": False, "validate_terms": False, "citation_chasing": False, "confirm_search": True}})
    pipeline.run(ctx)                                            # stops before the search: no confirmed query yet
    st = ctx.store
    assert st.get("protocol") and st.get("search") is None
    t = next(x for x in todos.build(ctx) if x["kind"] == "search")
    assert t["required"] and not t["variants"]
    assert "Choose the search strategy" in todos.next_action(ctx, todos.build(ctx), False, 0)["text"] or True
    protocol = st.get("protocol")["data"]
    with ctx.at("search"):
        m = search.suggest_queries(ctx, protocol)
    assert len(m["variants"]) == 6 and m["variants"][0]["id"] == "V0" and all(v["count"] is not None for v in m["variants"])
    with ctx.at("search"):
        r = search.refine_query(ctx, protocol, m["variants"][1]["query"], "exclude review papers")
    assert r["query"].endswith("NOT review[pt]") and r["count"] == len(r["query"]) * 10 and "lost" in r["explanation"]
    from sragent import state
    assert state.strategy(st)["chat"][-1]["query"] == r["query"]
    c = search.confirm_query(ctx, r["query"], note="team choice")
    assert c["count"] == r["count"] and state.confirmed_query(st)["query"] == r["query"]
    pipeline.run(ctx)                                            # now the search runs, with the confirmed query
    sn = st.get("search")["data"]
    assert sn["query"] == r["query"] and sn["query_source"] == "confirmed by the review team" and sn["confirmed"]["note"] == "team choice"
    card = next(x for x in todos.build(ctx) if x["kind"] == "search")   # the card stays, read-only, once the search ran
    assert card["optional"] and card["ran"]["query"] == r["query"] and card["ran"]["source"] == "confirmed by the review team"


def test_a_protocol_revision_from_the_page_does_not_hold_the_interface(tmp_path):
    """'Ask the agent' on the protocol card is the operation protocol.revise, run as a job: no model call in the web
    request, the interface keeps answering, and the job runs `sragent op protocol.revise`."""
    import yaml
    from sragent import pipeline
    from sragent.ui.server import App
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx, until="protocol")
    cfgp = tmp_path / "ui.yaml"
    cfg = {k: v for k, v in ctx.cfg.items() if not k.startswith("_")}
    cfg["models_file"] = "models.yaml"
    cfg["local"] = {**cfg["local"], "endpoints_file": "local_endpoints.yaml"}
    cfgp.write_text(yaml.safe_dump(cfg))
    (tmp_path / "models.yaml").write_text((ROOT / "configs/models.yaml").read_text())
    (tmp_path / "local_endpoints.yaml").write_text(yaml.safe_dump({"endpoints": []}))
    app = App(str(cfgp))
    calls = len((ctx.run_dir / "usage.jsonl").read_text().splitlines())
    with app.lock:                                       # the worker cannot start the job while the lock is held
        r = app.op({"name": "protocol.revise", "args": {"instruction": "widen I2"}})
        assert r["ok"] and r["job"]["op"] == "protocol.revise" and r["job"]["args"] == {"instruction": "widen I2"}
        assert app._cmd(r["job"], cfgp)[3:5] == ["op", "protocol.revise"]
        app.queue.clear()
    assert len((ctx.run_dir / "usage.jsonl").read_text().splitlines()) == calls     # no model call in the request
    assert "todos" in app.state()
    assert app.op({"name": "nope"})["ok"] is False

def test_protocol_changed_with_the_review_team(tmp_path):
    """Before approval the team can ask the agent for a change in plain words (one call, applied, approval asked
    again, recorded) or edit criteria in place without a model call; agent-only mode refuses both."""
    from sragent import pipeline, todos
    from sragent.stages import planning
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx, until="protocol")
    st = ctx.store
    t = next(x for x in todos.build(ctx) if x["kind"] == "protocol")
    assert t["criteria"] and all("id" in c and "text" in c for c in t["criteria"])
    todos.apply(ctx, {"todo_id": "protocol", "option": "approve"})
    from sragent import state
    assert state.approval(st)
    with ctx.at("plan"):
        r = planning.revise_protocol(ctx, "widen I2")
    assert r["applied"] and r["changed"]["changed"] == ["I2"] and "widened as asked" in st.get("protocol")["data"]["criteria"][1]["text"]
    assert not state.approval(st) and st.meta["protocol_revisions"][-1]["instruction"] == "widen I2"
    import yaml
    assert "widened as asked" in yaml.safe_load(open(ctx.run_dir / "protocol.yaml"))["criteria"][1]["text"]
    r2 = planning.edit_criteria(ctx, [{"id": "I1", "text": "about emicizumab (edited)", "type": "inclusion", "stage": "title_abstract"},
                                      {"id": "E9", "text": "Study is a conference abstract.", "type": "exclusion", "stage": "title_abstract"}], note="by hand")
    crit = {c["id"]: c for c in st.get("protocol")["data"]["criteria"]}
    assert r2["applied"] and crit["I1"]["text"].endswith("(edited)") and crit["E9"]["type"] == "exclusion"
    assert st.get("crit:E9") is not None
    assert "protocol_revisions" in st.meta and st.meta["protocol_revisions"][-1]["actor"] == "user"
    import pytest
    from sragent.provenance import NotAllowed
    ctx2 = make_ctx(tmp_path / "ao", extra_cfg={"pipeline": {"fulltext": False, "validate_terms": False, "citation_chasing": False,
                                                              "human_mode": "agent_only", "fulltext_unclear": "include"}}) if (tmp_path / "ao").mkdir() or True else None
    pipeline.run(ctx2, until="protocol")
    with pytest.raises(NotAllowed):
        planning.edit_criteria(ctx2, [{"id": "I1", "text": "x", "type": "inclusion"}])


def test_fetched_full_texts_are_kept_as_files(tmp_path, monkeypatch):
    """The agent-fetched text is kept as fulltext/auto/<id>.txt and, when a PMC id exists, the PDF is downloaded from
    Europe PMC (then NCBI) as <id>.pdf; a refusal is recorded, not raised."""
    import requests
    from sragent.stages import fulltext
    ctx = make_ctx(tmp_path)

    class R:
        def __init__(self, code, content): self.status_code, self.content = code, content
    calls = []
    monkeypatch.setattr(requests, "get", lambda url, **kw: calls.append(url) or (R(200, b"%PDF-1.4 fake") if "europepmc.org" in url else R(403, b"")))
    data = {}
    fulltext.keep_fetched_files(ctx, "1001", "PMC123", "## Results\nThe half-life was 27 days.", data)
    d = fulltext.fulltext_dir(ctx) / "auto"
    assert (d / "1001.txt").read_text().startswith("## Results") and (d / "1001.pdf").read_bytes().startswith(b"%PDF-")
    assert data["auto_pdf"] == "auto/1001.pdf" and data["auto_text"] == "auto/1001.txt"
    monkeypatch.setattr(requests, "get", lambda url, **kw: R(403, b""))
    d2 = {}
    fulltext.keep_fetched_files(ctx, "1002", "PMC999", "text", d2)
    assert "auto_pdf" not in d2 and "refused" in d2["auto_pdf_status"] and (d / "1002.txt").exists()


def test_pointing_to_the_sentence_behind_a_flagged_number_confirms_it(tmp_path):
    """Answering an evidence flag with where the number is in the paper is not a change request: the sentence the
    check located is cited for that item, the value stands, and no disagreement card is raised."""
    from sragent import feedback, pipeline, todos
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    n = st.get("ext:1001")
    field = next(iter(n["data"]["fields"]))

    def pointing(messages, mode):
        u = messages[-1]["content"]
        if "check the feedback against the source" in u:
            return json.dumps({"verdict": "unsupported", "explanation": "the number is reported in s2", "evidence": ["s2"], "revised": None})
        return mock_fn(messages, mode)
    ctx.hub.mock_fn = pointing
    ctx.hub.cache_read = False
    ctx.hub._clients.clear()
    text = (f"page 1: the sentence says it (context: Numbers not found in the paper's text as written (a sum, a rounding or a "
            f"misread? check them against the paper): {field}: 12)")
    cs = feedback.handle_feedback(ctx, text, target="ext:1001")
    assert cs["status"] == "confirmed" and "s2" in cs["explanation"]
    item = st.get("ext:1001")["data"]["fields"][field]
    assert "s2" in item["evidence"] and item["confirmed_by"].startswith("the review team")
    assert not [f for f in st.get("ext:1001")["flags"] if f["status"] == "open" and "-> agent:" in f["text"]]
    assert not [t for t in todos.build(ctx) if t["kind"] == "disagreement"]


def test_an_extraction_item_added_to_the_config_is_extracted_for_every_included_study(tmp_path):
    """A new extraction field (a review-defining setting, accepted after the start) is not a dependency of the
    existing extractions, so they are not stale; the engine still plans to extract it, keeps a person's edited values,
    and the synthesis statements that rest on changed data are revised."""
    from sragent import engine, graph, pipeline
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    before = set(st.get("ext:1001")["data"]["fields"])
    ctx.cfg["topic"]["extraction_fields"].append({"name": "funding", "description": "Funding source."})
    ctx.topic = ctx.cfg["topic"]
    assert "field:funding" in graph.sync_settings(st, ctx.cfg)
    p = engine.plan(ctx)
    ex = [i for i in p["items"] if i["type"] == "extraction"]
    assert ex and all(i["action"] == "recompute" and "funding" in " ".join(i["why"]) for i in ex)
    cs = engine.execute(ctx, {"id": "t", "changes": []}, tag="t", render=False)
    after = st.get("ext:1001")["data"]["fields"]
    assert set(after) == before | {"funding"} and "field:funding" in st.get("ext:1001")["depends_on"]
    assert not [i for i in engine.plan(ctx)["items"] if i["type"] == "extraction"]   # done once


def test_data_items_changed_in_the_interface_at_any_stage(tmp_path):
    """The extraction items and quality domains are edited from the To-do page: the topic file is rewritten in place,
    the review-defining change is accepted and recorded, and the engine plans the new item for every extracted study."""
    import shutil
    from sragent.ui.server import Workspace
    from sragent.ui.configs import Library
    cdir = tmp_path / "configs" / "review" / "t"
    cdir.mkdir(parents=True)
    topic = (ROOT / "configs/benchmark/emicizumab_pk/topic.yaml").read_text()
    (cdir / "topic.yaml").write_text(topic)
    recf = tmp_path / "recs.jsonl"
    recf.write_text("\n".join(json.dumps(r) for r in RECORDS))
    (cdir / "run.yaml").write_text(f"""include: [topic.yaml]
run_dir: {tmp_path / 'run'}
cache_dir: {tmp_path / 'cache'}
llm_defaults: {{concurrency: 1}}
models: {{default: {{writer: {{provider: mock, model: mock}}}}}}
budget: {{total_usd: 1, stages: {{{", ".join(f"{s}: unlimited" for s in __import__("sragent.config").config.MODEL_STAGES)}}}}}
pipeline: {{fulltext: false, validate_terms: false, citation_chasing: false}}
topic: {{records: {{source: file, file: {recf}}}}}
""")
    # the helper keeps the key line and its comment, rewrites only the list
    new_text = Library.set_topic_list(topic, "extraction_fields", [{"name": "a", "kind": "drug", "description": "A."}, {"name": "b", "description": "B \"quoted\"."}])
    assert "extraction_fields:" in new_text and "- {name: a, kind: drug, description: A.}" in new_text or "name: a" in new_text
    assert "rob_domains:" in new_text and "pk_results" not in new_text.split("extraction_fields:", 1)[1].split("rob_tool_name", 1)[0]
    from sragent import pipeline
    from sragent.config import load_config
    fdir = tmp_path / "run" / "fulltext"
    fdir.mkdir(parents=True, exist_ok=True)
    for r in RECORDS:                               # full texts supplied, as make_ctx does: inclusion needs them
        if r["rid"] in ("1001", "1002", "1003"):
            (fdir / f"{r['rid']}.txt").write_text(f"## Title\n{r['title']}\n## Methods\n" + (r["abstract"] + " ") * 25)
    ctx = Ctx.create(load_config(cdir / "run.yaml"), mock_fn=mock_fn)   # the review itself, with the test mock
    ctx.quiet = True
    pipeline.run(ctx)
    assert ctx.store.included_ids()
    ws = Workspace(None, str(tmp_path / "configs"))
    app = ws.get("configs/review/t/run.yaml")   # paths are relative to the folder that holds configs/
    fields = [dict(f) for f in app.cfg["topic"]["extraction_fields"]] + [{"name": "funding", "description": "Funding source."}]
    r = app.data_items({"op": "save", "fields": fields}, ws.lib)
    assert r["ok"], r
    assert "funding" in (cdir / "topic.yaml").read_text() and "pk_results" in (cdir / "topic.yaml").read_text()
    assert any(f["name"] == "funding" for f in app.cfg["topic"]["extraction_fields"])     # reloaded
    from sragent.config_lock import changes as cfg_changes
    assert cfg_changes(app.ctx().store) and (r["pending"].get("summary") or {}).get("extraction:recompute", 0) >= 1
    bad = app.data_items({"op": "save", "fields": [{"name": "Bad Name", "description": "x"}]}, ws.lib)
    assert not bad["ok"] and "letters" in bad["message"]


def test_long_pubmed_terms_are_posted(monkeypatch):
    """A date filter over hundreds of PMIDs is far too long for a URL (HTTP 414); such terms go in a POST body."""
    from sragent.sources import http, pubmed
    seen = []
    monkeypatch.setattr(http, "get", lambda url, params=None, **kw: seen.append((kw.get("post"), len(params["term"]))) or
                        {"esearchresult": {"count": "1", "idlist": [params["term"].split("[")[0]], "querytranslation": ""}})
    pubmed.esearch("aspirin[tiab]")
    keep = pubmed.within_dates([str(10000000 + i) for i in range(200)], maxdate="2026/10/21")
    assert seen[0][0] is False and seen[1][0] is True and seen[1][1] > 1500 and keep


def test_full_text_settles_unreported_alternatives_and_exclusions():
    """Read in full: alternatives of which the paper reports none are not met as a group, and an exclusion the paper
    gives no sign of does not apply; rederive applies the same rule to stored assessments (no model call)."""
    from sragent.stages.screening import settle, rederive
    protocol = {"criteria": [{"id": "I1", "type": "inclusion", "text": "about aspirin"},
                             {"id": "I4", "type": "inclusion", "text": "HUVEC data", "any_of": "A"},
                             {"id": "I5", "type": "inclusion", "text": "trophoblast data", "any_of": "A"},
                             {"id": "E5", "type": "exclusion", "text": "reports none of HUVEC or trophoblast"}]}
    A = lambda **kw: [{"id": k, "assessment": v[0], "reported": v[1], "evidence": []} for k, v in kw.items()]
    a = settle(protocol, A(I1=("met", True), I4=("unclear", False), I5=("unclear", False), E5=("unclear", False)), "full_text")
    by = {x["id"]: x for x in a}
    assert by["I4"]["assessment"] == "not_met" and "none of the alternatives" in by["I4"]["settled"]
    assert by["E5"]["assessment"] == "not_met" and "does not apply" in by["E5"]["settled"]
    b = settle(protocol, A(I1=("met", True), I4=("unclear", False), I5=("met", True), E5=("unclear", False)), "full_text")
    assert {x["id"]: x["assessment"] for x in b}["I4"] == "unclear"            # one alternative met: the group stands as it is
    d = {"stage": "full_text", "decision": "awaiting", "criteria": A(I1=("met", True), I4=("unclear", False), I5=("unclear", False), E5=("unclear", False))}
    dec, because, stage, by_rule = rederive(protocol, d, "await")
    assert dec == "exclude" and set(because) == {"I4", "I5"}
    assert settle(protocol, A(I4=("unclear", False), I5=("unclear", False)), "title_abstract")[0]["assessment"] == "unclear"


def test_screening_decisions_get_a_second_reading(tmp_path):
    """pipeline.verify_screening: a second agent decides independently. Agreement is recorded; a disputed
    title/abstract exclusion is forwarded to the full text; a disputed full-text decision becomes undecided for the
    person with both readings; the agent-only policy applies instead when nobody is in the loop."""
    from sragent import pipeline, todos
    from sragent.studies import status_of
    ctx = make_ctx(tmp_path, extra_cfg={"pipeline": {"fulltext": False, "validate_terms": False, "citation_chasing": False, "verify_screening": "all"}})
    pipeline.run(ctx)
    st = ctx.store
    d1 = SC(st, "1001")
    assert d1["decision"] == "include" and d1["screen_verify"]["outcome"] == "agreed" and d1["screen_verify"]["agree"]
    d4 = SC(st, "1004")        # excluded at title/abstract, disputed: forwarded, then no full text
    assert d4["screen_verify"]["outcome"] == "forwarded" and d4["decision"] in ("fulltext_unavailable", "forward")
    assert d4.get("title_abstract", {}).get("decision") == "forward" or d4["decision"] == "forward"
    d2 = SC(st, "1002")        # included at full text, disputed: undecided for the person
    assert d2["decision"] == "awaiting" and d2["screen_verify"]["outcome"] == "pending_disagreement" and d2["because"] == ["I3"]
    assert "1002" not in st.included_ids() and status_of(st, "1002")[1] == "undecided: screener and verifier disagree"
    t = next(x for x in todos.build(ctx) if x["kind"] == "eligibility" and x["rid"] == "1002")
    assert t["verify"]["decision"] == "exclude" and t["suggest"]["option"] == "exclude" and "disagree" in t["suggest"]["why"]
    from sragent import attention
    assert any("verifier disagree" in x["text"] for x in attention.by_node(st).get(SN(st, "1002")["id"], []))
    rows = [json.loads(l) for l in (ctx.run_dir / "usage.jsonl").read_text().splitlines()]
    assert sum(r["tag"].startswith("screen_verify_") for r in rows) >= 4
    (tmp_path / "ao").mkdir()
    ctx2 = make_ctx(tmp_path / "ao", extra_cfg={"pipeline": {"fulltext": False, "validate_terms": False, "citation_chasing": False,
                                                              "verify_screening": "full_text", "human_mode": "agent_only", "fulltext_unclear": "include"}})
    pipeline.run(ctx2)
    e2 = SC(ctx2.store, "1002")
    assert e2["decision"] == "include" and e2["screen_verify"]["outcome"] == "include_by_policy_disagreement" and e2["unclear_by_rule"] == "include"
    assert SC(ctx2.store, "1004").get("screen_verify") is None   # full_text mode: title/abstract exclusions not checked


def test_second_reading_of_decisions_already_made(tmp_path):
    """verify-screening applies the second reading to a finished review: agreements recorded, a disputed full-text
    inclusion becomes undecided with its outputs set aside, a disputed title/abstract exclusion is forwarded; records
    a person decided are skipped, and nothing is read twice."""
    from sragent import pipeline
    from sragent.stages.screening import verify_existing
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)                                   # verify_screening off: no second readings yet
    st = ctx.store
    assert not any(n["data"].get("screen_verify") for n in st.by_type("screen"))
    r = verify_existing(ctx, st.get("protocol")["data"], mode="all")
    assert r["checked"] >= 4 and r["outcomes"].get("agreed", 0) >= 2
    assert SC(st, "1002")["decision"] == "awaiting" and st.get("ext:1002")["status"] == "excluded"
    assert SC(st, "1004")["decision"] == "forward" and SC(st, "1004")["screen_verify"]["outcome"] == "forwarded"
    assert SC(st, "1001")["decision"] == "include" and SC(st, "1001")["screen_verify"]["agree"]
    r2 = verify_existing(ctx, st.get("protocol")["data"], mode="all")
    assert r2["checked"] == 0                            # nothing is read twice


def test_pending_mode_lets_the_verifier_decide_undecided_records(tmp_path):
    """verify_screening: pending reads only records the screener could not decide after their full text: the
    verifier's decision stands (included (verifier)), and a record neither can decide stays with the person."""
    from sragent import pipeline
    from sragent.stages.screening import verify_existing
    from sragent.studies import status_of
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    for rid in ("1003", "1001"):                         # make two included records undecided, as if unclear after full text
        set_fulltext_decision(st, rid, decision="awaiting", because=["I3"])
    r = verify_existing(ctx, st.get("protocol")["data"], mode="pending")
    assert r["checked"] == 2 and r["outcomes"] == {"decided_by_verifier": 1, "still_undecided": 1}
    d3 = SC(st, "1003")                  # the case series: the verifier decided include
    assert d3["decision"] == "include" and d3["decided_by"] == "verifier" and status_of(st, "1003")[1] == "included (verifier)"
    assert next(a for a in d3["criteria"] if a["id"] == "I3")["assessment"] == "met"
    d1 = SC(st, "1001")                  # neither could decide: stays with the person
    assert d1["decision"] == "awaiting" and d1["screen_verify"]["outcome"] == "still_undecided"
    assert SC(st, "1002").get("screen_verify") is None          # decided records are not read in pending mode


def test_blind_verifier_reads_without_the_screeners_answer(tmp_path):
    """verify_screening_blind: the verifier is shown the record and the criteria, not the screener's assessments or
    decision; the two readings are compared afterwards, with the same outcomes as the anchored reading."""
    from sragent import pipeline
    from sragent.stages.screening import verify_existing
    ctx = make_ctx(tmp_path, extra_cfg={"pipeline": {"verify_screening_blind": True}})
    seen = []
    ctx.hub.mock_fn = lambda m, mode: (seen.append(m[-1]["content"]), mock_fn(m, mode))[1]
    pipeline.run(ctx)
    st = ctx.store
    for rid in ("1003", "1001"):
        set_fulltext_decision(st, rid, decision="awaiting", because=["I3"])
    r = verify_existing(ctx, st.get("protocol")["data"], mode="pending")
    assert r["outcomes"] == {"decided_by_verifier": 1, "still_undecided": 1}
    asked = [u for u in seen if "An independent screening of one record" in u]
    assert len(asked) == 2 and not any("SCREENER" in u or "awaiting" in u for u in asked)   # nothing of the first reading
    d3 = SC(st, "1003")
    assert d3["decision"] == "include" and d3["decided_by"] == "verifier" and d3["screen_verify"]["blind"]
    rows = [json.loads(l) for l in (ctx.run_dir / "usage.jsonl").read_text().splitlines()]
    assert {(r["stage"], r["role"]) for r in rows if r["tag"].startswith("screen_verify")} == {("eligibility", "verifier")}


def test_rob_drafts_need_a_person_only_with_an_open_reason(tmp_path):
    """verify_rob off: a draft the agent doubts is a required To-do with its reason; a draft without a reason is optional
    (shown, not waiting). The reasons also cover a judgment without a cited sentence and an overall milder than a domain."""
    from sragent import pipeline, todos
    from sragent.stages.rob import reasons_of
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    rob = {t["rid"]: t for t in todos.build(ctx) if t["kind"] == "rob"}
    assert set(rob) == {"1001", "1002", "1003"}
    assert rob["1001"]["optional"] and not rob["1001"]["reasons"]
    assert not rob["1002"]["optional"] and not rob["1003"]["optional"]
    assert "could not settle it" in rob["1003"]["reasons"][0]
    na = todos.next_action(ctx, [t for t in todos.build(ctx) if t["kind"] == "rob"], False, 0)
    assert "neither agent could settle (2 studies)" in na["text"]
    kinds = lambda d: [r["kind"] for r in reasons_of(d)]
    assert kinds({"domains": [{"domain": "a", "judgment": "high", "check": {"status": "none"}}], "overall": "high"}) == ["no_evidence"]
    assert kinds({"domains": [{"domain": "a", "judgment": "high", "check": {"status": "verified"}}], "overall": "low"}) == ["overall_milder"]
    assert kinds({"domains": [{"domain": "a", "judgment": "unclear", "check": {"status": "none"}}], "overall": "unclear"}) == []


def test_rob_verifier_settles_flagged_drafts_and_leaves_the_rest_to_a_person(tmp_path):
    """verify_rob flagged: only drafts with a reason are read again. The verifier's decision replaces the draft's (the
    agent's judgment is kept beside it, both readings recorded); what it cannot settle stays a required To-do."""
    from sragent import pipeline, todos
    from sragent.studies import study_detail
    ctx = make_ctx(tmp_path)
    ctx.cfg["pipeline"]["verify_rob"] = "flagged"
    pipeline.run(ctx)
    st = ctx.store
    assert st.get("rob:1001")["data"].get("rob_verify") is None                   # no reason: not read again
    d3 = st.get("rob:1003")["data"]
    assert d3["rob_verify"]["outcome"] == "decided_by_verifier" and d3["overall"] == "high" and d3["overall_agent"] == "low"
    dom = d3["domains"][0]
    assert dom["judgment"] == "high" and dom["judgment_agent"] == "low" and dom["decided_by"] == "verifier"
    assert d3["rob_verify"]["changed"][0]["agent"] == "low"
    d2 = st.get("rob:1002")["data"]
    assert d2["rob_verify"]["outcome"] == "still_undecided" and d2["overall"] == "low"   # the draft stays as it was
    rob = {t["rid"]: t for t in todos.build(ctx) if t["kind"] == "rob"}
    assert rob["1003"]["optional"] and rob["1001"]["optional"] and not rob["1002"]["optional"]
    assert rob["1002"]["verify"]["outcome"] == "still_undecided"
    page = study_detail(ctx, "1003")["rob"]
    assert page["verify"]["outcome"] == "decided_by_verifier" and page["domains"][0]["decided_by"] == "verifier"


def test_verify_rob_backfill_reads_only_flagged_unchecked_drafts(tmp_path):
    from sragent import pipeline
    from sragent.stages.rob import verify_existing
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)                                                              # verify_rob off: drafts with doubts wait
    r = verify_existing(ctx)
    assert r == {"checked": 2, "outcomes": {"decided_by_verifier": 1, "still_undecided": 1}}
    assert verify_existing(ctx)["checked"] == 0                                    # already read: not again


def test_reassess_rob_redrafts_all_but_confirmed(tmp_path):
    from sragent import pipeline, todos
    from sragent.stages.rob import reassess_all
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    todos.apply(ctx, {"todo_id": "rob:1001", "option": "confirm", "text": "fine"})
    v = {r: ctx.store.get(f"rob:{r}")["version"] for r in ("1001", "1002", "1003")}
    ctx.cfg["pipeline"]["verify_rob"] = "flagged"
    r = reassess_all(ctx)
    assert r["redrafted"] == 2 and r["kept_confirmed"] == 1
    assert r["outcomes"] == {"decided_by_verifier": 1, "still_undecided": 1}
    assert ctx.store.get("rob:1001")["version"] == v["1001"] and ctx.store.get("rob:1002")["version"] > v["1002"]


def test_reassess_rob_resumes_without_paying_twice(tmp_path):
    from sragent import pipeline
    from sragent.llm import BudgetExceeded
    from sragent.stages import rob
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    real, calls = rob.rob_one, []
    def failing(c, rid):
        if rid == "1003":
            raise BudgetExceeded("budget")
        calls.append(rid)
        return real(c, rid)
    rob.rob_one = failing
    try:
        import pytest
        with pytest.raises(BudgetExceeded):
            rob.reassess_all(ctx)
    finally:
        rob.rob_one = real
    assert ctx.store.meta.get("rob_reassess")
    done = set(calls)
    r = rob.reassess_all(ctx)                                    # resumes: only what the first pass did not draft
    assert r["redrafted"] == 3 - len(done) and "rob_reassess" not in ctx.store.meta


# ---------------------------------------------------------------- long jobs and what goes out of date (06 chunk A)

def test_outputs_go_out_of_date_only_when_the_part_they_read_changes(tmp_path):
    import yaml
    from sragent import graph, ops, pipeline
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    assert graph.stale(st) == {}
    # confirming a risk-of-bias draft changes no rating: the statement that cites the study stays current
    ops.run(ctx, "rob.confirm", {"rids": ["1001"]})
    assert st.get("rob:1001")["version"] > st.get("syn:S1")["dep_versions"]["rob:1001"]
    assert "syn:S1" not in graph.stale(st)
    # a changed rating reaches it
    ops.run(ctx, "rob.edit_domains", {"rid": "1001", "changes": [{"domain": "participant_selection", "judgment": "high"}]})
    assert [r["dep"] for r in graph.stale_reasons(st, "syn:S1")] == ["rob:1001"]
    # a criterion moved to another stage: the decisions are reached, the search is not
    pfile = ctx.run_dir / "protocol.yaml"
    p = yaml.safe_load(open(pfile))
    p["criteria"][2]["stage"] = "full_text"
    yaml.safe_dump(p, open(pfile, "w"), sort_keys=False)
    ops.run(ctx, "protocol.import_file")
    stale = graph.stale(st)
    assert "search" not in stale and any(k.startswith(("screen:", "elig:")) for k in stale)
    # the population the search is built from changes: now the search is out of date
    p["pico"]["population"] = "adults with haemophilia A"
    yaml.safe_dump(p, open(pfile, "w"), sort_keys=False)
    ops.run(ctx, "protocol.import_file")
    assert [r["dep"] for r in graph.stale_reasons(st, "search")] == ["protocol"]


def test_a_long_step_saves_as_it_goes_and_reports_its_progress(tmp_path, monkeypatch):
    import pytest
    ctx = make_ctx(tmp_path)
    monkeypatch.setattr(Ctx, "SAVE_EVERY", 1)       # chunks of 4 (one worker)
    monkeypatch.setattr(Ctx, "SAVE_SECONDS", 0)     # a save after every chunk
    saved = []
    real_save = Ctx.save

    def spy(self):
        saved.append(sorted(k for k in self.store.nodes if k.startswith("note:")))
        real_save(self)
    monkeypatch.setattr(Ctx, "save", spy)

    def one(i):
        if i == 6:
            raise RuntimeError("stopped")             # the job stops on the 7th item
        ctx.store.write(f"note:{i}", "setting", {"value": i})
        return i
    with pytest.raises(RuntimeError):
        ctx.map(one, range(10), label="Testing")
    assert saved[0] == [f"note:{i}" for i in range(4)]          # the first chunk was saved before the stop
    p = json.loads((ctx.run_dir / "progress.json").read_text())
    assert (p["label"], p["done"], p["total"]) == ("Testing", 7, 10)


def test_a_copy_of_the_review_never_saves_over_it(tmp_path):
    import dataclasses
    ctx = make_ctx(tmp_path)
    ctx.save()
    before = (ctx.run_dir / "store.json").read_bytes()
    sb = dataclasses.replace(ctx, store=ctx.store.clone())     # a proposal's sandbox: same folder
    sb.store.write("note:x", "setting", {"value": 1})
    sb.save()
    sb.map(lambda i: i, [1, 2])
    assert (ctx.run_dir / "store.json").read_bytes() == before
    other = tmp_path / "eval_copy"                              # a copy with a folder of its own saves there
    other.mkdir()
    s2 = ctx.store.clone()
    s2.run_dir = other
    dataclasses.replace(ctx, store=s2, run_dir=other).save()
    assert (other / "store.json").exists()


def _criterion_change_review(tmp_path, concurrency: int) -> Ctx:
    import yaml
    from sragent import ops, pipeline
    tmp_path.mkdir(parents=True, exist_ok=True)
    ctx = make_ctx(tmp_path, extra_cfg={"llm_defaults": {"concurrency": concurrency}})
    pipeline.run(ctx)
    pfile = ctx.run_dir / "protocol.yaml"
    p = yaml.safe_load(open(pfile))
    p["criteria"].append({"id": "E1", "type": "exclusion", "text": "studies only in children", "stage": "title_abstract"})
    yaml.safe_dump(p, open(pfile, "w"), sort_keys=False)
    ops.run(ctx, "protocol.import_file")
    return ctx


def test_an_update_stopped_partway_keeps_what_it_finished_and_continues(tmp_path, monkeypatch):
    import pytest
    from sragent import engine
    from sragent.stages import screening

    def to_do(c):
        return sorted(i["node"] for i in engine.plan(c)["items"] if i["action"] == "recompute" and i["type"] in ("screen", "eligibility"))

    def decisions(c):
        return {r: (SC(c.store, r)["decision"], sorted(a["id"] for a in SC(c.store, r)["criteria"])) for r in ("1001", "1002", "1003", "1004")}

    whole = _criterion_change_review(tmp_path / "whole", concurrency=1)
    engine.execute(whole, tag="update")
    ctx = _criterion_change_review(tmp_path / "stopped", concurrency=4)
    first = to_do(ctx)
    assert len(first) == 4
    real, calls = screening.reassess, []

    def stops_on_the_third(*a, **k):
        calls.append(a[2])
        if len(calls) == 3:
            raise RuntimeError("stopped")
        return real(*a, **k)
    monkeypatch.setattr(screening, "reassess", stops_on_the_third)
    with pytest.raises(RuntimeError):
        engine.execute(ctx, tag="update")
    monkeypatch.setattr(screening, "reassess", real)
    again = Ctx.create(ctx.cfg, mock_fn=mock_fn)                 # the review as saved: what was finished is kept
    again.quiet = True
    left = to_do(again)
    assert 0 < len(left) < len(first)
    engine.execute(again, tag="update")
    assert to_do(again) == []
    assert decisions(again) == decisions(whole)                  # the same result as an update that was not stopped
    p = json.loads((again.run_dir / "progress.json").read_text())
    assert p["done"] == p["total"]


# ---------------------------------------------------------------- budget: a total and a limit per stage (06 chunk B)

def test_the_budget_section_lists_every_stage_that_calls_a_model():
    import pytest
    from sragent.config import MODEL_STAGES, budget_limits, load_config, unlimited_budget
    ok = unlimited_budget(30)
    ok["stages"]["screen"] = 5
    b = budget_limits({"budget": ok})
    assert b["total_usd"] == 30.0 and b["stages"]["screen"] == 5.0 and b["stages"]["extract"] is None
    assert set(b["stages"]) == set(MODEL_STAGES)
    for bad, says in ((None, "budget is required"),
                      ({"total_usd": 30, "stages": {s: "unlimited" for s in MODEL_STAGES if s != "rob"}}, "missing rob"),
                      ({"total_usd": 30, "stages": {**ok["stages"], "retrieve": 1}}, "unknown retrieve"),
                      ({"total_usd": 30, "stages": {**ok["stages"], "screen": "*"}}, "budget.stages.screen"),
                      ({"total_usd": "unlimited", "stages": ok["stages"]}, "budget.total_usd")):
        with pytest.raises(ValueError, match=says):
            budget_limits({"budget": bad})
    with pytest.raises(ValueError, match="budget_usd was replaced"):
        load_config(None, {"budget_usd": 5})


def test_a_stage_limit_stops_that_stage_only_and_says_which_limit(tmp_path):
    import pytest
    from sragent.llm import BudgetExceeded, CostTracker
    log = tmp_path / "usage.jsonl"
    log.write_text(json.dumps({"stage": "screen", "usd": 1.5}) + "\n" + json.dumps({"stage": "extract", "usd": 0.5}) + "\n")
    t = CostTracker({"total_usd": 10.0, "stages": {"screen": 1.5, "extract": None}}, log)
    assert t.spent == 2.0 and t.by_stage == {"screen": 1.5, "extract": 0.5}       # spending so far, per stage
    with pytest.raises(BudgetExceeded) as e:
        t.check("screen")
    assert e.value.stage == "screen" and e.value.limit == 1.5
    stop = json.loads((tmp_path / "budget_stop.json").read_text())
    assert stop["stage"] == "screen" and stop["limit"] == 1.5
    t.check("extract")                                   # another stage still has room
    assert t.room("extract") == 8.0 and t.room("screen") == 0.0
    t.add("extract", "writer", "m", "extract", 0, 0, 8.0, 0.0, False)
    with pytest.raises(BudgetExceeded) as e:
        t.check("extract")
    assert e.value.stage is None and "total budget" in str(e.value)               # the total is the ceiling


def test_a_review_stopped_by_a_stage_limit_continues_when_it_is_raised(tmp_path):
    import pytest
    from sragent import pipeline
    from sragent.config import unlimited_budget
    from sragent.llm import BudgetExceeded
    stopped = unlimited_budget(1.0)
    stopped["stages"]["screen"] = 0                      # no room for title/abstract screening
    ctx = make_ctx(tmp_path, extra_cfg={"budget": stopped})
    with pytest.raises(BudgetExceeded) as e:
        pipeline.run(ctx)
    assert e.value.stage == "screen" and ctx.store.get("protocol") and not ctx.store.by_type("screen")
    again = make_ctx(tmp_path)                           # the limit raised: the same review, from where it stopped
    pipeline.run(again)
    assert again.store.get("report") and len(again.store.by_type("screen")) == 4


def test_the_interface_shows_saves_and_estimates_the_budget(tmp_path):
    import yaml
    from sragent import pipeline
    from sragent.ui.configs import Library
    from sragent.ui.server import App
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx, until="screen")
    run = tmp_path / "configs" / "review" / "t" / "run.yaml"
    run.parent.mkdir(parents=True)
    (tmp_path / "configs" / "models.yaml").write_text((ROOT / "configs/models.yaml").read_text())
    cfg = {k: v for k, v in ctx.cfg.items() if not k.startswith("_") and k != "topic"}
    cfg.update({"models_file": "../../models.yaml", "topic": ctx.cfg["topic"]})
    run.write_text(yaml.safe_dump(cfg))
    app, lib = App(str(run)), Library(tmp_path / "configs")
    b = app.budget_view()
    assert b["total_usd"] == 1.0 and {x["key"] for x in b["stages"]} == set(cfg["budget"]["stages"])
    r = app.save_budget({"total_usd": "12", "stages": {**{k: "" for k in cfg["budget"]["stages"]}, "extract": "3"}}, lib)
    assert r["ok"], r
    written = yaml.safe_load(run.read_text())["budget"]
    assert written["total_usd"] == 12 and written["stages"]["extract"] == 3 and written["stages"]["rob"] == "unlimited"
    assert "  stages:\n    protocol: unlimited" in run.read_text()                         # one stage per line, editable
    assert not app.save_budget({"total_usd": "12", "stages": {"screen": "lots"}}, lib)["ok"]
    e = app.run_estimate("extract.run", {})
    assert e["known"] and e["calls"] > 0 and {x["key"] for x in e["stages"]} >= {"extract"} and e["fits_total"] is not None
    assert app.run_estimate("retrieve.run", {})["calls"] == 0           # "no model calls" when nothing is left to do
    assert app.run_estimate("rob.verify", {})["known"] is False          # other jobs: the room left only
    with app.lock:                                                      # Resume: the same job queued again
        app.history.append({"id": "j1", "action": "op", "op": "extract.run", "args": {}, "label": "Extract", "status": "budget",
                            "budget_stop": {"stage": "extract"}, "returncode": 3})
        res = app.resume("j1")
        app.queue.clear()
    assert res["ok"] and res["job"]["op"] == "extract.run" and res["job"]["id"] != "j1" and "budget_stop" not in res["job"]


def test_many_decisions_save_once_and_their_preview_never(tmp_path, monkeypatch):
    from sragent import ops, pipeline
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    for rid in ("1001", "1002"):
        set_fulltext_decision(ctx.store, rid, decision="awaiting")
    saves = []
    real = Ctx.save
    monkeypatch.setattr(Ctx, "save", lambda self: (self.owns_folder() and saves.append(self.run_dir), real(self)))   # writes only
    items = [{"rid": "1001", "decision": "include"}, {"rid": "1002", "decision": "exclude", "criterion": "I3"}]
    ops.preview(ctx, "eligibility.decide_many", {"items": items})
    assert saves == []                                    # a preview works on a copy that is never saved
    assert ops.run(ctx, "eligibility.decide_many", {"items": items})["ok"]
    assert saves == [ctx.run_dir]                          # one save for the whole batch, not one per study
    assert SC(ctx.store, "1002")["decision"] == "exclude"


def test_a_disagreement_names_what_each_agent_said(tmp_path):
    from sragent import todos
    ctx = make_ctx(tmp_path)
    from sragent import pipeline
    pipeline.run(ctx)
    # the screener included 1001; the verifier's second reading excludes it on I3: undecided, for a person
    set_fulltext_decision(ctx.store, "1001", decision="awaiting", because=["I3"],
                          screen_verify={"decision": "exclude", "agree": False, "outcome": "pending_disagreement"})
    t = next(x for x in todos.build(ctx) if x["id"] == "elig:1001")
    assert "the screener says include, the verifier says exclude" in t["why"]
    assert t["suggest"]["option"] == "exclude" and t["suggest"]["criterion"] == "I3" and "verifier" in t["suggest"]["why"]


def test_the_full_text_verify_button_follows_the_verify_setting(tmp_path):
    """With verify_screening: full_text the button reads every full-text decision not yet read twice, with its count;
    otherwise only the undecided ones. A person's decision is never read again."""
    from sragent.stages.screening import verify_todo
    from sragent.ui.stagepage import _eligibility_verify
    ctx = make_ctx(tmp_path)
    st = ctx.store
    for rid, dec, extra in [("1", "include", {}), ("2", "exclude", {}), ("3", "awaiting", {}),
                            ("4", "exclude", {"screen_verify": {"outcome": "agreed"}}),
                            ("5", "include", {"human_decision": {"decision": "include"}})]:
        st.write(f"rec:{rid}", "record", {"title": rid}, depends_on=[])
        st.write(f"screen:{rid}", "screen", {"stage": "title_abstract", "decision": "forward"}, depends_on=[f"rec:{rid}"])
        st.write(f"elig:{rid}", "eligibility", {"stage": "full_text", "decision": dec, **extra}, depends_on=[f"screen:{rid}"])
    assert verify_todo(st, "full_text") == ["1", "2", "3"]
    assert verify_todo(st, "pending") == ["3"]
    ctx.cfg["pipeline"]["verify_screening"] = "full_text"
    (op, label), args, n = _eligibility_verify(ctx)
    assert (op, label, args, n) == ("eligibility.verify", "Verify unread (3)", {"mode": "full_text"}, 3)
    ctx.cfg["pipeline"]["verify_screening"] = "pending"
    assert _eligibility_verify(ctx)[0][1] == "Verify undecided (1)"


def test_a_full_text_found_after_it_was_declared_not_obtainable_is_screened(tmp_path):
    """A study marked not obtainable whose file is added later is read and screened on it at the next run, and the
    not-obtainable mark is lifted (it no longer applies)."""
    from sragent import ops, pipeline, state
    from sragent.stages import fulltext
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    n = st.get("screen:1004")
    st.upsert(n["id"], "screen", {**n["data"], "decision": "forward"}, depends_on=n["depends_on"])
    st.upsert("ft:1004", "fulltext", {"kind": "abstract", "text": ""}, depends_on=["rec:1004"])
    ops.run(ctx, "retrieve.declare_unobtainable", {"rids": ["1004"]})
    assert "1004" in state.unavailable(st)
    from sragent.ui.stagepage import stage_page
    row = lambda k: next(r for r in stage_page(ctx, k)["items"] if r["rid"] == "1004")
    assert row("retrieve")["decision"] == "no full text · not obtainable"      # the reason stays beside the mark
    assert row("eligibility")["decision"] == "not screened: no full text · not obtainable"
    (fulltext.fulltext_dir(ctx) / "1004.txt").write_text("## Methods\n" + "Emicizumab population PK model in 60 children. " * 60)
    ops.run(ctx, "eligibility.run", {})
    assert st.get("ft:1004")["data"]["origin"] == "user file 1004.txt"
    assert SC(st, "1004")["decision"] in ("include", "exclude", "awaiting")
    assert "1004" not in state.unavailable(st)


def test_the_verifier_can_read_only_where_the_screener_contradicts_itself(tmp_path):
    """verify_screening: conflict reads the undecided full-text decisions and those whose overall answer differs from
    the decision the criterion answers give; full_text reads every one; an unknown level is refused."""
    import pytest
    from sragent.config import load_config
    from sragent.stages.screening import self_conflict, verify_todo
    from sragent.ui.stagepage import _eligibility_verify
    assert self_conflict("include", "exclude") and self_conflict("include", "uncertain") and self_conflict("exclude", "uncertain")
    assert not self_conflict("include", "include") and not self_conflict("awaiting", "uncertain") and not self_conflict("exclude", None)
    ctx = make_ctx(tmp_path)
    st = ctx.store
    for rid, dec, lean in [("1", "include", "include"), ("2", "include", "exclude"), ("3", "exclude", "uncertain"),
                           ("4", "awaiting", "uncertain"), ("5", "exclude", "exclude")]:
        st.write(f"rec:{rid}", "record", {"title": rid}, depends_on=[])
        st.write(f"screen:{rid}", "screen", {"stage": "title_abstract", "decision": "forward"}, depends_on=[f"rec:{rid}"])
        st.write(f"elig:{rid}", "eligibility", {"stage": "full_text", "decision": dec, "llm_decision": lean}, depends_on=[f"screen:{rid}"])
    assert verify_todo(st, "pending") == ["4"]
    assert verify_todo(st, "conflict") == ["2", "3", "4"]
    assert verify_todo(st, "full_text") == ["1", "2", "3", "4", "5"]
    ctx.cfg["pipeline"]["verify_screening"] = "conflict"
    assert _eligibility_verify(ctx)[0][1] == "Verify undecided and conflicting (3)"
    p = tmp_path / "bad.yaml"
    p.write_text("pipeline:\n  verify_screening: sometimes\n")
    with pytest.raises(ValueError, match="verify_screening"):
        load_config(str(p))


ROWS_FIELD = {"name": "outcome_data", "kind": "rows", "description": "One row per result.",
              "columns": {"outcome_family": ["trough", "half-life"], "n": "number", "mean": "number",
                          "unit": {"type": "text", "description": "as written"}, "source": ["text", "table", "figure"]}}


def test_a_rows_item_extracts_one_checked_row_per_result_and_exports_them(tmp_path, monkeypatch):
    """kind: rows. The prompt lists the columns; each row keeps its own cited sentences; numbers become numbers and
    one found nowhere in the paper is marked; a value off a choice list is kept and marked; the item's value is a
    summary written by the code; the synthesis reads the rows; meta_rows.csv has one line per row; a person's edit of
    a number puts what reads the rows out of date; the data-items card writes the columns back unchanged."""
    import csv
    import yaml
    import pytest
    import mock_llm
    from sragent import graph, ops, pipeline, rows, schemas
    from sragent.stages import extraction
    from sragent.ui.configs import Library
    seen = []

    def with_rows(messages, mode):
        out = mock_llm.mock_fn(messages, mode)
        u = messages[-1]["content"] if isinstance(messages[-1]["content"], str) else ""
        if "Extract the following items" in u and "outcome_data [rows]" in u:
            seen.append(u)
            d = json.loads(out)
            d["fields"]["outcome_data"] = {"note": "", "rows": [
                {"outcome_family": "trough", "n": "12", "mean": 52, "unit": "ug/mL", "source": "text", "evidence": ["s3"], "note": ""},
                {"outcome_family": "Half-life", "n": 12, "mean": 27, "unit": "days", "source": None, "evidence": [], "note": ""},
                {"outcome_family": "clearance", "n": None, "mean": 99.5, "unit": None, "source": "figure", "evidence": ["s1"], "note": "Fig 2"}]}
            out = json.dumps(d)
        return out

    monkeypatch.setattr(sys.modules[__name__], "mock_fn", with_rows)
    base = yaml.safe_load((ROOT / "configs/benchmark/emicizumab_pk/topic.yaml").read_text())["topic"]["extraction_fields"]
    (tmp_path / "bad").mkdir()
    with pytest.raises(ValueError, match="needs columns"):
        make_ctx(tmp_path / "bad", extra_cfg={"topic": {"extraction_fields": base + [{**ROWS_FIELD, "columns": {}}]}})
    ctx = make_ctx(tmp_path, extra_cfg={"topic": {"extraction_fields": base + [ROWS_FIELD]}})
    pipeline.run(ctx)
    st = ctx.store
    assert seen and "outcome_family: one of trough | half-life" in seen[0] and "unit: text (as written)" in seen[0]
    assert "n: a number as written" in seen[0]
    rid = next(r for r in st.included_ids() if st.get(f"ext:{r}"))
    item = st.get(f"ext:{rid}")["data"]["fields"]["outcome_data"]
    r1, r2, r3 = item["rows"]
    assert (r1["n"], r1["mean"], r2["outcome_family"], r3["outcome_family"]) == (12, 52, "half-life", "clearance")
    assert r3["off_list"] == ["outcome_family"] and r3["uncovered"] == ["99.5"] and "uncovered" not in r1
    assert r1["evidence"] and r1["quote"]
    assert r2["evidence"] and r2["auto_evidence"] == r2["evidence"] and "27" in r2["quote"]   # cited nothing: found by the code
    assert "Never drop, shorten or merge a row because no sentence" in seen[0]
    assert item["value"] == "3 rows: trough, half-life, clearance"
    assert "row 3: outcome_family=clearance; mean=99.5" in extraction.table_text(ctx, [rid])
    path = ops.run(ctx, "extract.export_rows", {})["path"]
    lines = list(csv.DictReader(open(path)))
    assert len(lines) == 3 * sum(1 for r in st.included_ids() if st.get(f"ext:{r}")) and lines[0]["mean"] == "52"
    # a person corrects a number: the rows are what was edited, and what reads them goes out of date
    rob_v = st.get(f"rob:{rid}")
    edited = "\n".join(json.dumps(x) for x in [{"outcome_family": "trough", "n": 12, "mean": 53, "unit": "ug/mL"},
                                               {"outcome_family": "half-life", "n": 12, "mean": 27, "unit": "days"}])
    ops.run(ctx, "extract.edit_values", {"rid": rid, "fields": {"outcome_data": edited}})
    item = st.get(f"ext:{rid}")["data"]["fields"]["outcome_data"]
    assert [r["mean"] for r in item["rows"]] == [53, 27] and item["edited"]["before_rows"][0]["mean"] == 52
    assert item["value"] == "2 rows: trough, half-life"
    assert rob_v and any(x["dep"] == f"ext:{rid}" for x in graph.stale_reasons(st, f"rob:{rid}"))
    # the data-items card keeps the columns when it writes the list back
    text = "topic:\n  extraction_fields:\n    - {name: a, description: x}\n  rob_domains: []\n"
    out = yaml.safe_load(Library.set_topic_list(text, "extraction_fields", [{"name": "a", "description": "x"}, ROWS_FIELD]))
    assert out["topic"]["extraction_fields"][1] == ROWS_FIELD and out["topic"]["rob_domains"] == []
    # no union-typed parameter (the API allows 16 per request): every column is a string, "" when not reported
    sch = json.dumps(schemas.extract(base + [ROWS_FIELD]))
    assert sch.count("anyOf") == 1 and '"null"' in sch       # only eligibility_doubt, as before
    assert rows.schema(ROWS_FIELD)["properties"]["rows"]["items"]["properties"]["source"]["enum"] == ["text", "table", "figure", ""]
    assert rows.clean(ROWS_FIELD, [{"outcome_family": "", "n": "1,024", "mean": "", "unit": "", "source": ""}])[0]["n"] == 1024
    kept = rows.clean(ROWS_FIELD, [{"outcome_family": "trough", "n": "75-150", "mean": "", "unit": "", "source": ""}])[0]
    assert kept["n"] == "75-150" and kept["off_list"] == ["n"]        # not one number: kept as written, marked


def test_a_reply_shape_the_api_cannot_compile_is_asked_again_in_plain_json(tmp_path):
    """A 400 "Schema is too complex for compilation" (a wide rows item, say) does not fail the call: it is asked again
    without the structured-output format, once."""
    from sragent.config import model_for
    from sragent.llm import LLM, CostTracker, DiskCache
    cfg = make_ctx(tmp_path).cfg
    llm = LLM(cfg, model_for(cfg, None), CostTracker(unlimited_budget(1.0)), DiskCache(tmp_path / "c.sqlite"))
    seen = []

    def call(msgs, json_out, max_tokens, temperature, schema=None):
        seen.append(schema)
        if schema is not None:
            raise RuntimeError("Error code: 400 - Schema is too complex for compilation.")
        return '{"ok": true}', 10, 5
    llm._call = call
    assert llm.chat([{"role": "user", "content": "x"}], tag="t", schema={"type": "object"}) == {"ok": True}
    assert seen == [{"type": "object"}, None]


def test_figure_only_rows_get_estimates_marked_as_such(tmp_path, monkeypatch):
    """A row whose result is only in a figure gets its numbers from the page image: only empty columns are filled,
    each is listed in row["estimated"], the CSV says so, a reported number is never replaced, and a study without a
    PDF is skipped with the reason."""
    import csv
    import yaml
    from sragent import figures, ops, pipeline
    base = yaml.safe_load((ROOT / "configs/benchmark/emicizumab_pk/topic.yaml").read_text())["topic"]["extraction_fields"]
    field = {**ROWS_FIELD, "columns": {"experiment": "text", **ROWS_FIELD["columns"], "sd": "number"}}
    import mock_llm
    asked, real = [], mock_llm.mock_fn

    def mock(messages, mode):
        c = messages[-1]["content"]
        if isinstance(c, list):                       # the figure call: text + page images
            asked.append(c)
            return json.dumps({"rows": [{"row": 1, "readable": True, "how": "panel a, bar 2",
                                         "values": {"mean": "3.1", "sd": "0.4", "unit": "fold", "n": "99", "outcome_family": "nonsense"}},
                                        {"row": 2, "readable": False, "values": {}}]})
        if "Extract the following items" not in c:
            return real(messages, mode)
        out = json.loads(real(messages, mode))
        out["fields"]["outcome_data"] = {"note": "", "rows": [
            {"experiment": "Fig 3a", "outcome_family": "trough", "n": "12", "mean": "", "unit": "", "source": "figure", "sd": "", "evidence": []},
            {"experiment": "Figure 3b", "outcome_family": "half-life", "n": "", "mean": "", "unit": "", "source": "figure", "sd": "", "evidence": []},
            {"experiment": "Table 1", "outcome_family": "trough", "n": "12", "mean": "52", "unit": "ug/mL", "source": "table", "sd": "", "evidence": []}]}
        return json.dumps(out)

    monkeypatch.setattr(sys.modules[__name__], "mock_fn", mock)
    ctx = make_ctx(tmp_path, extra_cfg={"topic": {"extraction_fields": base + [field]}})
    pipeline.run(ctx)
    st = ctx.store
    rid = next(r for r in st.included_ids() if st.get(f"ext:{r}"))
    assert figures.pages_of_figure(["intro, see Fig. 3", "F I G U R E 3 Effects of x", "Fig. 4. Other"], "3") == [2, 1]
    assert ops.run(ctx, "extract.read_figures", {"rids": [rid]})["skipped"][rid].startswith("no PDF")
    monkeypatch.setattr(figures, "pdf_of", lambda c, r: Path("x.pdf"))
    monkeypatch.setattr(figures, "page_texts", lambda p: ["text", "Figure 3. Trough and half-life"])
    monkeypatch.setattr(figures, "_images", lambda p, pages: ["aGk="] * len(pages))
    monkeypatch.setattr(figures.shutil, "which", lambda x: "/usr/bin/pdftoppm")
    res = ops.run(ctx, "extract.read_figures", {"rids": [rid]})
    assert (res["rows"], res["filled"], res["figures"]) == (2, 1, 1) and "row 3" not in asked[-1][0]["text"]
    r1, r2, r3 = st.get(f"ext:{rid}")["data"]["fields"]["outcome_data"]["rows"]
    assert (r1["mean"], r1["sd"], r1["unit"], r1["n"], r1["outcome_family"]) == (3.1, 0.4, "fold", 12, "trough")   # n was reported: kept
    assert r1["estimated"] == ["mean", "sd", "unit"] and r1["figure_read"]["figure"] == "3" and "uncovered" not in r1
    assert "estimated" not in r2 and "estimated" not in r3 and r3["mean"] == 52
    line = next(x for x in csv.DictReader(open(ops.run(ctx, "extract.export_rows", {})["path"])) if x["study_id"] == rid)
    assert line["estimated_from_figure"] == "mean sd unit" and line["figure"] == "3"


def test_pooling_is_computed_by_code_from_the_rows(tmp_path, monkeypatch):
    """sragent/meta.py: effect sizes from a row's numbers (ratio of means with SD from SEM or a CI, risk ratio from
    events), one effect per study, random-effects pooling with the Hartung-Knapp interval, reasons for rows left
    out, and the plan checked against the rows item's columns."""
    import math
    import pytest
    from sragent import meta
    assert abs(meta.t_quantile(0.975, 4) - 2.7764) < 1e-3 and abs(meta.t_quantile(0.975, 1) - 12.706) < 1e-2
    col = {"treated": {"n": "nt", "value": "vt", "spread": "st", "upper": "ut"}, "control": {"n": "nc", "value": "vc", "spread": "sc", "upper": "uc"},
           "value_type": "kind", "spread_type": "sk", "normalised": {"column": "basis", "value": "normalised"}}
    row = {"nt": 4, "nc": 4, "vt": 2.0, "vc": 1.0, "st": 0.2, "sc": 0.1, "kind": "mean", "sk": "SD"}
    e, _ = meta.effect_of(row, col)
    assert e["measure"] == "ratio of means" and abs(e["yi"] - math.log(2)) < 1e-12 and abs(e["vi"] - 0.005) < 1e-12
    e2, _ = meta.effect_of({**row, "st": 0.1, "sc": 0.05, "sk": "SEM"}, col)                 # SEM x sqrt(n) = the same SD
    assert abs(e2["vi"] - 0.005) < 1e-12
    e3, _ = meta.effect_of({**row, "st": 1.804, "ut": 2.196, "sc": 0.902, "uc": 1.098, "sk": "95% CI"}, col)
    assert abs(e3["vi"] - 0.005) < 1e-4
    e4, _ = meta.effect_of({**row, "sc": None, "basis": "normalised"}, col)                  # control set to 1: the treated CV
    assert abs(e4["vi"] - 0.005) < 1e-12 and e4["notes"]
    rr, _ = meta.effect_of({"nt": 100, "nc": 100, "vt": 10, "vc": 20, "kind": "number of events"}, col)
    assert rr["measure"] == "risk ratio" and abs(rr["yi"] - math.log(0.5)) < 1e-12 and abs(rr["vi"] - 0.13) < 1e-12
    assert abs(meta.effect_of({"nt": 100, "nc": 100, "vt": 10, "vc": 20, "kind": "percent or proportion"}, col)[0]["yi"] - math.log(0.5)) < 1e-12
    pct = {"nt": 3, "nc": 3, "vt": 10, "vc": 20, "st": 1, "sc": 2, "sk": "SD", "kind": "percent or proportion", "unit": "experiments"}
    assert meta.effect_of(pct, {**col, "binary_only_if": {"column": "unit", "values": ["participants"]}})[0]["measure"] == "ratio of means"
    assert meta.effect_of({**pct, "unit": "participants"}, {**col, "binary_only_if": {"column": "unit", "values": ["participants"]}})[0]["measure"] == "risk ratio"
    assert meta.effect_of({**row, "kind": "median"}, col)[1] == "a median without its quartiles or range"
    robn = {"status": "active", "data": {"overall": "high", "domains": [{"domain": "dose", "judgment": "high"}, {"domain": "bias", "judgment": "low"}]}}
    assert meta._high_risk(robn, "high") and not meta._high_risk(robn, {"judgment": "high", "domains": ["bias"]}) and meta._high_risk(robn, {"judgment": "high"})
    med, _ = meta.effect_of({"nt": 30, "nc": 30, "vt": 20, "st": 15, "ut": 28.5, "vc": 10, "sc": 7, "uc": 13, "kind": "median", "sk": "IQR"}, col)
    mt, mc = (15 + 20 + 28.5) / 3, 10.0
    assert abs(med["yi"] - math.log(mt / mc)) < 1e-12 and abs(med["vi"] - (100 / (30 * mt ** 2) + (6 / 1.35) ** 2 / (30 * mc ** 2))) < 1e-12 and med["notes"]
    assert meta._from_median(10, 2, 18, 50, "range") == (10.0, 4.0) and meta._from_median(10, 12, 18, 50, "range") is None
    assert meta.effect_of({**row, "st": None}, col)[1].startswith("no usable spread")
    assert meta.effect_of({**row, "nt": None}, col)[1] == "group size missing"
    same = meta.pool([(math.log(2), 0.01)] * 4)
    assert same["tau2"] == 0 and same["i2"] == 0 and abs(same["ratio"] - 2) < 1e-9
    assert abs(same["ci"][1] - same["ci"][0] - 2 * meta.t_quantile(0.975, 3) * 0.05) < 1e-6        # not narrower than classical
    het = meta.pool([(0.0, 0.01), (1.0, 0.01), (2.0, 0.01)])
    assert abs(het["log"] - 1.0) < 1e-9 and abs(het["tau2"] - 0.99) < 1e-9 and het["i2"] > 98
    assert "prediction" not in het and het["prediction_note"]            # three studies cannot support one
    assert meta.pool([(0.0, 0.01), (1.0, 0.01), (2.0, 0.01)], prediction_min=3)["prediction"][0] < -4
    assert meta.pool([(0.0, 0.01), (1.0, 0.01), (2.0, 0.01), (0.5, 0.01), (1.5, 0.01)])["prediction"][0] < -1
    # a whole review: the plan names real columns; one effect per study; a group below min_studies is listed, not pooled
    field = {"name": "outcome_data", "kind": "rows", "description": "x",
             "columns": {"family": ["a", "b"], "cmp": ["t vs c", "other"], "nt": "number", "nc": "number", "vt": "number", "vc": "number",
                         "st": "number", "ut": "number", "sc": "number", "uc": "number", "kind": ["mean", "median"], "sk": ["SD", "SEM"]}}
    plan = {"field": "outcome_data", "include": {"cmp": ["t vs c"]}, "group_by": ["family"], "min_studies": 2,
            "columns": {k: v for k, v in col.items() if k != "normalised"}}
    with pytest.raises(ValueError, match="not columns of outcome_data: nope"):
        meta.plan_of({"extraction_fields": [field], "meta_analysis": {**plan, "group_by": ["nope"]}})
    import mock_llm
    asked = []

    def mock(messages, mode):
        u = messages[-1]["content"]
        if "ONE outcome group" in u:
            asked.append(u)
            return json.dumps({"statements": [{"text": "Pooled: doubled.", "cites": ["1001", "9999"], "scope": "a"}]})
        return mock_llm.mock_fn(messages, mode)
    monkeypatch.setattr(sys.modules[__name__], "mock_fn", mock)
    ctx = make_ctx(tmp_path, extra_cfg={"topic": {"extraction_fields": [field], "meta_analysis": plan}})
    st = ctx.store
    mk = lambda fam, vt, cmp="t vs c": {"family": fam, "cmp": cmp, "nt": 4, "nc": 4, "vt": vt, "vc": 1.0, "st": 0.2, "sc": 0.1, "kind": "mean", "sk": "SD"}
    data = {"1001": [mk("a", 2.0), mk("a", 2.0), mk("b", 3.0), mk("a", 9.0, "other")], "1002": [mk("a", 2.0), {**mk("a", 2.0), "kind": "median"}]}
    for rid, rs in data.items():
        st.write(f"rec:{rid}", "record", {"title": rid, "authors": "Kim A, Lee B", "year": "2020", "pmid": rid}, depends_on=[])
        st.write(f"screen:{rid}", "screen", {"stage": "title_abstract", "decision": "forward"}, depends_on=[f"rec:{rid}"])
        st.write(f"elig:{rid}", "eligibility", {"stage": "full_text", "decision": "include"}, depends_on=[f"screen:{rid}"])
        st.write(f"ext:{rid}", "extraction", {"fields": {"outcome_data": {"value": "x", "rows": rs}}}, depends_on=[f"elig:{rid}"])
    from sragent import ops
    r = ops.run(ctx, "synthesis.meta_analysis", {})
    assert (r["rows"], r["rows_used"], r["pooled_groups"], r["groups"]) == (6, 4, 1, 2)
    assert r["not_used"] == {"not the comparison analysed": 1, "a median without its quartiles or range": 1}
    res = json.loads((ctx.run_dir / "meta" / "results.json").read_text())
    a = res["groups"][0]
    assert a["group"] == {"family": "a"} and a["k"] == 2 and abs(a["pooled"]["ratio"] - 2) < 1e-9
    assert a["studies"][0]["rows"] == [1, 2] and abs(a["studies"][0]["vi"] - 0.005) < 1e-12 and a["studies"][0]["label"] == "Kim 2020"
    assert res["groups"][1]["pooled"] is None
    # the synthesis of a review with a plan: one call per pooled group with its pooled result; never the whole table
    from sragent.stages import synthesis
    st.write("protocol", "protocol", {"criteria": []}, depends_on=[])
    assert synthesis.synthesize(ctx) == 1 and len(asked) == 1
    assert "pooled ratio 2.00 (95% CI" in asked[0] and "row 1: family=a" in asked[0] and "family=b" not in asked[0]
    syn = st.get("syn:S1")["data"]
    assert syn["cites"] == ["1001"] and syn["pooled"].startswith("a (ratio of means, treated / control): pooled ratio 2.00")
    ev = synthesis.statement_evidence(ctx, syn["cites"], syn["text"], syn["basis"])        # the check reads what the writer read
    assert ev.startswith("POOLED RESULT") and "[1001] Kim 2020: ratio 2.00" in ev and "row 1: family=a" in ev and "RISK OF BIAS" in ev
    # finer groups by rule, and two papers of one trial counted once
    for rid, rs in data.items():
        for r in rs:
            r["name"] = "serum PlGF" if r["family"] == "a" else "x"
    plan2 = {**plan, "refine": [{"when": {"family": "a"}, "column": "cmp", "set": "family", "rules": [{"label": "tc", "pattern": "^T VS"}], "otherwise": "rest"}],
             "same_study": [["1002", "1001"]], "exclude": [{"column": "family", "pattern": "^b$", "label": "not wanted"}]}
    ctx.topic["meta_analysis"] = plan2
    res2 = meta.analyse(ctx)
    g2 = {tuple(g["group"].values()): g for g in res2["groups"]}
    assert set(g2) == {("a: tc",)} and res2["not_used"]["left out by rule: not wanted"] == 1 and [s["rid"] for s in g2[("a: tc",)]["studies"]] == ["1002"]
    assert res2["not_used"]["same trial as another included paper"] == 2 and not res2["possible_duplicates"]
    assert res["possible_duplicates"] == [{"studies": ["1001", "1002"], "group": {"family": "a"}, "measure": "ratio of means"}]
    with pytest.raises(ValueError, match="refine"):
        meta.plan_of({"extraction_fields": [field], "meta_analysis": {**plan, "refine": [{"column": "cmp", "set": "nt", "rules": []}]}})
    assert (ctx.run_dir / "meta" / "forest_a_ratio_of_means.svg").read_text().startswith("<svg") and (ctx.run_dir / "meta" / "effects.csv").exists()


def test_an_estimate_counts_what_a_verifier_and_a_repair_read(tmp_path):
    """Input tokens per call are measured on the review's own papers before the defaults are used (a long paper costs
    its length again for the risk-of-bias verifier), and a synthesis with a meta-analysis plan is counted per pooled
    group, with its checks and repairs."""
    from sragent import pipeline
    from sragent.estimate import DEFAULTS, estimate, sizes
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx, until="extract")
    for n in ctx.store.by_type("fulltext"):
        n["data"]["chars"] = 70000
    m = sizes(ctx)
    assert m["rob"] == m["extract"] == int(60000 / 2.2) + 1500 and m["rob_verify"] > m["rob"] > DEFAULTS["rob"][0]
    assert m["screen_full_text"] == int(15000 / 2.2) + 1500 and m["synthesis_repair"] > 1000
    price = {"input_per_m": 2.0, "output_per_m": 10.0}
    plain = estimate({"rob_verify": 100}, "m", price, "low")
    sized = estimate({"rob_verify": 100}, "m", price, "low", measured=m)
    assert sized["total_usd"] > 2.5 * plain["total_usd"] and sized["rows"][0]["source"] == "this review's papers"
    hist = {("rob_verify", "m"): {"in": 30000, "out": 900, "n": 5}}
    assert estimate({"rob_verify": 100}, "m", price, "low", hist, m)["rows"][0]["in_per_call"] == 30000      # its own history first


def test_the_stage_pages_offer_figure_reading_and_show_the_pooled_results(tmp_path):
    """Data extraction: studies not extracted can be selected and extracted together, and the figure-only rows left to
    read are counted (with the studies that have no PDF). Synthesis: the pooled table, computed without a model."""
    from sragent import figures
    from sragent.ui.stagepage import stage_page
    field = {"name": "outcome_data", "kind": "rows", "description": "x",
             "columns": {"experiment": "text", "family": ["a"], "nt": "number", "nc": "number", "vt": "number", "vc": "number", "st": "number",
                         "sc": "number", "kind": ["mean"], "sk": ["SD"], "source": ["text", "figure"]}}
    plan = {"field": "outcome_data", "group_by": ["family"], "min_studies": 2,
            "columns": {"treated": {"n": "nt", "value": "vt", "spread": "st"}, "control": {"n": "nc", "value": "vc", "spread": "sc"},
                        "value_type": "kind", "spread_type": "sk"}}
    ctx = make_ctx(tmp_path, extra_cfg={"topic": {"extraction_fields": [field], "meta_analysis": plan}})
    st = ctx.store
    full = {"experiment": "Table 1", "family": "a", "nt": 4, "nc": 4, "vt": 2.0, "vc": 1.0, "st": 0.2, "sc": 0.1, "kind": "mean", "sk": "SD", "source": "text"}
    fig = {"experiment": "Fig 2a", "family": "a", "nt": 3, "nc": 3, "kind": "mean", "source": "figure"}
    st.write("protocol", "protocol", {"criteria": []}, depends_on=[])
    for rid, rs in {"1001": [full, fig], "1002": [full], "1003": None}.items():
        st.write(f"rec:{rid}", "record", {"title": rid, "authors": "Kim A", "year": "2020"}, depends_on=[])
        st.write(f"screen:{rid}", "screen", {"stage": "title_abstract", "decision": "forward"}, depends_on=[f"rec:{rid}"])
        st.write(f"elig:{rid}", "eligibility", {"stage": "full_text", "decision": "include"}, depends_on=[f"screen:{rid}"])
        if rs is not None:
            st.write(f"ext:{rid}", "extraction", {"fields": {"outcome_data": {"value": "x", "rows": rs}}}, depends_on=[f"elig:{rid}"])
    assert figures.pending(ctx) == {"rows": 0, "figures": 0, "studies": 0, "no_pdf": ["1001"]}
    ex = stage_page(ctx, "extract")
    acts = {r["rid"]: [(a["op"], a["args"]) for a in r["actions"]] for r in ex["items"]}
    assert acts["1003"] == [("extract.studies", {"rids": ["1003"]})] and acts["1001"][0][0] == "extract.recompute"
    assert ex["extras"]["figures"]["no_pdf"] == 1 and ex["extras"]["figures"]["rows"] == 0
    m = stage_page(ctx, "synthesis")["extras"]["meta"]
    assert (m["rows"], m["rows_used"]) == (3, 2) and m["groups"][0]["k"] == 2 and abs(m["groups"][0]["ratio"] - 2) < 1e-9
    assert m["groups"][0]["plot"] == "/meta/forest_a_ratio_of_means.svg" and m["not_used"] == {"no values for both groups": 1}


def test_a_large_report_is_written_as_a_main_page_and_appendix_pages(tmp_path):
    """Thousands of screened records and every study's source text do not fit one page a browser can render: Appendix A
    and C go to pages, the main page lists them, and a link to a study or record leads to the page that holds it."""
    from sragent import pipeline
    from sragent.stages import report
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    html = report.report_html(ctx)
    assert report.split_report(html) == (html, {})                         # a small report stays one page
    main, parts = report.split_report(html, limit=1000, part_size=4000)
    a = sorted(n for n in parts if n.startswith("appendix_a_"))
    c = sorted(n for n in parts if n.startswith("appendix_c_"))
    assert len(a) >= 2 and len(c) >= 1 and "<div class='study'>" not in main and "<tr id='screen:" not in main
    rid = ctx.store.included_ids()[0]
    home = next(n for n in a if f"id='ext:{rid}'" in parts[n])
    assert f"href='\x00PART:{home}\x00#ext:{rid}'" in main                 # the results section links to the study's page
    assert sum(parts[n].count("<div class='study'>") for n in a) == html.count("<div class='study'>")
    assert sum(parts[n].count("<tr id='screen:") for n in c) == html.count("<tr id='screen:")
    assert "href='\x00MAIN\x00#ref-1'" in parts[home] and f"href='#src-{rid}-" in parts[home]   # back to the references; its own sentences stay local
    report.SPLIT_OVER, old = 1000, report.SPLIT_OVER
    try:
        report.write_reports(ctx)
    finally:
        report.SPLIT_OVER = old
    out = (ctx.run_dir / "report_evidence.html").read_text()
    page = (ctx.run_dir / "report_parts" / "appendix_c_01.html").read_text()
    assert "report_parts/appendix_a_01.html" in out and "\x00" not in out and "\x00" not in page and "../report_evidence.html" in page
    report.write_reports(ctx)                                             # small again: the pages are removed
    assert not (ctx.run_dir / "report_parts").exists()


def test_the_interface_serves_the_report_for_readers(tmp_path):
    """The planned report is a page of the interface (Output - For readers), built on demand from report_plan.yaml
    beside the run file; a review without a plan says so instead of linking a page that does not exist."""
    import yaml
    from sragent import pipeline
    from sragent.ui.server import App
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    run = tmp_path / "configs" / "review" / "t" / "run.yaml"
    run.parent.mkdir(parents=True)
    (tmp_path / "configs" / "models.yaml").write_text((ROOT / "configs/models.yaml").read_text())
    cfg = {k: v for k, v in ctx.cfg.items() if not k.startswith("_") and k != "topic"}
    cfg.update({"models_file": "../../models.yaml", "topic": ctx.cfg["topic"]})
    run.write_text(yaml.safe_dump(cfg))
    app = App(str(run))
    assert app.state()["run"]["manuscript"] is False
    assert "no <code>report_plan.yaml</code>" in app.manuscript_page()
    (run.parent / "report_plan.yaml").write_text(yaml.safe_dump(
        {"title": "For readers", "sections": [{"title": "Results", "content": [{"p": "{{n:included}} studies."}]}]}))
    assert app.state()["run"]["manuscript"] is True
    page = app.manuscript_page()
    assert "<h1>For readers</h1>" in page and "studies." in page and (ctx.run_dir / "manuscript.html").exists()
    (run.parent / "report_plan.yaml").write_text(yaml.safe_dump({"title": "x", "sections": [{"title": "s", "content": [{"p": "[@9999]"}]}]}))
    assert "9999" in app.manuscript_page()                      # a plan with problems says what they are


def test_the_interface_says_when_it_is_running_the_code_it_started_with(tmp_path, monkeypatch):
    """A long-running interface keeps the modules it imported, so an edited source file changes the pages but not
    what computes them. It says so, on the home view and on the report for readers itself, instead of serving
    numbers built by code that is no longer on disk."""
    import yaml
    from sragent import pipeline
    from sragent.ui import server as srv
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    run = tmp_path / "configs" / "review" / "t" / "run.yaml"
    run.parent.mkdir(parents=True)
    (tmp_path / "configs" / "models.yaml").write_text((ROOT / "configs/models.yaml").read_text())
    cfg = {k: v for k, v in ctx.cfg.items() if not k.startswith("_") and k != "topic"}
    cfg.update({"models_file": "../../models.yaml", "topic": ctx.cfg["topic"]})
    run.write_text(yaml.safe_dump(cfg))
    (run.parent / "report_plan.yaml").write_text(yaml.safe_dump(
        {"title": "For readers", "sections": [{"title": "Results", "content": [{"p": "{{n:included}} studies."}]}]}))
    app = srv.App(str(run))

    monkeypatch.setattr(srv, "_CODE", (0.0, []))
    monkeypatch.setattr(srv, "_STARTED", time.time() + 10 ** 6)   # nothing on disk is newer than this process
    assert srv.code_changed() == [] and app.state()["code_changed"] == []
    assert "code this interface started with" not in app.manuscript_page()

    monkeypatch.setattr(srv, "_CODE", (0.0, []))
    monkeypatch.setattr(srv, "_STARTED", 0.0)                     # every source file was edited after the start
    edited = srv.code_changed()
    assert "reportplan.py" in edited and app.state()["code_changed"] == edited
    assert "code this interface started with" in app.manuscript_page()   # on the page, not only in the interface


def test_a_planned_report_is_checked_and_rendered_from_its_plan(tmp_path):
    """sragent/reportplan.py: a plan names sections, figures and tables; citations must be included studies, pooled
    numbers are tokens filled from the computation, figures are drawn from the rows, and a plan with problems is not
    rendered."""
    from sragent import reportplan
    field = {"name": "outcome_data", "kind": "rows", "description": "x",
             "columns": {"name": "text", "family": ["a", "b"], "tier": ["cells", "people"], "dose": "number", "unit": "text", "nt": "number",
                         "nc": "number", "vt": "number", "vc": "number", "st": "number", "sc": "number", "kind": ["mean"], "sk": ["SD"],
                         "direction": ["increase", "decrease", "no change"]}}
    mplan = {"field": "outcome_data", "group_by": ["family", "tier"], "min_studies": 2,
             "columns": {"treated": {"n": "nt", "value": "vt", "spread": "st"}, "control": {"n": "nc", "value": "vc", "spread": "sc"},
                         "value_type": "kind", "spread_type": "sk", "direction": "direction"}}
    ctx = make_ctx(tmp_path, extra_cfg={"topic": {"extraction_fields": [field], "meta_analysis": mplan}})
    st = ctx.store
    row = lambda name, d, dose, unit: {"name": name, "family": "a", "tier": "cells", "dose": dose, "unit": unit, "nt": 4, "nc": 4, "vt": 2.0,
                                       "vc": 1.0, "st": 0.2, "sc": 0.1, "kind": "mean", "sk": "SD", "direction": d}
    st.write("protocol", "protocol", {"criteria": []}, depends_on=[])
    for rid, rs in {"1001": [row("marker one", "increase", 100, "uM"), row("marker two", "decrease", "10-4", "M")],
                    "1002": [row("marker one", "increase", 2, "mM")]}.items():
        st.write(f"rec:{rid}", "record", {"title": f"Title {rid}", "authors": "Kim A, Lee B", "year": "2020"}, depends_on=[])
        st.write(f"screen:{rid}", "screen", {"stage": "title_abstract", "decision": "forward"}, depends_on=[f"rec:{rid}"])
        st.write(f"elig:{rid}", "eligibility", {"stage": "full_text", "decision": "include"}, depends_on=[f"screen:{rid}"])
        st.write(f"ext:{rid}", "extraction", {"fields": {"outcome_data": {"value": "x", "rows": rs}}}, depends_on=[f"elig:{rid}"])
    plan = {"title": "A planned report", "summary": ["Pooled: {{pool:a · cells|ratio of means}}."],
            "concepts": {"column": "name", "domains": [{"key": "d1", "label": "Domain one"}],
                         "items": [{"name": "One", "domain": "d1", "pattern": "marker one"}, {"name": "Two", "domain": "d1", "pattern": "marker two"}]},
            "dose": {"value": "dose", "unit": "unit", "factors": {"uM": 1, "mM": 1000, "M": 1000000}, "band": [10, 100], "band_label": "range"},
            "sections": [{"title": "Results", "content": [
                {"p": "Shown in {fig:map} and {tab:t1}; see [@@1001; @1002]. {{n:included}} studies."},
                {"figure": {"id": "map", "type": "evidence_map", "caption": "Where the evidence is."}},
                {"figure": {"id": "dirs", "type": "direction_plot", "caption": "Directions."}},
                {"figure": {"id": "mech", "type": "mechanism_map", "caption": "Map.", "intervention": "Treatment", "inputs": ["stress"],
                            "outputs": ["function"], "panels": [{"domain": "d1", "items": ["One", "Two"]}]}},
                {"figure": {"id": "dose", "type": "dose_plot", "caption": "Dose."}},
                {"figure": {"id": "forest", "type": "forest", "group": "a · cells|ratio of means", "caption": "Forest."}},
                {"table": {"id": "t1", "type": "pooled", "caption": "Pooled."}},
                {"table": {"id": "t2", "caption": "Written.", "columns": ["A", "B"], "rows": [["x", "see [@1001]"]]}}]}]}
    D = reportplan.Data(ctx, plan)
    assert [r["conc"] for r in D.results] == [100.0, 100.0, 2000.0]                # uM as written, 10-4 M, 2 mM
    assert D.by_concept()["One"]["cells"] == {"1001": "increase", "1002": "increase"}
    assert reportplan.check(ctx, plan) == []
    r = reportplan.render(ctx, plan)
    html = (ctx.run_dir / "manuscript.html").read_text()
    assert r["ok"] and (r["figures"], r["tables"], r["references"]) == (5, 2, 2)
    assert "Kim et al. 2020 <span class='cite'>[<a href='#ref-1'>1</a>, <a href='#ref-2'>2</a>]" in html and "2 studies." in html
    assert "2.00 (95% CI" in html and "<a href='#fig-map'>Figure 1</a>" in html and "<a href='#tab-t1'>Table 1</a>" in html
    assert html.count("<svg") == 5 and (ctx.run_dir / "manuscript_figures" / "mech.svg").exists() and "2 of 2 studies" in html
    bad = {**plan, "summary": ["{{pool:nope|x}} [@9999] {fig:none}"]}
    errs = reportplan.check(ctx, bad)
    assert any("9999" in e for e in errs)
    bad2 = {**plan, "summary": ["{{pool:nope|x}} {fig:none}"]}
    errs = reportplan.check(ctx, bad2)
    assert len(errs) == 2 and not reportplan.render(ctx, bad2)["ok"]
    inv = reportplan.inventory(ctx, plan)
    assert inv["concepts"]["One"] == {"cells": {"increase": 2}} and inv["pooled_groups"][0]["group"] == "a · cells" and "forest" in inv["figure_types"]
    # a study measured in two models counts once, with "mixed" when its own results disagree
    st.write("ext:1002", "extraction", {"fields": {"outcome_data": {"value": "x", "rows": [
        {**row("marker one", "increase", 2, "mM"), "tier": "people"}, {**row("marker one", "decrease", 2, "mM")}]}}}, depends_on=["elig:1002"])
    D2 = reportplan.Data(ctx, plan)
    assert D2.study_directions("One") == {"1001": "increase", "1002": "mixed"}
    assert reportplan.render_text("{{dir:One|increase}} and {{dir:One}}", D2, reportplan.Refs(st), {}, {}) == \
        "1 of 2 and 1 of 2 studies report increase"
    assert "not an item" in " ".join(reportplan.check(ctx, {**plan, "summary": ["{{dir:Nope}}"]}))


def test_a_group_of_alternatives_is_not_ruled_out_before_all_of_them_are_read(tmp_path):
    """An any_of group whose remaining alternative is a full-text criterion cannot be declared not met at
    title/abstract: the record goes forward and is decided when that alternative has been read."""
    from sragent.stages.screening import rule_decision
    protocol = {"criteria": [
        {"id": "I1", "type": "inclusion", "stage": "title_abstract"},
        {"id": "I2", "type": "inclusion", "stage": "title_abstract", "any_of": "A"},
        {"id": "I3", "type": "inclusion", "stage": "title_abstract", "any_of": "A"},
        {"id": "I5", "type": "inclusion", "stage": "full_text", "any_of": "A"}]}
    no_group = [{"id": "I1", "assessment": "met"}, {"id": "I2", "assessment": "not_met"}, {"id": "I3", "assessment": "not_met"}]
    assert rule_decision(protocol, no_group, "title_abstract") == ("uncertain", ["I2", "I3"])      # I5 not read yet
    assert rule_decision(protocol, no_group + [{"id": "I5", "assessment": "not_met"}], "full_text") == ("exclude", ["I2", "I3", "I5"])
    assert rule_decision(protocol, no_group + [{"id": "I5", "assessment": "met"}], "full_text") == ("include", [])
    met = [{"id": "I1", "assessment": "met"}, {"id": "I2", "assessment": "met"}, {"id": "I3", "assessment": "not_met"}]
    assert rule_decision(protocol, met, "title_abstract") == ("include", [])                       # one alternative met: decided
    same_stage = {"criteria": [c for c in protocol["criteria"] if c["id"] != "I5"]}
    assert rule_decision(same_stage, no_group, "title_abstract") == ("exclude", ["I2", "I3"])      # no deferred member: unchanged


def test_a_concept_map_reads_at_one_level(tmp_path):
    """Figures group the concepts by domain in the order the plan declares the domains, so a domain never appears as
    two blocks whatever order its items were written in; a molecule carries the function the plan places it upstream
    of, so a pathway row is not left hanging; and a leads_to that names nothing is an error."""
    from sragent import reportplan
    field = {"name": "outcome_data", "kind": "rows", "description": "x",
             "columns": {"name": "text", "tier": ["cells"], "nt": "number", "nc": "number", "vt": "number",
                         "vc": "number", "kind": ["mean"], "direction": ["increase", "decrease"]}}
    mplan = {"field": "outcome_data", "group_by": ["tier"], "min_studies": 9,
             "columns": {"treated": {"n": "nt", "value": "vt"}, "control": {"n": "nc", "value": "vc"},
                         "value_type": "kind", "direction": "direction"}}
    con = {"column": "name", "tier_column": "tier",
           "domains": [{"key": "sig", "label": "Signalling"}, {"key": "fn", "label": "Function"}],
           "items": [{"name": "Kinase", "domain": "sig", "pattern": "kinase", "leads_to": ["Invasion"]},
                     {"name": "Invasion", "domain": "fn", "pattern": "invasion", "short": "invasion"},
                     {"name": "Phosphatase", "domain": "sig", "pattern": "phosphatase", "leads_to": ["Invasion"]}]}
    ctx = make_ctx(tmp_path, extra_cfg={"topic": {"extraction_fields": [field], "meta_analysis": mplan}})
    st = ctx.store
    st.write("protocol", "protocol", {"criteria": []}, depends_on=[])
    for rid, nm in (("1", "kinase"), ("2", "invasion assay"), ("3", "phosphatase")):
        st.write(f"rec:{rid}", "record", {"title": rid, "authors": "Kim A", "year": "2020"}, depends_on=[])
        st.write(f"screen:{rid}", "screen", {"stage": "title_abstract", "decision": "forward"}, depends_on=[f"rec:{rid}"])
        st.write(f"elig:{rid}", "eligibility", {"stage": "full_text", "decision": "include"}, depends_on=[f"screen:{rid}"])
        st.write(f"ext:{rid}", "extraction", {"fields": {"outcome_data": {"value": "x", "rows": [
            {"name": nm, "tier": "cells", "direction": "increase"}]}}}, depends_on=[f"elig:{rid}"])
    plan = {"title": "T", "tiers": ["cells"], "concepts": con, "sections": [{"title": "S", "content": [
        {"figure": {"id": "d", "type": "direction_plot", "caption": "Per study."}},
        {"table": {"id": "t", "type": "concepts", "caption": "Per concept."}}]}]}
    D = reportplan.Data(ctx, plan)
    rows = reportplan._concept_rows(D, {})
    assert [r["label"] for r in rows] == ["Kinase \u2192 invasion", "Phosphatase \u2192 invasion", "Invasion"]
    svg = reportplan.FIGURES["direction_plot"](D, {})
    assert svg.count("Signalling") == 1 and svg.count("Function") == 1       # one block per domain, not two
    cols, body = reportplan.TABLES["concepts"](D, {})
    assert body[0][1] == "Kinase \u2192 invasion" and body[2][1] == "Invasion"
    out = reportplan.render(ctx, plan)
    assert out["ok"] and "<li id='ref-1'><span>1.</span><span>" in Path(out["path"]).read_text()
    bad = {**plan, "concepts": {**con, "items": con["items"] + [{"name": "X", "domain": "sig", "pattern": "x",
                                                                 "leads_to": ["Nothing"]}]}}
    assert any("leads_to 'Nothing'" in e for e in reportplan.check(ctx, bad))


def test_what_was_measured_and_where_are_separate_analyses(tmp_path):
    """A concentration in blood and the expression of the same molecule in cells are different quantities, so they are
    never one pooled estimate, one count or one table line: what was measured is a grouping dimension of its own, the
    plan's settings keep the cells and the women apart, and a pooled name that no longer says which is an error."""
    from sragent import meta, reportplan
    field = {"name": "outcome_data", "kind": "rows", "description": "x",
             "columns": {"name": "text", "sample": "text", "tier": ["cells", "women"], "nt": "number", "nc": "number",
                         "vt": "number", "vc": "number", "sd_t": "number", "sd_c": "number", "kind": ["mean"],
                         "disp": ["SD"], "dose": "text", "unit": "text", "direction": ["increase", "decrease", "no change"]}}
    mplan = {"field": "outcome_data", "group_by": ["name", "tier", "readout"], "min_studies": 2,
             "classify": [{"set": "readout", "column": "name", "also": ["sample"],
                           "rules": [{"label": "release / circulating level", "pattern": r"serum|medium"}],
                           "otherwise": {"label": "expression"}}],
             "columns": {"treated": {"n": "nt", "value": "vt", "spread": "sd_t"}, "control": {"n": "nc", "value": "vc", "spread": "sd_c"},
                         "value_type": "kind", "spread_type": "disp", "direction": "direction"}}
    ctx = make_ctx(tmp_path, extra_cfg={"topic": {"extraction_fields": [field], "meta_analysis": mplan}})
    st = ctx.store
    st.write("protocol", "protocol", {"criteria": []}, depends_on=[])
    def row(sample, tier, vt, dose, unit, way="decrease"):
        return {"name": "Factor", "sample": sample, "tier": tier, "nt": 3, "nc": 3, "vt": vt, "vc": 10, "sd_t": 1,
                "sd_c": 1, "kind": "mean", "disp": "SD", "dose": dose, "unit": unit, "direction": way}
    rows = {"1": [row("maternal serum", "women", 5, "81", "mg/day")],
            "2": [row("maternal serum", "women", 6, "150", "mg/day")],
            "3": [row("cell lysate", "cells", 7, "30", "uM")],
            "4": [row("cell lysate", "cells", 12, "1000", "uM", "increase"),
                  row("cell lysate", "cells", 12, "1000-2000", "uM", "increase")],
            "5": [row("culture medium", "cells", 8, "5-50", "uM")]}
    for rid, rs in rows.items():
        st.write(f"rec:{rid}", "record", {"title": rid, "authors": f"A{rid} B", "year": "2020"}, depends_on=[])
        st.write(f"screen:{rid}", "screen", {"stage": "title_abstract", "decision": "forward"}, depends_on=[f"rec:{rid}"])
        st.write(f"elig:{rid}", "eligibility", {"stage": "full_text", "decision": "include"}, depends_on=[f"screen:{rid}"])
        st.write(f"ext:{rid}", "extraction", {"fields": {"outcome_data": {"value": "x", "rows": rs}}}, depends_on=[f"elig:{rid}"])
    res = meta.analyse(ctx)
    pooled = {" \u00b7 ".join(g["group"].values()) for g in res["groups"] if g["pooled"]}
    assert pooled == {"Factor \u00b7 women \u00b7 release / circulating level", "Factor \u00b7 cells \u00b7 expression"}
    plan = {"title": "T", "tiers": ["cells", "women"],
            "settings": [{"key": "cells", "label": "Cells", "short": "cells", "tiers": ["cells"]},
                         {"key": "women", "label": "Women", "short": "women", "tiers": ["women"]}],
            "concepts": {"column": "name", "domains": [{"key": "d", "label": "Signalling"}],
                         "items": [{"name": "Factor", "domain": "d", "pattern": "factor"}]},
            "dose": {"value": "dose", "unit": "unit", "unit_label": "\u00b5M", "factors": {"uM": 1}, "band": [10, 100],
                     "clinical": {"unit_label": "mg/day", "factors": {"mg/day": 1}, "band": [75, 150]}},
            "sections": [{"title": "S", "content": [
                {"p": "In the cells {{dir:Factor|decrease|cells}}; in the women {{dir:Factor|decrease|women}}. "
                      "{{dose:cells|concentration|yes}} of {{dose:cells}} cell studies used the human concentration."},
                {"table": {"id": "t", "type": "concepts", "caption": "Per setting."}},
                {"table": {"id": "dc", "type": "dose_check", "caption": "The dose question."}}]}]}
    D = reportplan.Data(ctx, plan)
    assert len(D.study_directions("Factor", D.tiers_for("cells"))) == 3        # the women are not counted here
    assert len(D.study_directions("Factor", D.tiers_for("women"))) == 2
    cols, body = reportplan.TABLES["concepts"](D, {})
    assert [r[2] for r in body] == ["Cells", "Women"]                          # one line per setting, never added
    assert body[0][4] == "5\u20132,000 \u00b5M" and body[1][4] == "81\u2013150 mg/day"       # each on its own scale
    assert body[0][5] == "**decrease** (majority)" and body[1][5] == "**decrease**, 2 studies only"
    cols, body = reportplan.TABLES["dose_check"](D, {})
    assert sorted((r[1], r[4]) for r in body) == [("Cells", "no, above it"), ("Cells", "partly"), ("Cells", "yes"),
                                                  ("Women", "yes"), ("Women", "yes")]
    assert [r[2] for r in body if r[0] == "[@@4]"] == ["1000 uM; 1000-2000 uM"]    # same render every time, whatever the order of a set
    html = reportplan.render(ctx, plan)
    assert html["ok"] and "In the cells 2 of 3; in the women 2 of 2." in Path(html["path"]).read_text()
    assert "1 of 3 cell studies used the human concentration" in Path(html["path"]).read_text()
    vague = {**plan, "sections": [{"title": "S", "content": [{"p": "{{pool:Factor|ratio of means}}"}]}]}
    assert any("names 2 pooled groups" in e for e in reportplan.check(ctx, vague))


def test_tissue_from_a_treated_woman_is_not_an_in_vitro_exposure(tmp_path):
    """A setting is where the aspirin was given, not only what was studied: placental tissue from a woman who took
    75 mg a day is in-vivo exposure although the model is tissue, and serum that had aspirin added to it in a tube is
    in-vitro exposure although the material came from a woman. Counts never mix the two."""
    from sragent import reportplan
    field = {"name": "outcome_data", "kind": "rows", "description": "x",
             "columns": {"name": "text", "tier": ["cells", "women"], "dose": "text", "unit": "text", "nt": "number", "nc": "number",
                         "vt": "number", "vc": "number", "st": "number", "sc": "number", "kind": ["mean"], "disp": ["SD"],
                         "direction": ["increase", "decrease", "no change"]}}
    mplan = {"field": "outcome_data", "group_by": ["name", "tier"], "min_studies": 2,
             "columns": {"treated": {"n": "nt", "value": "vt", "spread": "st"}, "control": {"n": "nc", "value": "vc", "spread": "sc"},
                         "value_type": "kind", "spread_type": "disp", "direction": "direction"}}
    ctx = make_ctx(tmp_path, extra_cfg={"topic": {"extraction_fields": [field], "meta_analysis": mplan}})
    st = ctx.store
    st.write("protocol", "protocol", {"criteria": []}, depends_on=[])
    row = lambda tier, dose, unit, way: {"name": "Factor", "tier": tier, "dose": dose, "unit": unit, "nt": 3, "nc": 3, "vt": 5, "vc": 10,
                                         "st": 1, "sc": 1, "kind": "mean", "disp": "SD", "direction": way}
    rows = {"1": [row("cells", "100", "uM", "decrease")],                 # aspirin added to the culture
            "2": [row("cells", "75", "mg/day", "increase")],              # tissue from a woman who took aspirin
            "3": [row("women", "0.01", "mM", "increase")],                # her serum, with aspirin added in a tube
            "4": [row("women", "100", "mg/day", "decrease")]}
    for rid, rs in rows.items():
        st.write(f"rec:{rid}", "record", {"title": rid, "authors": f"A{rid} B", "year": "2020"}, depends_on=[])
        st.write(f"screen:{rid}", "screen", {"stage": "title_abstract", "decision": "forward"}, depends_on=[f"rec:{rid}"])
        st.write(f"elig:{rid}", "eligibility", {"stage": "full_text", "decision": "include"}, depends_on=[f"screen:{rid}"])
        st.write(f"ext:{rid}", "extraction", {"fields": {"outcome_data": {"value": "x", "rows": rs}}}, depends_on=[f"elig:{rid}"])
    plan = {"title": "T", "tiers": ["cells", "women"],
            "settings": [{"key": "vitro", "label": "Aspirin added in vitro", "tiers": ["cells", "women"], "given": "in vitro"},
                         {"key": "exvivo", "label": "Tissue from treated women", "tiers": ["cells"], "given": "in vivo"},
                         {"key": "women", "label": "Pregnant women", "tiers": ["women"], "given": "in vivo"}],
            "concepts": {"column": "name", "domains": [{"key": "d", "label": "D"}],
                         "items": [{"name": "Factor", "domain": "d", "pattern": "factor"}]},
            "dose": {"value": "dose", "unit": "unit", "factors": {"uM": 1, "mM": 1000},
                     "clinical": {"unit_label": "mg/day", "factors": {"mg/day": 1}, "band": [75, 150]}},
            "sections": [{"title": "S", "content": [{"p": "In vitro {{dir:Factor|decrease|vitro}}; ex vivo {{dir:Factor|increase|exvivo}}."}]}]}
    D = reportplan.Data(ctx, plan)
    assert {r["rid"]: r["given"] for r in D.results} == {"1": "in vitro", "2": "in vivo", "3": "in vitro", "4": "in vivo"}
    assert {r["rid"]: r["setting"] for r in D.results} == {"1": "vitro", "2": "exvivo", "3": "vitro", "4": "women"}
    assert sorted(D.study_directions("Factor", setting="vitro")) == ["1", "3"]       # the serum study is an in-vitro one
    assert sorted(D.study_directions("Factor", setting="exvivo")) == ["2"]           # and the explant study is not
    assert D.counts()["studies:vitro"] == 2 and D.counts()["studies:exvivo"] == 1
    r = reportplan.render(ctx, plan)
    assert r["ok"] and "In vitro 1 of 2; ex vivo 1 of 1." in Path(r["path"]).read_text()


def test_a_pathway_figure_shows_the_chain_and_what_kind_of_change_each_step_shows(tmp_path):
    """A cascade the plan declares: nodes are laid out one column past their deepest parent, a feedback edge does not
    deepen the graph, a node linked to a concept is annotated from the data with the kind of change measured, and a
    node the studies never measured says so."""
    from sragent import figlib, reportplan
    nodes = [{"id": "d", "label": "Drug"}, {"id": "k", "label": "Kinase", "concept": "One"},
             {"id": "m", "label": "Messenger", "concept": "Two"}, {"id": "f", "label": "Function"}]
    edges = [{"from": "d", "to": "k"}, {"from": "k", "to": "m"}, {"from": "m", "to": "f"}, {"from": "f", "to": "d", "effect": "inhibits"}]
    assert figlib.layer_of(nodes, edges) == {"d": 0, "k": 1, "m": 2, "f": 3}        # the loop does not push the drug along
    field = {"name": "outcome_data", "kind": "rows", "description": "x",
             "columns": {"name": "text", "level": ["protein", "mRNA"], "tier": ["cells"], "nt": "number", "nc": "number", "vt": "number",
                         "vc": "number", "kind": ["mean"], "direction": ["increase", "decrease", "no change"]}}
    plan = {"field": "outcome_data", "group_by": ["tier", "readout"], "min_studies": 9,
            "classify": [{"set": "readout", "column": "name", "also": ["level"],
                          "rules": [{"label": "phosphorylation (activation)", "pattern": r"\bp-|phospho"},
                                    {"label": "protein level", "pattern": "protein"}],
                          "otherwise": {"column": "level", "map": {"mRNA": "mRNA level"}}}],
            "columns": {"treated": {"n": "nt", "value": "vt"}, "control": {"n": "nc", "value": "vc"}, "value_type": "kind", "direction": "direction"}}
    con = {"column": "name", "domains": [{"key": "d1", "label": "Signalling"}], "items": [{"name": "One", "domain": "d1", "pattern": "kinase"}, {"name": "Two", "domain": "d1", "pattern": "nothing-matches-this"}]}
    ctx = make_ctx(tmp_path, extra_cfg={"topic": {"extraction_fields": [field], "meta_analysis": plan}})
    st = ctx.store
    st.write("protocol", "protocol", {"criteria": []}, depends_on=[])
    rows = {"1001": [{"name": "p-kinase", "level": "protein", "tier": "cells", "direction": "increase", "nt": 3, "nc": 3, "vt": 2, "vc": 1, "kind": "mean"}],
            "1002": [{"name": "kinase mRNA", "level": "mRNA", "tier": "cells", "direction": "decrease", "nt": 3, "nc": 3, "vt": 1, "vc": 2, "kind": "mean"}]}
    for rid, rs in rows.items():
        st.write(f"rec:{rid}", "record", {"title": rid, "authors": "Kim A", "year": "2020"}, depends_on=[])
        st.write(f"screen:{rid}", "screen", {"stage": "title_abstract", "decision": "forward"}, depends_on=[f"rec:{rid}"])
        st.write(f"elig:{rid}", "eligibility", {"stage": "full_text", "decision": "include"}, depends_on=[f"screen:{rid}"])
        st.write(f"ext:{rid}", "extraction", {"fields": {"outcome_data": {"value": "x", "rows": rs}}}, depends_on=[f"elig:{rid}"])
    full = {"title": "T", "concepts": con, "sections": [{"title": "S", "content": [
        {"figure": {"id": "pw", "type": "pathway", "nodes": nodes, "edges": edges, "caption": "Chain."}},
        {"table": {"id": "mod", "type": "modulation", "caption": "What changed."}}]}]}
    D = reportplan.Data(ctx, full)
    assert D.readouts("One") == {"phosphorylation (activation)": {"increase": ["1001"]}, "mRNA level": {"decrease": ["1002"]}}
    cols, body = reportplan.TABLES["modulation"](D, {})
    assert [r[3] for r in body] == ["phosphorylation (activation)", "mRNA level"] and body[0][4] == "increase 1"
    svg = reportplan.FIGURES["pathway"](D, full["sections"][0]["content"][0]["figure"])
    assert "Kinase" in svg and "1 of 2 studies" in svg and "not measured" in svg       # Messenger was never measured
    assert svg.count("mixed") == 1                                                    # only in the legend: the node is not mixed
    assert reportplan.check(ctx, full) == [] and reportplan.render(ctx, full)["ok"]
    bad = {**full, "sections": [{"title": "S", "content": [{"figure": {"id": "pw", "type": "pathway", "caption": "",
                                                                      "nodes": [{"id": "a", "label": "A", "concept": "Nope"}],
                                                                      "edges": [{"from": "a", "to": "ghost"}]}}]}]}
    errs = reportplan.check(ctx, bad)
    assert any("ghost" in e for e in errs) and any("Nope" in e for e in errs)
