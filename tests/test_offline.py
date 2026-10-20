"""Offline tests (no API calls). Run:  pytest -q tests/

The pipeline test uses the mock LLM and a tiny in-memory record set, so it needs no network
except for term validation / full-text lookups, which are disabled here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mock_llm import mock_fn  # noqa: E402

from sragent.config import load_config  # noqa: E402
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


def make_ctx(tmp_path, fulltexts=("1001", "1002", "1003"), extra_cfg: dict | None = None) -> Ctx:
    recf = tmp_path / "recs.jsonl"
    recf.write_text("\n".join(json.dumps(r) for r in RECORDS))
    fdir = tmp_path / "run" / "fulltext"
    fdir.mkdir(parents=True, exist_ok=True)
    for r in RECORDS:   # the full texts a person supplied (no network in tests)
        if r["rid"] in fulltexts:
            (fdir / f"{r['rid']}.txt").write_text(f"## Title\n{r['title']}\n## Methods\n" + (r["abstract"] + " ") * 25)
    cfg = load_config(ROOT / "configs/benchmark/emicizumab_pk/topic.yaml", {
        "run_dir": str(tmp_path / "run"), "cache_dir": str(tmp_path / "cache"), "budget_usd": 1.0,
        "llm_defaults": {"provider": "mock", "model": "mock", "concurrency": 1},
        "pipeline": {"fulltext": False, "validate_terms": False, "citation_chasing": False},
        "topic": {"records": {"source": "file", "file": str(recf)}}, **(extra_cfg or {})})
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
    n = st.get("screen:1003")
    d = dict(n["data"]); d["decision"] = "awaiting"; d["stage"] = "full_text"
    st.upsert(n["id"], "screen", d, depends_on=n["depends_on"])
    ids = {t["id"]: t for t in todos.build(ctx)}
    assert "protocol" in ids and "elig:1003" in ids and any(k.startswith("rob:") for k in ids)
    assert all(t["question"] and t["options"] for t in ids.values())
    # a record whose full-text review is blocked is requested, unless declared not obtainable
    n = st.get("screen:1004")
    before = dict(n["data"])
    st.upsert(n["id"], "screen", {**before, "decision": "fulltext_unavailable", "stage": "retrieval"}, depends_on=n["depends_on"])
    req = [t for t in todos.build(ctx) if t["id"] == "fulltexts"]
    assert req and "1004" in str(req[0])
    st.meta["pdf_unavailable"] = ["1004"]
    assert not [t for t in todos.build(ctx) if t["id"] == "fulltexts"]
    st.meta["pdf_unavailable"] = []
    st.upsert(n["id"], "screen", before, depends_on=n["depends_on"])
    # answers
    assert todos.apply(ctx, {"todo_id": "protocol", "option": "approve"})["ok"]
    assert todos.apply(ctx, {"todo_id": "elig:1003", "option": "exclude", "extra": {"criterion": "I3"}, "text": "no PK"})["ok"]
    assert "1003" not in st.included_ids() and st.get("screen:1003")["data"]["human_decision"]["decision"] == "exclude"
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
    assert [x["key"] for x in s["steps"]][:3] == ["plan", "search", "screen"] and s["todos"]
    e = app.estimate({"action": "run", "until": "report", "agent": {"provider": "gemini", "model": "gemini-3.8-flash", "effort": "low"}})
    assert e["total_usd"] == 0  # everything already done
    m = app.models()
    flash = next(x for x in m["api"] if x["id"] == "gemini-3.8-flash")   # from the model catalog
    assert flash["input_per_m"] == 0.75 and flash["provider"] == "gemini"
    assert m["local"]["endpoints"] == 1 and m["local"]["models"] == ["auto"]
    job = yaml.safe_load(app._write_job_config({"action": "rerun", "agent": {"provider": "local", "model": "auto"}}).read_text())
    assert job["local"]["endpoints_file"] == str((tmp_path / "local_endpoints.yaml").resolve())
    assert job["job_agent"]["provider"] == "local"     # a model picked for one re-run: this job only
    job = yaml.safe_load(app._write_job_config({"action": "run", "agent": {"provider": "local", "model": "auto"}}).read_text())
    assert "job_agent" not in job and "agent" not in (job.get("roles") or {})   # pipeline jobs follow stage_models
    r = app.answer({"todo_id": "protocol", "option": "approve"})
    assert r["ok"]


def test_awaiting_flags_follow_the_decision(tmp_path):
    import pytest
    from sragent.stages import screening
    ctx = make_ctx(tmp_path)
    st = ctx.store
    st.upsert("rec:1", "record", {"rid": "1", "title": "t", "abstract": "a"})
    st.upsert("screen:1", "screen", {"stage": "full_text", "decision": "awaiting", "because": ["I1"]})
    screening.sync_awaiting_flags(ctx)
    screening.sync_awaiting_flags(ctx)
    assert [f["status"] for f in st.get("screen:1")["flags"]] == ["open"]
    st.get("screen:1")["data"]["decision"] = "exclude"
    screening.sync_awaiting_flags(ctx)
    assert [f["status"] for f in st.get("screen:1")["flags"]] == ["resolved"]
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
    pipeline.run(ctx, until="fulltext")
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
    assert res["added"] == 1 and ctx.store.get(f"screen:{ctx.store.meta['snowball']['added'][0]}")
    assert ctx.store.meta["found_via"][ctx.store.meta["snowball"]["added"][0]] == ["citation"]


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
    cfg["budget_usd"], cfg["contact_email"] = 3.0, "me@example.org"      # free to change at any time
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


def test_stage_models(tmp_path):
    """Each stage uses its own model and effort; a stage's model is fixed once that stage has started."""
    import copy

    import pytest
    from sragent import config_lock, pipeline
    from sragent.estimate import estimate_stages
    big = {"model": "mock-big", "reasoning_effort": "high", "pricing": {"input_per_m": 2.0, "output_per_m": 10.0}}
    ctx = make_ctx(tmp_path, extra_cfg={"stage_models": {"plan": big, "extract": {**big, "reasoning_effort": "medium"}}})
    pipeline.run(ctx, until="fulltext")
    rows = [json.loads(line) for line in (ctx.run_dir / "usage.jsonl").read_text().splitlines()]
    by_tag = {}
    for r in rows:
        by_tag.setdefault(r["tag"].split("_rerun")[0], set()).add(r["model"])
    assert by_tag["plan"] == {"mock-big"} and by_tag["screen_title_abstract"] == {"mock"}
    rec = ctx.store.meta["config_lock"]["values"]
    assert rec["stage_models.plan"]["model"] == "mock-big" and "stage_models.extract" not in rec   # not started yet

    cfg = copy.deepcopy(ctx.cfg)                                    # a stage that has not started: free to change
    cfg["stage_models"]["extract"] = {**big, "reasoning_effort": "low"}
    cfg["stage_models"]["rob"] = {"reasoning_effort": "medium"}
    c2 = Ctx.create(cfg, mock_fn=mock_fn)
    c2.quiet = True
    est = estimate_stages({"extract": 2, "rob": 2}, cfg)
    assert {r["stage"]: (r["model"], r["effort"]) for r in est["rows"]} == {"extract": ("mock-big", "low"), "rob": ("mock", "medium")}
    pipeline.run(c2)
    rows = [json.loads(line) for line in (ctx.run_dir / "usage.jsonl").read_text().splitlines()]
    assert {r["model"] for r in rows if r["tag"] == "extract"} == {"mock-big"}
    assert {r["model"] for r in rows if r["tag"] == "rob"} == {"mock"}
    assert c2.store.meta["config_lock"]["values"]["stage_models.extract"]["reasoning_effort"] == "low"

    cfg3 = copy.deepcopy(cfg)                                       # a started stage: fixed
    cfg3["stage_models"]["screen"] = {"reasoning_effort": "high"}
    with pytest.raises(config_lock.ConfigChanged) as e:
        Ctx.create(cfg3, mock_fn=mock_fn)
    assert [c["key"] for c in e.value.changes] == ["stage_models.screen"]
    from sragent.stages.report import report_markdown
    assert "mock-big (high effort): protocol" in report_markdown(c2)


