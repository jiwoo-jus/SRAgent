"""Phase 0 of the refactor (docs/refactor/04-refactor-plan.md §8): every operation, pinned before it moves.

The refactor gives each thing a person or the agent can do one operation (04 §4). Today many of them are reached
through several entry points (a To-do answer, a bulk save, a study-page edit, a CLI command) that do not do quite the
same thing. This module freezes what each entry point does now, one scenario per entry point, filed under the
operation it will become. A scenario records:

  result        what the call returned (ok / message / job it asks for)
  effects       what it did besides the review content: lines in human_decisions.jsonl, provenance actions, flags
                opened and closed (node, source), proposals saved, model calls (by tag), store meta keys changed,
                and what the update engine now considers out of date (node, action)
  review        the semantic view of the review afterwards (tests/test_golden.py:view)

When a phase moves an entry point onto its operation, the snapshot shows exactly which effects change; a change
that removes a known inconsistency (03-inconsistencies.md, ids in KNOWN) is expected and reviewed, anything else is
a regression. Snapshots are in tests/golden/ops/; after an intended change:

    SRAGENT_UPDATE_GOLDEN=1 pytest -q tests/test_operations.py      and review the diff of tests/golden/ops/

OPS lists every operation of 04 §4 with how it is covered today: scenarios here, an existing golden scenario
("golden:<name>"), an existing offline test ("offline:<test>"), or why it cannot be yet ("new: ..." for an operation
that does not exist today, "gap: ..." for one that needs a service the offline tests do not have).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_golden import FULLTEXTS, check, cs_view, reviewed, view  # noqa: E402

# G03 with a full text that leaves the PK criterion unclear: it waits for a person (eligibility to-do)
AWAITING = {**FULLTEXTS, "G03": "Emicizumab exposure was described in 140 patients; bleeding rates fell."}


# ---------------------------------------------------------------- what an operation did
def _lines(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


# review state that lived in meta until phase 2b (sragent/state.py)
STATE_TYPES = ("approval", "strategy", "cohort", "synthesis_scope", "snowball", "pdf_request", "rules_run")


def _snapshot(ctx) -> dict:
    from sragent import provenance
    st = ctx.store
    fb = ctx.run_dir / "feedback"
    return {"hd": len(_lines(ctx.run_dir / "human_decisions.jsonl")),
            "prov": len(provenance.events(ctx.run_dir)),
            "usage": len(_lines(ctx.run_dir / "usage.jsonl")),
            "flags": {(n["id"], f["id"]): (f.get("source"), f["status"]) for n in st.nodes.values() for f in n["flags"]},
            "notes": {f"{x['node']} [{x['source']}]" for x in __import__("sragent.attention", fromlist=["x"]).notes(st)},
            "meta": json.loads(json.dumps(st.meta, default=str)),
            "state": {n["id"]: n["version"] for n in st.nodes.values() if n["type"] in STATE_TYPES},
            "proposals": {p.name for p in fb.glob("*.json")} if fb.exists() else set()}


def _normal(v, tmp):
    """Results without paths, ids that change per run, or timestamps."""
    if isinstance(v, dict):
        return {k: _normal(x, tmp) for k, x in v.items()
                if k not in ("ts", "id", "created", "seconds", "cost_usd", "updated", "changeset")}
    if isinstance(v, (list, tuple)):
        return [_normal(x, tmp) for x in v]
    if isinstance(v, str):
        import re
        return re.sub(r"\b(RR|FB|UP)\d+\b", "<proposal>", v.replace(str(tmp), "<tmp>"))
    return v


def effects(ctx, before: dict, tmp) -> dict:
    from sragent import engine, provenance
    st = ctx.store
    after = _snapshot(ctx)
    hd = _lines(ctx.run_dir / "human_decisions.jsonl")[before["hd"]:]
    prov = provenance.events(ctx.run_dir)[before["prov"]:]
    usage = _lines(ctx.run_dir / "usage.jsonl")[before["usage"]:]
    opened = sorted(f"{nid} [{src}]" for (nid, fid), (src, s) in after["flags"].items()
                    if s == "open" and before["flags"].get((nid, fid), (None, None))[1] != "open")
    closed = sorted(f"{nid} [{src}]" for (nid, fid), (src, s) in after["flags"].items()
                    if s != "open" and before["flags"].get((nid, fid), (None, None))[1] == "open")
    meta = sorted(k for k in set(before["meta"]) | set(after["meta"]) if before["meta"].get(k) != after["meta"].get(k))
    fb = ctx.run_dir / "feedback"
    new_cs = [json.loads((fb / n).read_text()) for n in sorted(after["proposals"] - before["proposals"])]
    plan = engine.plan(ctx) if st.get("protocol") else {"items": []}
    return {"decisions_log": [f"{x.get('todo_id', '').split(':', 1)[0]}:{x.get('option')} ({x.get('status')})" for x in hd],
            "provenance": sorted(f"{e['category']}:{e['action']}" for e in prov),
            "flags_opened": opened, "flags_closed": closed, "meta_changed": meta,
            "state_nodes": sorted(k for k, v in after["state"].items() if before["state"].get(k) != v),
            "notes_appeared": sorted(after["notes"] - before["notes"]), "notes_cleared": sorted(before["notes"] - after["notes"]),
            "proposals": [cs_view(c) for c in new_cs],
            "model_calls": dict(sorted(Counter(u.get("tag") for u in usage).items())),
            "out_of_date": sorted(f"{i['node']} {i['action']}" for i in plan["items"] if i["action"] not in ("render",))}


def run(name: str, ctx, tmp, fn) -> None:
    from sragent.graph import edge_problems
    before = _snapshot(ctx)
    res = fn(ctx)
    bad = {n["id"]: p for n in ctx.store.nodes.values() if (p := edge_problems(n))}
    assert not bad, f"{name}: written edges differ from graph.TYPES: {dict(list(bad.items())[:5])}"
    if isinstance(res, dict) and res.get("changes") and isinstance(res["changes"][0], dict):
        res = cs_view(res)                                  # a change set (proposal or applied)
    check(f"ops/{name}", {"result": _normal(res, tmp), "effects": effects(ctx, before, tmp), "review": view(ctx)})


@pytest.fixture
def offline_services(monkeypatch):
    """PubMed counts and term look-ups answered locally (the offline tests have no network)."""
    from sragent.stages import extraction, search
    monkeypatch.setattr(search.pubmed, "esearch", lambda q, retmax=0, maxdate=None, **k: {"count": len(q), "translation": q})
    monkeypatch.setattr(extraction, "validate_field_terms",
                        lambda value, kind, entities=None, known=None: [{"term": value.split()[0], "status": "validated", "source": "test"}])


def _cli(monkeypatch, ctx, cmd: str, **args):
    from sragent import cli
    monkeypatch.setattr(cli, "_ctx", lambda a, **k: ctx)
    getattr(cli, cmd)(argparse.Namespace(**args))
    return None


def _edit_protocol(ctx, change):
    import yaml
    p = yaml.safe_load(open(ctx.run_dir / "protocol.yaml"))
    change(p)
    (ctx.run_dir / "protocol.yaml").write_text(yaml.safe_dump(p, sort_keys=False))


def _add_e1(p):
    p["criteria"].append({"id": "E1", "type": "exclusion", "text": "studies only in children", "stage": "title_abstract"})


# ---------------------------------------------------------------- scenarios: name -> (operation, setup, call)
# setup: "reviewed" (the golden review), "awaiting" (G03 waits for a person), "planned" (stopped after the protocol),
# "searched" (stopped before the search)
def _todo(tid, opt, text="", extra=None):
    from sragent import todos
    return lambda c: todos.apply(c, {"todo_id": tid, "option": opt, "text": text, "extra": extra or {}})


def _bulk(kind, ids=(), note="", items=None):
    from sragent import todos
    return lambda c: {"ok": True, "count": todos.bulk(c, kind, list(ids), note, items)}


def _edit(**b):
    from sragent import actions
    return lambda c: actions.edit(c, b)


class _flag_then:
    """Setup: an open flag on `node`; the operation answers it."""
    def __init__(self, node, text, source, then):
        self.node, self.text, self.source, self.then = node, text, source, then


def _protocol_revise(c):
    from sragent import ops
    return ops.run(c, "protocol.revise", {"instruction": "widen I2"})


def _protocol_edit_criteria(c):
    from sragent import ops
    return ops.run(c, "protocol.edit_criteria", {
        "criteria": [{"id": "I1", "text": "about emicizumab (edited)", "type": "inclusion", "stage": "title_abstract"},
                     {"id": "E9", "text": "Study is a conference abstract.", "type": "exclusion", "stage": "title_abstract"}],
        "note": "by hand"})




def _pipeline(until):
    def f(c):
        from sragent import pipeline
        pipeline.run(c, until=until)
        return None
    return f


def _search(op, **kw):
    def f(c):
        from sragent.stages import search
        protocol = c.store.get("protocol")["data"]
        with c.at("search"):
            if op == "suggest":
                m = search.suggest_queries(c, protocol)
                return {"ok": True, "count": len(m["variants"])}
            if op == "refine":
                return {"ok": True, **search.refine_query(c, protocol, kw["query"], kw["instruction"])}
            if op == "confirm":
                return {"ok": True, **search.confirm_query(c, kw["query"], note="chosen")}
            if op == "count":
                return {"ok": True, **search.count(c, kw["query"])}
            if op == "clear":   # inline in App.search_strategy today
                search.strategy_meta(c.store)["confirmed"] = None
                return {"ok": True}
    return f


def _rerun(rid, step, note=""):
    from sragent import actions
    return lambda c: actions.rerun(c, rid, step, note=note)


def _feedback(text, target):
    from sragent import feedback
    return lambda c: feedback.handle_feedback(c, text, target=target, auto_accept=False)


def _fn(path, *a, **k):
    def f(c):
        import importlib
        mod, name = path.rsplit(".", 1)
        return getattr(importlib.import_module(mod), name)(c, *a, **k)
    return f


def _proto_fn(path, **k):
    def f(c):
        import importlib
        mod, name = path.rsplit(".", 1)
        return getattr(importlib.import_module(mod), name)(c, c.store.get("protocol")["data"], **k)
    return f


def _with_pdf(rid, body):
    def f(c):
        from sragent import sync
        from sragent.stages import fulltext
        (fulltext.fulltext_dir(c) / f"{rid}.txt").write_text("## Methods\n" + (body + " ") * 30)
        return sync.process_pdfs(c)
    return f


G03_PDF = "Emicizumab pharmacokinetics in 140 patients: trough concentrations were 48 ug/mL."

SCENARIOS = {
    # protocol
    "protocol.draft@pipeline": ("protocol.draft", "empty", _pipeline("plan")),
    "protocol.approve@todo": ("protocol.approve", "planned", _todo("protocol", "approve", "looks right")),
    "protocol.edit_criteria@page": ("protocol.edit_criteria", "reviewed", _protocol_edit_criteria),
    "protocol.revise@page": ("protocol.revise", "reviewed", _protocol_revise),
    "protocol.import_file@todo_edited": ("protocol.import_file", "reviewed", "file_then_todo"),
    "protocol.import_file@next_run": ("protocol.import_file", "reviewed", "file_then_run"),
    "protocol.import_file@cli_rescreen": ("protocol.import_file", "reviewed", "cli_rescreen"),
    # search
    "search.suggest_variants@page": ("search.suggest_variants", "planned", _search("suggest")),
    "search.refine@page": ("search.refine", "planned", _search("refine", query="emicizumab[tiab]", instruction="exclude reviews")),
    "search.count@page": ("search.count", "planned", _search("count", query="emicizumab[tiab] AND pharmacokinetics")),
    "search.confirm_strategy@page": ("search.confirm_strategy", "planned", _search("confirm", query="emicizumab[tiab]")),
    "search.clear_strategy@page": ("search.clear_strategy", "planned", _search("clear")),
    "search.run@pipeline": ("search.run", "planned", _pipeline("search")),
    # screen (title/abstract)
    "screen.restore_rule_exclusion@bulk": ("screen.restore_rule_exclusion", "reviewed", _bulk("restore_rule", ["G07"], note="it is a full paper")),
    "screen.recompute@rerun": ("screen.recompute", "reviewed", _rerun("G05", "screen", "check I1 again")),
    "screen.verify@cli": ("screen.verify", "reviewed", _proto_fn("sragent.stages.screening.verify_existing", mode="all")),
    "screen.rederive@rescreen_rule_only": ("screen.rederive", "reviewed", _proto_fn("sragent.stages.screening.rescreen_rule_only")),
    "screen.apply_rules@rules": ("screen.apply_rules", "reviewed", _fn("sragent.rules.apply")),
    # retrieve
    "retrieve.request@todo_elig_pdf": ("retrieve.request", "awaiting", _todo("elig:G03", "pdf")),
    "retrieve.declare_unobtainable@todo_fulltexts": ("retrieve.declare_unobtainable", "reviewed", _todo("fulltexts", "process", extra={"unavailable": "G03"})),
    "retrieve.declare_unobtainable@bulk": ("retrieve.declare_unobtainable", "reviewed", _bulk("pdf_unavailable", ["G03"])),
    "retrieve.decide_without@bulk": ("retrieve.decide_without", "reviewed", _bulk("pdf_unwant", ["G03"])),
    "retrieve.process_added_files@todo": ("retrieve.process_added_files", "reviewed", _todo("fulltexts", "process")),
    "retrieve.process_added_files@pdfs": ("retrieve.process_added_files", "reviewed", _with_pdf("G03", G03_PDF)),
    "retrieve.fetch@cli_fetch_files": ("retrieve.fetch", "reviewed", _fn("sragent.stages.fulltext.backfill_fetched_files")),
    # eligibility (full text)
    "eligibility.decide@todo_include": ("eligibility.decide", "awaiting", _todo("elig:G03", "include", "PK in table 2")),
    "eligibility.decide@todo_exclude": ("eligibility.decide", "awaiting", _todo("elig:G03", "exclude", "no drug levels", {"criterion": "I3"})),
    "eligibility.decide@todo_include_without_fulltext": ("eligibility.decide", "reviewed", _todo("elig:G03", "include")),
    "eligibility.decide@bulk": ("eligibility.decide", "awaiting", _bulk("eligibility", items=[{"rid": "G03", "option": "include", "text": "PK in table 2"}])),
    "eligibility.decide@study_page": ("eligibility.decide", "awaiting", _edit(kind="screen", rid="G03", decision="include", note="PK in table 2")),
    "eligibility.decide@study_page_awaiting": ("eligibility.decide", "reviewed", _edit(kind="screen", rid="G01", decision="awaiting", note="not sure")),
    "eligibility.overturn@study_page": ("eligibility.overturn", "reviewed", _edit(kind="screen", rid="G02", decision="exclude", criterion="I2", note="paediatric only")),
    "eligibility.recompute@rerun": ("eligibility.recompute", "reviewed", _rerun("G01", "screen")),
    "eligibility.verify@cli": ("eligibility.verify", "awaiting", _proto_fn("sragent.stages.screening.verify_existing", mode="pending")),
    # extraction
    "extract.edit_values@study_page": ("extract.edit_values", "reviewed", _edit(kind="ext", rid="G01", fields={"sample_size": "14 participants"}, note="Table 1")),
    "extract.revalidate_terms@cli": ("extract.revalidate_terms", "reviewed", _fn("sragent.stages.extraction.revalidate_terms")),
    # risk of bias
    "rob.confirm@todo": ("rob.confirm", "reviewed", _todo("rob:G01", "confirm", "agreed")),
    "rob.confirm@bulk": ("rob.confirm", "reviewed", _bulk("rob_confirm", ["rob:G01", "rob:G02"], note="all fine")),
    "rob.edit_domains@todo_change_one": ("rob.edit_domains", "reviewed", _todo("rob:G01", "change", "randomisation unclear", {"domain": "missing_data", "judgment": "high"})),
    "rob.edit_domains@todo_change_many": ("rob.edit_domains", "reviewed", _todo("rob:G01", "change", "two domains", {"changes": [{"domain": "missing_data", "judgment": "high"}, {"domain": "pk_analysis", "judgment": "some_concerns"}]})),
    "rob.edit_domains@study_page": ("rob.edit_domains", "reviewed", _edit(kind="rob", rid="G01", changes=[{"domain": "missing_data", "judgment": "high"}], note="dropouts")),
    "rob.recompute@rerun": ("rob.recompute", "reviewed", _rerun("G01", "rob", "look at attrition")),
    "rob.recompute@todo_fix": ("rob.recompute", "reviewed", _todo("rob:G01", "fix", "re-check attrition")),
    "rob.recompute@todo_fix_job": ("rob.recompute", "reviewed", lambda c: __import__("sragent.ops", fromlist=["x"]).run(
        c, "rob.recompute", {"rid": "G01", "note": "re-check attrition"})),
    "rob.verify@cli": ("rob.verify", "reviewed", _fn("sragent.stages.rob.verify_existing")),
    "rob.redraft_all@cli": ("rob.redraft_all", "reviewed", _fn("sragent.stages.rob.reassess_all")),
    # synthesis
    "synthesis.edit_statement@study_page": ("synthesis.edit_statement", "reviewed", "syn_edit"),
    "synthesis.resolve_flag@todo_ok": ("synthesis.resolve_flag", "reviewed",
                                       _flag_then("syn:S1", "Check this wording.", "check", lambda fid: _todo(f"flag:syn:S1:{fid}", "ok", "fine"))),
    "synthesis.resolve_flag@todo_fix": ("synthesis.resolve_flag", "reviewed",
                                        _flag_then("syn:S1", "Check this wording.", "check", lambda fid: _todo(f"flag:syn:S1:{fid}", "fix", "say adults"))),
    "synthesis.revise@feedback": ("synthesis.revise", "reviewed", _feedback("Say that the data are from adults only.", "syn:S1")),
    # report
    "report.render@write_reports": ("report.render", "reviewed", _fn("sragent.stages.report.write_reports")),
    # cross-stage
    "comment@save": ("comment", "reviewed", "comment"),
    "comment@resolve": ("comment", "reviewed", "comment_resolve"),
    "proposal.accept@todo": ("proposal.accept", "reviewed", "proposal_accept_todo"),
    "proposal.reject@todo": ("proposal.reject", "reviewed", "proposal_reject_todo"),
    "propagate_edit@todo_ok": ("propagate_edit", "reviewed", "edited_ok"),
    "propagate_edit@todo_update": ("propagate_edit", "reviewed", "edited_update"),
    "feedback@todo_clarify": ("feedback", "reviewed", "clarify"),
    "feedback@todo_insist": ("feedback", "reviewed",
                             _flag_then("ext:G01", "Feedback: sample size is 999 -> agent: the source says 12", "feedback",
                                        lambda fid: _todo(f"disagree:ext:G01:{fid}", "insist", "it is 999"))),
    "update_all@todo_sync": ("update_all", "reviewed", _todo("sync", "sync")),
    "update_all@sync_after_protocol": ("update_all", "reviewed", "sync_after_protocol"),
    "preview@impact": ("preview", "reviewed", lambda c: {"ok": True, "message": json.dumps(__import__("sragent.engine", fromlist=["x"]).preview(c, "ext:G01"), sort_keys=True, default=str)}),
    "snooze@todo_later": ("snooze", "awaiting", _todo("elig:G03", "later")),
}


def _special(name, ctx, monkeypatch):
    """Scenarios that need a step before the operation: the step runs here, the returned call is the operation."""
    from sragent import actions, feedback, sync, todos
    if name == "cli_rescreen":
        _edit_protocol(ctx, _add_e1)
        return lambda c: _cli(monkeypatch, c, "cmd_rescreen")
    if isinstance(name, _flag_then):
        fl = ctx.store.flag(name.node, name.text, actor="agent", source=name.source)
        return name.then(fl["id"])
    if name == "file_then_todo":
        _edit_protocol(ctx, _add_e1)
        return _todo("protocol", "edited")
    if name == "file_then_run":
        from sragent.stages import planning
        _edit_protocol(ctx, _add_e1)
        return lambda c: (planning.plan(c), None)[1]
    if name == "syn_edit":
        d = ctx.store.get("syn:S1")["data"]
        return _edit(kind="syn", node="syn:S1", text=d["text"] + " (adults only)", cites=d["cites"], note="scope")
    if name == "comment":
        from sragent import ops
        return lambda c: ops.run(c, "comment", {"target": "study:G01", "text": "check the dosing"})
    if name == "comment_resolve":
        from sragent import ops
        cid = ops.run(ctx, "comment", {"target": "study:G01", "text": "check the dosing"})["comment"]["id"]
        return lambda c: ops.run(c, "comment.resolve", {"id": cid, "note": "done"})
    if name in ("proposal_accept_todo", "proposal_reject_todo"):
        cs = feedback.handle_feedback(ctx, "Population of G02 is wrong", target="ext:G02", auto_accept=False)
        accept = name.endswith("accept_todo")
        return _todo(f"cs:{cs['id']}", "accept" if accept else "reject", "ok" if accept else "keep it")
    if name in ("edited_ok", "edited_update"):
        actions.edit(ctx, {"kind": "ext", "rid": "G01", "fields": {"sample_size": "14 participants"}, "note": "Table 1"})
        from sragent import todos
        assert any(t["id"] == "edited:ext:G01" for t in todos.build(ctx))     # derived from the out-of-date dependents
        return _todo("edited:ext:G01", "ok" if name == "edited_ok" else "update")
    if name == "clarify":
        cs = feedback.handle_feedback(ctx, "this is wrong", target=None, auto_accept=False)
        return _todo(f"clarify:{cs['id']}", "answer", "the sample size of G01")
    if name == "sync_after_protocol":
        _edit_protocol(ctx, _add_e1)
        todos.apply(ctx, {"todo_id": "protocol", "option": "edited"})
        return sync.sync_dependents
    raise KeyError(name)


def _setup(kind, tmp_path):
    from test_golden import build
    if kind == "empty":
        return build(tmp_path)
    if kind in ("planned", "searched"):
        from sragent import pipeline
        ctx = build(tmp_path)
        pipeline.run(ctx, until="protocol")
        return ctx
    return reviewed(tmp_path, AWAITING if kind == "awaiting" else FULLTEXTS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_operation(name, tmp_path, monkeypatch, offline_services):
    op, setup, call = SCENARIOS[name]
    ctx = _setup(setup, tmp_path)
    fn = call if callable(call) else _special(call, ctx, monkeypatch)
    run(name, ctx, tmp_path, fn)


# ---------------------------------------------------------------- every operation of 04 §4 is accounted for
OPS = {
    # protocol
    "protocol.approve": "ops", "protocol.edit_criteria": "ops", "protocol.import_file": "ops",
    "protocol.draft": "ops", "protocol.revise": "ops",
    "protocol.export_file": "op: no scenario (writes protocol.yaml)",
    # search
    "search.confirm_strategy": "ops", "search.clear_strategy": "ops",
    "search.draft_strategy": "ops:search.suggest_variants@page (the agent's own query V0 is drafted there)",
    "search.suggest_variants": "ops", "search.refine": "ops", "search.count": "ops", "search.run": "ops",
    # screen
    "screen.restore_rule_exclusion": "ops", "screen.run": "golden:pipeline", "screen.recompute": "ops",
    "screen.verify": "ops", "screen.rederive": "ops", "screen.apply_rules": "ops",
    "screen.clear": "offline:test_clear_keeps_what_people_decided_and_the_next_run_redoes_the_rest",
    # retrieve
    "retrieve.declare_unobtainable": "ops", "retrieve.decide_without": "ops", "retrieve.request": "ops",
    "retrieve.fetch": "ops", "retrieve.process_added_files": "ops", "retrieve.clear": "gap: not offered: full texts are files (ops/stage.py NOT_CLEARABLE)",
    # eligibility
    "eligibility.decide": "ops", "eligibility.overturn": "ops", "eligibility.run": "golden:pipeline",
    "eligibility.recompute": "ops", "eligibility.verify": "ops",
    "eligibility.rederive": "ops:screen.rederive@rescreen_rule_only (one function for both stages today)",
    "eligibility.clear": "offline:test_clear_keeps_what_people_decided_and_the_next_run_redoes_the_rest",
    # snowball
    "snowball.run": "gap: needs PubMed citation links; a local citation source comes with phase 3",
    "snowball.clear": "gap: not offered: clear screening instead (ops/stage.py NOT_CLEARABLE)",
    # extraction
    "extract.edit_values": "ops", "extract.run": "golden:pipeline", "extract.recompute": "golden:rerun_extraction",
    "extract.revalidate_terms": "ops", "extract.clear": "offline:test_clear_keeps_what_people_decided_and_the_next_run_redoes_the_rest",
    # risk of bias
    "rob.confirm": "ops", "rob.edit_domains": "ops", "rob.run": "golden:pipeline", "rob.recompute": "ops",
    "rob.verify": "ops", "rob.redraft_all": "ops", "rob.clear": "offline:test_clear_keeps_what_people_decided_and_the_next_run_redoes_the_rest",
    # synthesis
    "synthesis.edit_statement": "ops", "synthesis.resolve_flag": "ops", "synthesis.write": "golden:pipeline",
    "synthesis.update": "golden:person_excludes_then_sync", "synthesis.revise": "ops",
    "synthesis.clear": "offline:test_clear_keeps_what_people_decided_and_the_next_run_redoes_the_rest",
    # report
    "report.render": "ops", "report.export": "new: phase 4 (today the report files are rewritten in place)",
    # cross-stage
    "comment": "ops", "proposal.accept": "ops", "proposal.reject": "ops", "propagate_edit": "ops",
    "settings.change": "offline:test_data_items_changed_in_the_interface_at_any_stage",
    "models.set": "gap: writes the run file in configs/ (App.set_stage_models); moves into the store in phase 5",
    "feedback": "ops", "update_all": "ops", "preview": "ops", "snooze": "ops",
    "stop": "gap: a job subprocess (App.stop); characterized when jobs move to operations in phase 3",
    "cancel": "gap: a queued job (App.cancel); characterized when jobs move to operations in phase 3",
}

# known inconsistencies these snapshots freeze on purpose (03-inconsistencies.md): fixing one changes its snapshot
KNOWN = {
    "eligibility.decide@todo_include vs @study_page": "I01: different flags closed, report files, log ids",
    "eligibility.decide@bulk": "I02: 'N saved' counts refused items",
    "protocol.* (five entry points)": "I08: revision history, actor, provenance name and decision log differ",
    "rob.confirm / rob.edit_domains": "I05: three write paths; the To-do 'Change' path logs twice",
    "rob.recompute@rerun / @todo_fix / redraft_all": "I06: three triggers, three mechanisms, three model sources",
    "decisions_log of every human entry point": "I19: the logged content depends on the entry point",
    "retrieve.process_added_files@pdfs": "I41 fixed (update_policy); the snapshot is after that fix",
}


def test_the_package_writes_through_write_or_annotate():
    """store.write (a computation from inputs) or store.annotate (a decision, an edit, a note): never the bare upsert,
    which would mark a person's decision current with inputs they never saw (03 I61)."""
    import re
    root = Path(__file__).resolve().parents[1] / "sragent"
    calls = [f"{p.relative_to(root)}:{i}" for p in root.rglob("*.py") if p.name != "store.py"
             for i, line in enumerate(p.read_text().splitlines(), 1) if re.search(r"\.upsert\(", line)]
    assert not calls, f"bare upsert calls: {calls}"


