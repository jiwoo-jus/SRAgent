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
from sragent.evidence import verify_quote  # noqa: E402
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


def make_ctx(tmp_path) -> Ctx:
    recf = tmp_path / "recs.jsonl"
    recf.write_text("\n".join(json.dumps(r) for r in RECORDS))
    cfg = load_config(ROOT / "configs/topics/emicizumab_pk.yaml", {
        "run_dir": str(tmp_path / "run"), "cache_dir": str(tmp_path / "cache"), "budget_usd": 1.0,
        "llm_defaults": {"provider": "mock", "model": "mock", "concurrency": 1},
        "pipeline": {"fulltext": False, "validate_terms": False},
        "topic": {"records": {"source": "file", "file": str(recf)}}})
    ctx = Ctx.create(cfg, mock_fn=mock_fn)
    ctx.quiet = True
    return ctx


def test_parse_json_variants():
    assert parse_json('<think>x</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": {"b": [1,2]}} trailing') == {"a": {"b": [1, 2]}}


def test_sentence_evidence():
    from sragent.evidence import numbered, resolve, segment
    src = ("Title\n\nMETHODS: Emicizumab was given to 12 participants. Samples were taken weekly.\n"
           "RESULTS: Trough concentrations were 52 ug/mL. Half-life was 27 days.")
    segs = segment(src)
    assert [s["sid"] for s in segs][:2] == ["s1", "s2"] and segs[-1]["section"] == "Results"
    assert "[s3]" in numbered(src)
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


def test_pipeline_milestone(tmp_path):
    from sragent import pipeline
    ctx = make_ctx(tmp_path)
    pipeline.run(ctx)
    assert set(ctx.store.included_ids()) == {"1001", "1002", "1003"}
    assert (ctx.run_dir / "store.json").exists()
    calls = ctx.hub.tracker.calls
    pipeline.run(ctx)
    assert ctx.hub.tracker.calls == calls
    assert not ctx.store.by_type("extraction")