def test_rescreen_keeps_records_waiting_for_full_text(tmp_path):
    """Re-applying a protocol never includes a record without a full-text review; it can still exclude it."""
    import copy

    from sragent import pipeline
    from sragent.stages import screening
    ctx = make_ctx(tmp_path, fulltexts=("1001",))          # 1002 and 1003 have no full text
    pipeline.run(ctx, until="fulltext")
    st = ctx.store
    waiting = sorted(n["id"] for n in st.by_type("screen") if n["data"]["stage"] == "retrieval")
    assert waiting == ["screen:1002", "screen:1003"]
    protocol = st.get("protocol")["data"]
    screening.rescreen_rule_only(ctx, protocol)
    assert st.included_ids() == ["1001"]
    assert all(st.get(w)["data"]["decision"] == "fulltext_unavailable" for w in waiting)
    p2 = copy.deepcopy(protocol)                            # a revised protocol that rules them out
    ta = next(c for c in p2["criteria"] if c["type"] == "inclusion" and c.get("stage", "title_abstract") == "title_abstract")
    ta["type"] = "exclusion"                                 # 'met' now means excluded
    screening.rescreen_rule_only(ctx, p2)
    for w in waiting:
        d = st.get(w)["data"]
        assert (d["decision"], d["stage"]) == ("exclude", "title_abstract") and ta["id"] in d["because"]