def test_edge_checker_catches_mismatches():
    from sragent.graph import edge_problems as e
    assert e({"id": "screen:G1", "type": "screen", "depends_on": ["rec:G1", "protocol", "cfg:x"]}) == ["undeclared cfg:x"]
    assert e({"id": "ext:G1", "type": "extraction", "depends_on": ["elig:G1", "cfg:question"]}) == ["missing ft:<rid>|rec:<rid>"]
    assert e({"id": "ext:G1", "type": "extraction", "depends_on": ["elig:G2", "rec:G1", "cfg:question"]}) == \
        ["missing elig:<rid>", "undeclared elig:G2"]
    assert e({"id": "x", "type": "nope", "depends_on": []}) == ["type nope is not declared in graph.TYPES"]


def test_a_confirmation_does_not_make_an_output_current(tmp_path):
    """Confirming a risk-of-bias draft whose extraction changed since it was drafted keeps it out of date (kept as
    confirmed, flagged for the person), instead of marking it current with an extraction it never read."""
    from sragent import actions, engine, graph, todos
    ctx = reviewed(tmp_path)
    actions.edit(ctx, {"kind": "ext", "rid": "G01", "fields": {"sample_size": "14 participants"}, "note": "Table 1"})
    assert graph.stale_reasons(ctx.store, "rob:G01")
    assert todos.apply(ctx, {"todo_id": "rob:G01", "option": "confirm", "text": "fine"})["ok"]
    assert graph.stale_reasons(ctx.store, "rob:G01")                     # still out of date ...
    item = next(i for i in engine.plan(ctx)["items"] if i["node"] == "rob:G01")
    assert item["action"] == "kept" and item["pinned"]                   # ... and kept, because a person confirmed it


# meta keys that are presentation, history or job bookkeeping, not review state (sragent/state.py)
META_ALLOWED = {"snoozed", "comments", "config_lock", "config_changes", "protocol_revisions", "flag_seq", "schema",
                "rob_reassess", "gold_input", "verified_extraction_input"}


def test_review_state_is_in_nodes_not_meta():
    """No code reads or writes review state in store.meta outside the documented presentation and bookkeeping keys."""
    import re
    root = Path(__file__).resolve().parents[1] / "sragent"
    used = {}
    for p in root.rglob("*.py"):
        if p.name == "state.py":                       # the migration from meta
            continue
        for k in re.findall(r"meta(?:\.get|\.setdefault|\.pop)?[\(\[]\s*[\"'](\w+)[\"']", p.read_text()):
            used.setdefault(k, []).append(p.name)
    assert set(used) <= META_ALLOWED, {k: v for k, v in used.items() if k not in META_ALLOWED}


def test_an_older_review_is_migrated_with_a_backup(tmp_path):
    """A review saved before phase 2b (side notes in meta, schema 2) opens with its notes moved into nodes, its old
    store kept beside it, and the same meaning: the approval holds, the scope, requests and rules are where they were."""
    from sragent import state
    from sragent.context import Ctx
    from test_golden import golden_mock
    ctx = reviewed(tmp_path, AWAITING)
    st = ctx.store
    state.approve(st, "ok")
    approved_v = st.get("protocol")["version"]
    # turn the nodes back into the meta of an older store
    m = st.meta
    m["protocol_approved"] = {"ts": 1.0, "note": "ok"}
    m["search_strategy"] = {"variants": [], "chat": [], "confirmed": {"query": "emicizumab[tiab]", "count": 9}}
    m["synthesis_considered"] = state.considered(st)
    m["synthesis_written_with"] = {"included": 2, "not_retrieved": 0, "when": "x"}
    m["pdf_wanted"], m["pdf_unavailable"] = {"G03": True}, ["G05"]
    m["rules_applied"] = state.rules_applied(st)
    m["waiting_for_pdfs"] = 1
    for nid in [n["id"] for n in st.nodes.values() if n["type"] in STATE_TYPES]:
        del st.nodes[nid]
    for n in st.by_type("screen"):
        n["depends_on"] = [d for d in n["depends_on"] if d != "cfg:screening_rules"]
    m["schema"] = 2
    st.reindex()
    ctx.save()
    c2 = Ctx.create(ctx.cfg, mock_fn=golden_mock)
    s2 = c2.store
    from sragent import graph as _g
    assert (ctx.run_dir / "store.schema2.bak.json").exists() and s2.meta["schema"] == _g.SCHEMA
    assert not set(s2.meta) - META_ALLOWED
    assert state.approval(s2) and s2.get("protocol")["version"] == approved_v
    assert state.confirmed_query(s2)["query"] == "emicizumab[tiab]"
    assert state.considered(s2) == ["G01", "G02"] and s2.get(state.SCOPE)["data"]["written_with"]["included"] == 2
    assert state.wanted(s2) == {"G03"} and state.unavailable(s2) == {"G05"}
    assert state.rules_applied(s2)["rules"] and "cfg:screening_rules" in s2.get("screen:G07")["depends_on"]
    from sragent import graph
    assert not graph.stale(s2), graph.stale(s2)         # the migration itself makes nothing out of date