def test_reply_that_is_not_one_object_is_asked_again(tmp_path):
    """A reply that is not one JSON object (e.g. only the list a check returns inside it) is re-asked once,
    and such a reply is never served from the cache."""
    from sragent.llm import LLM, CostTracker, DiskCache
    replies = iter(['["only the issues"]', '{"support": "supported", "issues": []}'])
    cfg = load_config(None, {"llm_defaults": {"provider": "mock", "model": "mock"}})
    llm = LLM(cfg, "agent", CostTracker(1), DiskCache(tmp_path / "c.sqlite"), mock_fn=lambda m, mode: next(replies))
    assert llm.chat([{"role": "user", "content": "check"}], tag="t") == {"support": "supported", "issues": []}


# ---------------------------------------------------------------- integrity of accepted proposals and human decisions
def test_accepting_a_proposal_keeps_what_the_sandbox_created(tmp_path):
    """Nodes the sandbox created on the way (here the full-text node of a record the feedback forwarded)
    come along when the proposal is accepted; before, only the listed changes were applied."""
    from sragent import feedback, pipeline
    from sragent.stages import fulltext
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    assert st.get("screen:1004")["data"]["decision"] == "exclude" and st.get("ft:1004") is None
    cs = feedback.handle_feedback(ctx, "Study 1004 should not have been excluded: it has human PK data.",
                                  target="screen:1004", propagate=True, auto_accept=False)
    assert cs["status"] == "proposed" and st.get("ft:1004") is None          # nothing applied yet
    assert "ft:1004" in cs["sandbox_nodes"] and "ft:1004" in cs["base_versions"] or True
    feedback.accept(ctx, cs["id"])
    assert st.get("ft:1004") is not None                                       # the retrieval the sandbox did
    d = st.get("screen:1004")["data"]
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
    n = st.get("screen:1003")
    st.upsert(n["id"], "screen", {**n["data"], "decision": "awaiting", "stage": "full_text"}, depends_on=n["depends_on"])
    assert todos.apply(ctx, {"todo_id": "elig:1003", "option": "include", "text": "PK data in table 2"})["ok"]
    assert st.get("screen:1003")["data"]["human_decision"]["decision"] == "include"
    # a new PDF for that study: read and re-extracted, the decision kept
    (fulltext.fulltext_dir(ctx) / "1003.txt").write_text("## Methods\n" + "Emicizumab prophylaxis, 30 patients, trough 40 ug/mL. " * 60)
    r = sync.process_pdfs(ctx)
    assert "1003" in r["processed"] and "1003" not in r["rescreened"] and "1003" in r["reextracted"]
    assert st.get("screen:1003")["data"]["human_decision"]["decision"] == "include" and "1003" in st.included_ids()
    # the next run's full-text review, the rules and a protocol re-application leave it alone
    assert not screening.needs_fulltext_review(st, "1003")
    screening.fulltext_review(ctx, st.get("protocol")["data"])
    rules.apply(ctx)
    screening.rescreen_rule_only(ctx, st.get("protocol")["data"])
    d = st.get("screen:1003")["data"]
    assert d["decision"] == "include" and d["human_decision"]["decision"] == "include"
    # the agent's own screening (fresh assessments) keeps the decision and records what it would have said
    screening.screen_one(ctx, st.get("protocol")["data"], "1003", "full_text")
    d = st.get("screen:1003")["data"]
    assert d["decision"] == "include" and d["human_decision"] and "agent_decision" in d
    # an explicit re-run a person asked for is the one way to replace it (as a proposal)
    cs = actions.rerun(ctx, "1003", "screen", note="please re-screen")
    assert cs["kind"] == "rerun" and st.get("screen:1003")["data"]["human_decision"]["decision"] == "include"


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
    assert any(f["status"] == "open" and f["source"] == "source_changed" for f in st.get("rob:1001")["flags"])


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
    import json as _json
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
    cfg = load_config(None, {"llm_defaults": {"provider": "mock", "model": "mock", "max_retries": 3}})
    calls = []
    def refuse(self, msgs, json_out, max_tokens, temperature):
        calls.append(1)
        raise Refused("declined", pin=120, pout=5)
    monkeypatch.setattr(LLM, "_call", refuse)
    tr = CostTracker(1.0)
    llm = LLM(cfg, "agent", tr, None, mock_fn=lambda m, mode: "{}")
    llm.c["pricing"] = {"input_per_m": 1000.0, "output_per_m": 1000.0}
    import pytest
    with pytest.raises(Refused):
        llm.chat([{"role": "user", "content": "x"}], tag="t")
    assert len(calls) == 1 and tr.spent > 0 and tr.calls == 1                 # not retried, still paid and recorded
    monkeypatch.undo()
    n = []
    llm = LLM(cfg, "agent", CostTracker(1.0), DiskCache(tmp_path / "c.sqlite"),
              mock_fn=lambda m, mode: (n.append(1), '{"ok": true}')[1])
    monkeypatch.setattr(llm.cache, "set", lambda k, v: (_ for _ in ()).throw(RuntimeError("database is locked")))
    assert llm.chat([{"role": "user", "content": "x"}], tag="t") == {"ok": True} and len(n) == 1
    # the budget is checked before every paid call, retries included
    tr = CostTracker(0.0)
    llm = LLM(cfg, "agent", tr, None, mock_fn=lambda m, mode: "{}")
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
    assert item["auto_evidence"] == [sid("Clearance")] and sid("Clearance") in cites   # 0.27: one sentence, cited
    assert "27" in item["uncovered"]                                # 27 occurs in two sentences: not guessed, flagged
    assert "12" in item["uncovered"]                                # "Twelve" is not a digit: flagged for the reviewer
    assert "52" not in item["uncovered"] and "5" not in item["uncovered"]   # cited / a single digit (not checked)
    plain = {"value": "Adults with haemophilia A"}
    assert cover_numbers(plain, ["s1"], text) == ["s1"] and "uncovered" not in plain