def test_one_screen_node_per_study_is_split_on_load(tmp_path):
    """A review saved when one screen node held both stages (schema 3) opens with the full-text decision on
    elig:<rid> and the title/abstract decision on screen:<rid>; every study's eligibility reads the same, the
    flags move with the decision, and nothing becomes out of date."""
    from sragent import graph
    from sragent.context import Ctx
    from test_golden import golden_mock
    ctx = reviewed(tmp_path, AWAITING)
    st = ctx.store
    want = json.loads(json.dumps(view(ctx), default=list))
    # merge each elig node back into its screen node, as schema 3 stored it
    for el in list(st.by_type("eligibility")):
        rid = el["id"].split(":", 1)[1]
        sc = st.get(f"screen:{rid}")
        sc["history"].append({"version": sc["version"], "data": sc["data"], "evidence": sc["evidence"], "status": sc["status"]})
        sc["data"] = {**el["data"], "title_abstract": {k: sc["data"].get(k) for k in ("decision", "because")}}
        sc["version"] += 1
        sc["depends_on"] = sorted({f"rec:{rid}"} | (set(el["depends_on"]) - {f"screen:{rid}", "cfg:fulltext_unclear"}))
        sc["dep_versions"] = {d: st.get(d)["version"] for d in sc["depends_on"]}
        sc["evidence"], sc["flags"] = el["evidence"], el["flags"]
        for x in ("ext", "rob"):
            m = st.get(f"{x}:{rid}")
            if m and f"elig:{rid}" in m["depends_on"]:
                m["depends_on"] = sorted((set(m["depends_on"]) - {f"elig:{rid}"}) | {f"screen:{rid}"})
                m["dep_versions"].pop(f"elig:{rid}")
                m["dep_versions"][f"screen:{rid}"] = sc["version"]
        del st.nodes[el["id"]]
    st.meta["schema"] = 3
    st.reindex()
    ctx.save()
    c2 = Ctx.create(ctx.cfg, mock_fn=golden_mock)
    s2 = c2.store
    assert (ctx.run_dir / "store.schema3.bak.json").exists() and s2.meta["schema"] == graph.SCHEMA
    assert {n["id"] for n in s2.by_type("eligibility")} == {"elig:G01", "elig:G02", "elig:G03", "elig:G06"}
    assert json.loads(json.dumps(view(c2), default=list)) == want
    assert s2.get("screen:G01")["data"]["stage"] == "title_abstract" and "elig:G01" in s2.get("ext:G01")["depends_on"]
    assert not graph.stale(s2)
    assert not {n["id"]: p for n in s2.nodes.values() if (p := graph.edge_problems(n))}