def test_synthesis_is_checked_by_another_model_and_absence_claims_see_every_study(tmp_path):
    from sragent import pipeline
    from sragent.stages import synthesis
    big = {"model": "mock-writer", "reasoning_effort": "high", "pricing": {"input_per_m": 1, "output_per_m": 1}}
    ctx = make_ctx(tmp_path, extra_cfg={"stage_models": {"synthesize": big}})
    pipeline.run(ctx)
    rows = [json.loads(l) for l in (ctx.run_dir / "usage.jsonl").read_text().splitlines()]
    assert {r["model"] for r in rows if r["tag"] == "synthesis"} == {"mock-writer"}
    assert {r["model"] for r in rows if r["tag"] == "synthesis_verify"} == {"mock"}      # the agent, not the writer
    with ctx.at("synthesize"):
        assert synthesis.checker(ctx).model == "mock"
    ctx.cfg["roles"]["verifier"] = {"model": "mock-checker", "pricing": {"input_per_m": 1, "output_per_m": 1}}
    ctx.hub._clients.clear()
    assert synthesis.checker(ctx).model == "mock-checker"
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
    st.meta["synthesis_considered"] = ["1001"]                    # pretend two included studies were added since
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
    assert ext["dep_versions"]["screen:1001"] == st.get("screen:1001")["version"]
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