def test_a_changed_rule_set_makes_its_exclusions_out_of_date(tmp_path):
    """Rule exclusions depend on the rule setting; the agent's own screening decisions do not."""
    from sragent import graph, state
    ctx = reviewed(tmp_path)
    st = ctx.store
    assert st.get("screen:G07")["data"]["rule"] and state.rules_applied(st)
    st.write("cfg:screening_rules", "setting", {"value": ["no_abstract_off_topic"]}, reason="rule set changed")
    stale = graph.stale(st)
    assert "screen:G07" in stale and "rules" in stale
    assert not [n for n in stale if n.startswith("screen:") and n != "screen:G07"]
    assert state.rules_applied(st) is None               # the interface applies the rules again


def test_derived_notes_are_never_stored_as_flags():
    """Awaiting, included-by-policy, differs-from-your-decision, source-changed and your-edit notices are derived from
    the graph (attention.py); no code stores them as flags that would then need to be kept in step by hand."""
    import re
    from sragent.attention import SOURCES
    root = Path(__file__).resolve().parents[1] / "sragent"
    hits = [f"{p.relative_to(root)}:{i}" for p in root.rglob("*.py") for i, line in enumerate(p.read_text().splitlines(), 1)
            if re.search(r"source=[\"'](%s)[\"']" % "|".join(SOURCES), line)]
    assert not hits, hits


# names of 04 §4 that the registry spells differently, and registered operations that 04 §4 did not list
REGISTERED_AS = {"propagate_edit": "edit.update_dependents", "eligibility.overturn": "eligibility.decide",
                 "eligibility.rederive": "screen.rederive",
                 "synthesis.update": "update_all", "synthesis.resolve_flag": "flag.answer", "search.draft_strategy": "search.suggest_variants"}
NOT_IN_04 = {"edit.dependents_fine", "eligibility.decide_many", "feedback.clarify", "feedback.disagreement", "comment.resolve", "flag.answer",
             "screen.run", "retrieve.run", "eligibility.run", "extract.run", "rob.run", "search.run", "synthesis.write",
             "protocol.draft", "report.render", "eligibility.decide", "update_all",
             "screen.rerun_all", "eligibility.rerun_all", "extract.rerun_all", "rob.rerun_all", "synthesis.rerun_all"}


def test_the_operations_are_the_registry():
    """Every operation of 04 §4 that exists today is registered (sragent/ops), and every registered operation is
    in 04 §4 or listed here as an addition."""
    from sragent import ops
    ops._load()
    existing = {k for k, how in OPS.items() if not how.startswith(("new", "gap"))} - {"settings.change"}
    missing = {k for k in existing if REGISTERED_AS.get(k, k) not in ops.REGISTRY}
    assert not missing, f"in 04 §4 and covered, but not registered: {sorted(missing)}"
    known = {REGISTERED_AS.get(k, k) for k in OPS} | NOT_IN_04
    extra = set(ops.REGISTRY) - known
    assert not extra, f"registered but in neither 04 §4 nor NOT_IN_04: {sorted(extra)}"
    for o in ops.listing():
        assert o["summary"], f"{o['name']} has no description"