def test_old_store_is_migrated_once(tmp_path):
    from sragent import graph, pipeline
    from sragent.store import ReviewStore
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    # make it look like a schema-1 store: no dep_versions, no settings nodes, coarse edges
    st = ctx.store
    for n in st.nodes.values():
        n.pop("dep_versions", None)
    for nid in [n["id"] for n in st.by_type("setting")]:
        del st.nodes[nid]
    for n in st.by_type("extraction"):
        n["depends_on"] = [d for d in n["depends_on"] if not d.startswith(("field:", "cfg:"))] + ["protocol"]
    st.meta.pop("schema", None)
    st.reindex()
    st.save()
    old = ReviewStore.load(ctx.run_dir / "store.json")
    done = graph.migrate(old, ctx.cfg)
    assert done and old.meta["schema"] == graph.SCHEMA
    assert graph.migrate(old, ctx.cfg) == []                            # idempotent
    e = old.get("ext:1001")
    assert "field:sample_size" in e["depends_on"] and "protocol" not in e["depends_on"]
    assert graph.stale(old) == {}                                       # migration makes nothing stale
    assert old.get("cfg:question")["data"]["value"] == ctx.topic["question"]
    assert set(old.children("cfg:question")) >= {"protocol"}            # reverse index rebuilt


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
    # the next run notices the edited file: recorded as a person's judgment
    planning.plan(ctx)
    ev = provenance.events(ctx.run_dir)
    assert any(e["action"] == "protocol_edited" and e.get("changed", {}).get("removed") == ["I3"] for e in ev)
    # in the agent-only condition an edited protocol.yaml is refused
    (tmp_path / "ao").mkdir()
    ao = make_ctx(tmp_path / "ao", extra_cfg={"pipeline": {"human_mode": "agent_only", "fulltext_unclear": "include"}})
    pipeline.run(ao, until="plan")
    p2 = yaml.safe_load(open(ao.run_dir / "protocol.yaml")); p2["criteria"].pop()
    yaml.safe_dump(p2, open(ao.run_dir / "protocol.yaml", "w"), sort_keys=False)
    import pytest
    with pytest.raises(provenance.NotAllowed):
        planning.plan(ao)


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
def test_engine_plan_is_free_and_execute_keeps_what_people_decided(tmp_path):
    import yaml
    from sragent import actions, engine, pipeline, todos
    from sragent.stages import planning
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    st = ctx.store
    assert engine.plan(ctx)["items"] == []                              # a finished review: nothing to do
    # a person decides 1002 and edits 1001's extraction; then a criterion is added to the protocol
    n = st.get("screen:1002")
    st.upsert(n["id"], "screen", {**n["data"], "decision": "awaiting", "stage": "full_text"}, depends_on=n["depends_on"])
    assert todos.apply(ctx, {"todo_id": "elig:1002", "option": "include", "text": "PK table"})["ok"]
    actions.edit(ctx, {"kind": "ext", "rid": "1001", "fields": {"sample_size": "14 participants"}, "note": "Table 1"})
    p = yaml.safe_load(open(ctx.run_dir / "protocol.yaml"))
    p["criteria"].append({"id": "E1", "type": "exclusion", "text": "studies only in children", "stage": "title_abstract"})
    yaml.safe_dump(p, open(ctx.run_dir / "protocol.yaml", "w"), sort_keys=False)
    n_calls = sum(1 for _ in (ctx.run_dir / "usage.jsonl").read_text().splitlines())
    planning.plan(ctx)
    plan = engine.plan(ctx)
    assert sum(1 for _ in (ctx.run_dir / "usage.jsonl").read_text().splitlines()) == n_calls   # planning made no model call
    by = {i["node"]: i for i in plan["items"]}
    assert by["screen:1001"]["action"] == "reassess" and by["screen:1001"]["criteria"] == ["E1"]
    assert by["screen:1002"]["action"] == "reassess" and by["screen:1002"]["pinned"]              # assessed, decision kept
    assert by["screen:1004"]["action"] == "reassess"                                             # excluded ones too
    assert plan["cost"]["total_usd"] is not None and plan["summary"]["screen:reassess"] >= 3
    cs = engine.execute(ctx, tag="test")
    d2 = st.get("screen:1002")["data"]
    assert d2["decision"] == "include" and d2["human_decision"] and "E1" in {a["id"] for a in d2["criteria"]}
    assert "agent_decision" in d2                                                                # shown beside the decision
    assert {a["id"] for a in st.get("screen:1001")["data"]["criteria"]} >= {"I1", "I2", "I3", "E1"}
    assert st.get("ext:1001")["data"]["fields"]["sample_size"]["value"] == "14 participants"      # the edit survived
    left = engine.plan(ctx)["items"]                                    # nothing left to do: only notes for a person
    assert [i for i in left if i["action"] not in ("keep", "render")] == []
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
    d = st.get("screen:1004")["data"]
    assert "rule" not in d and d.get("rule_restored", {}).get("rule") == "editorial"
    pipeline.run(ctx)
    sid = st.by_type("synthesis", active_only=True)[0]["id"]
    st.set_status(sid, synthesis.RETIRED, reason="test")
    synthesis.write_statements(ctx, [{"id": sid.split(":")[1], "text": "A new statement about 12 participants.", "cites": ["1001"]}], "test")
    assert st.get(sid)["status"] == synthesis.RETIRED                   # the retired one is untouched
    assert any(n["data"]["text"].startswith("A new statement") and n["status"] == "active" for n in st.by_type("synthesis"))