def test_api_op_applies_or_queues_and_the_cli_runs_operations(tmp_path, monkeypatch, capsys):
    """POST /api/op: a person's operation is applied now; a model operation becomes a job that runs `sragent op` in a
    subprocess (never a model call in the web request). `sragent op` lists and runs operations."""
    import yaml
    from sragent import cli, state
    from sragent.ui.server import App
    from test_golden import golden_mock
    ctx = reviewed(tmp_path)
    cfgp = tmp_path / "ui.yaml"
    cfg = {k: v for k, v in ctx.cfg.items() if not k.startswith("_")}
    cfg["models_file"] = str(Path(__file__).resolve().parents[1] / "configs/models.yaml")
    cfg["local"] = {**cfg["local"], "endpoints_file": str(tmp_path / "eps.yaml")}
    (tmp_path / "eps.yaml").write_text(yaml.safe_dump({"endpoints": []}))
    cfgp.write_text(yaml.safe_dump(cfg))
    app = App(str(cfgp))
    app.ctx().hub.mock_fn = golden_mock
    r = app.op({"name": "protocol.approve", "args": {"note": "fine"}})
    assert r["ok"] and state.approval(app.ctx().store)
    assert app.op({"name": "nope"})["ok"] is False
    with app.lock:                                   # the worker cannot take the job while the lock is held
        q = app.op({"name": "rob.verify", "args": {"rids": ["G01"]}})
        assert q["ok"] and q["job"]["action"] == "op" and q["job"]["op"] == "rob.verify"
        cmd = app._cmd(q["job"], cfgp)
        assert cmd[3:5] == ["op", "rob.verify"] and '"rids": ["G01"]' in cmd[-1]
        app.queue.clear()
    assert app.op({"name": "preview", "args": {"node": "ext:G01"}})["reach"]       # a read: answered now
    cli.main(["op", "--list"])
    out = capsys.readouterr().out
    assert "eligibility.decide" in out and "rob.edit_domains" in out
    monkeypatch.setattr(cli, "_ctx", lambda a, **k: ctx)
    cli.main(["op", "rob.confirm", "--config", "x", "--args", '{"rids": ["G01"], "note": "cli"}'])
    assert ctx.store.get("rob:G01")["data"]["human_confirmed"]["note"] == "cli"


def test_one_writer_per_run_folder(tmp_path):
    """While another process holds the run folder (a command, a job), a change made in the interface is queued, not
    applied under it; it is applied at the next poll once the folder is free (sragent/lease.py)."""
    import subprocess
    import sys as _sys
    import time as _time
    import yaml
    from sragent import lease, state
    from sragent.ui.server import App
    ctx = reviewed(tmp_path)
    cfgp = tmp_path / "ui.yaml"
    cfg = {k: v for k, v in ctx.cfg.items() if not k.startswith("_")}
    cfg["models_file"] = str(Path(__file__).resolve().parents[1] / "configs/models.yaml")
    cfg["local"] = {**cfg["local"], "endpoints_file": str(tmp_path / "eps.yaml")}
    (tmp_path / "eps.yaml").write_text(yaml.safe_dump({"endpoints": []}))
    cfgp.write_text(yaml.safe_dump(cfg))
    app = App(str(cfgp))
    root = str(Path(__file__).resolve().parents[1])
    holder = subprocess.Popen([_sys.executable, "-c", f"import sys, time; sys.path.insert(0, {root!r}); from sragent import lease; "
                               f"lease.acquire({str(ctx.run_dir)!r}); print('held', flush=True); time.sleep(3)"],
                              stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    assert not lease.try_acquire(ctx.run_dir) and "pid" in lease.holder(ctx.run_dir)
    r = app.op({"name": "protocol.approve", "args": {"note": "while busy"}})
    assert r["ok"] and r.get("queued") and not state.approval(app.ctx().store)      # queued, not applied under the holder
    holder.wait(10)
    for _ in range(20):
        app.state()
        if state.approval(app.ctx().store):
            break
        _time.sleep(0.2)
    assert state.approval(app.ctx().store)["note"] == "while busy" and not app.pending_answers


def test_every_stage_page_has_the_same_toolbar(tmp_path):
    """The ten stage pages are built from one template: the same five buttons with the same meaning; a button a stage
    does not have is disabled with its reason; rows carry the operations a person can apply."""
    from sragent.stagespec import KEYS
    from sragent.ui.stagepage import needs_you, stage_page
    ctx = reviewed(tmp_path, AWAITING)
    for k in KEYS:
        p = stage_page(ctx, k)
        assert [b["key"] for b in p["toolbar"]] == ["run", "rerun_all", "clear", "verify", "stop"], k
        assert all(b["enabled"] or b["reason"] for b in p["toolbar"]), k
        for row in p["items"]:
            assert {"id", "title", "stale", "notes", "actions"} <= set(row), (k, row)
    elig = stage_page(ctx, "eligibility")
    assert elig["status"] == "needs you" and any(a["op"] == "eligibility.decide" for r in elig["items"] for a in r["actions"])
    assert {b["key"]: b["enabled"] for b in stage_page(ctx, "search")["toolbar"]}["clear"] is False
    assert set(needs_you(ctx)) >= {"protocol", "eligibility"}


def test_clear_keeps_what_people_decided_and_the_next_run_redoes_the_rest(tmp_path):
    """<stage>.clear retires the agent's outputs and keeps a person's; the stage shows them as not done, the next
    run redoes exactly those; the preview showed the reach first and changed nothing."""
    from sragent import graph, ops, pipeline
    ctx = reviewed(tmp_path)
    st = ctx.store
    assert ops.run(ctx, "rob.confirm", {"rids": ["G01"], "note": "fine"})["ok"]
    before = {n["id"]: n["version"] for n in st.nodes.values()}
    pv = ops.preview(ctx, "rob.clear", {})
    assert pv["changes"] == ["rob:G02"] and "synthesis" in pv["reach"]
    assert {n["id"]: n["version"] for n in st.nodes.values()} == before          # the preview changed nothing
    r = ops.run(ctx, "rob.clear", {"note": "redo"})
    assert r["ok"] and r["cleared"] == ["rob:G02"] and r["kept"] == ["rob:G01"]
    assert st.get("rob:G02")["status"] == "retired"
    pipeline.run(ctx)
    assert st.get("rob:G02")["status"] == "active" and st.get("rob:G01")["data"]["human_confirmed"]
    assert not graph.stale(st, ["rob"])
    s = ops.run(ctx, "synthesis.clear", {})
    assert s["ok"] and s["count"] == 3                                           # no statement was edited by a person


REMOVED_ENDPOINTS = ("/api/answer", "/api/bulk", "/api/edit", "/api/protocol", "/api/search_strategy", "/api/rerun",
                     "/api/propagate", "/api/comment", "/api/comment_resolve")


def test_every_change_from_the_page_is_an_operation():
    """The page changes a review only through /api/op (with /api/preview first); the older request shapes are gone
    from the page scripts and from the server (04 §5.3)."""
    root = Path(__file__).resolve().parents[1] / "sragent" / "ui"
    page = "".join(p.read_text() for p in (root / "static").rglob("*.js")) + (root / "static" / "index.html").read_text()
    server = (root / "server.py").read_text()
    for ep in REMOVED_ENDPOINTS:
        assert f'"{ep}"' not in page, f"the page still calls {ep}"
        assert f'path == "{ep}"' not in server and f'"{ep}",' not in server, f"the server still serves {ep}"
    assert '"/api/op"' in page and '"/api/preview"' in page


def test_a_todo_answer_names_its_operation():
    """/api/resolve: every To-do answer stands for one operation (model ones run as jobs)."""
    from sragent import todos
    cases = {("elig:G03", "include"): "eligibility.decide", ("elig:G03", "pdf"): "retrieve.request",
             ("protocol", "approve"): "protocol.approve", ("protocol", "edited"): "protocol.import_file",
             ("rob:G01", "confirm"): "rob.confirm", ("rob:G01", "fix"): "rob.recompute", ("cs:FB1", "accept"): "proposal.accept",
             ("fulltexts", "process"): "retrieve.process_added_files", ("sync", "sync"): "update_all",
             ("edited:ext:G01", "update"): "edit.update_dependents", ("edited:ext:G01", "ok"): "edit.dependents_fine",
             ("flag:syn:S1:F3", "ok"): "flag.answer", ("elig:G03", "later"): "snooze"}
    for (tid, opt), name in cases.items():
        assert todos.to_operation({"todo_id": tid, "option": opt})[0] == name, (tid, opt)


def test_every_operation_is_accounted_for():
    import test_golden
    import test_offline
    by_op = {}
    for name, (op, _, _) in SCENARIOS.items():
        by_op.setdefault(op, []).append(name)
    assert set(by_op) <= set(OPS), f"scenarios for operations not in 04 §4: {sorted(set(by_op) - set(OPS))}"
    for op, how in OPS.items():
        kind = how.split(":", 1)[0]
        assert kind in ("ops", "op", "golden", "offline", "new", "gap"), (op, how)
        if how == "ops":
            assert by_op.get(op), f"{op}: marked 'ops' but no scenario"
        elif kind == "ops":
            assert how.split(":", 1)[1].split()[0] in SCENARIOS, (op, how)
        elif kind == "golden":
            assert hasattr(test_golden, f"test_golden_{how.split(':', 1)[1]}"), (op, how)
        elif kind == "offline":
            assert hasattr(test_offline, how.split(":", 1)[1]) or how.split(":", 1)[1] in globals(), (op, how)
