"""End-to-end orchestration. The stages are a registry (REGISTRY): each knows how to run itself on a review,
skips finished work, and can be left out with `pipeline.skip_stages`. `sragent run --until <stage>` stops
after that stage; the order is the registry's order."""
from __future__ import annotations

from .context import Ctx
from .stages import extraction, fulltext, planning, report, rob, screening, search, synthesis


def _plan(ctx, state):
    with ctx.at("plan"):
        state["protocol"] = planning.plan(ctx)
    ctx.save()


def _search(ctx, state):
    if ctx.store.get("search") is None:
        with ctx.at("search"):
            search.search(ctx, state["protocol"])
        ctx.save()


def _screen(ctx, state):
    with ctx.at("screen"):
        screening.screen_all(ctx, state["protocol"])
        # records screened before the protocol changed: re-assessed on the changed criteria only
        from . import engine
        p = engine.plan(ctx)
        if any(i["type"] == "screen" and i["action"] in ("reassess", "rederive") for i in p["items"]):
            cs = engine.execute(ctx, {"id": "pipeline_screen", "changes": []}, tag="pipeline", render=False, max_rounds=1)
            ctx.log(f"[screen] protocol changed since screening: {p['summary']}; "
                    f"{len([c for c in cs['changes'] if c['node'].startswith('screen:')])} decisions re-derived")


def _fulltext(ctx, state):
    with ctx.at("fulltext"):
        screening.fulltext_review(ctx, state["protocol"])     # retrieve + screen every forwarded record on its full text


def _snowball(ctx, state):
    from .stages import snowball
    with ctx.at("snowball"):
        snowball.snowball(ctx, state["protocol"])             # citation chasing from the included studies


def _extract(ctx, state):
    with ctx.at("extract"):
        state["changed"] += extraction.extract_all(ctx)


def _rob(ctx, state):
    with ctx.at("rob"):
        state["changed"] += rob.rob_all(ctx)


def _synthesize(ctx, state):
    st = ctx.store
    considered = set(st.meta.get("synthesis_considered") or [])
    human_edited = any(n["data"].get("edited") for n in st.by_type("synthesis", active_only=True))
    if human_edited and (state["changed"] or considered != set(st.included_ids())):
        ctx.log("[pipeline] the synthesis has human edits; it is not re-written (use 'Update outputs' in the To-do list)")
    elif not st.by_type("synthesis", active_only=True):
        with ctx.at("synthesize"):
            synthesis.synthesize(ctx)
    else:
        # an existing synthesis is updated, not re-written: only what the changes reach (the update engine)
        from . import engine
        cs = engine.execute(ctx, {"id": "pipeline", "changes": []}, tag="pipeline", render=False)
        ctx.save()
        if cs["changes"]:
            ctx.log(f"[pipeline] brought up to date: {sorted({c['node'] for c in cs['changes']})}")
        if cs["kept"]:
            ctx.log(f"[pipeline] kept as decided or edited by a person: {[k['node'] for k in cs['kept']]}")


def _report(ctx, state):
    report.write_reports(ctx)


# key, label, what runs it; `llm`: the stage makes model calls (its model comes from stage_models)
REGISTRY: list[dict] = [
    {"key": "plan", "label": "Protocol", "run": _plan, "llm": True},
    {"key": "search", "label": "Search", "run": _search, "llm": True},
    {"key": "screen", "label": "Title/abstract screening", "run": _screen, "llm": True},
    {"key": "fulltext", "label": "Full texts + full-text screening", "run": _fulltext, "llm": True},
    {"key": "snowball", "label": "Citation chasing", "run": _snowball, "llm": True},
    {"key": "extract", "label": "Data extraction", "run": _extract, "llm": True},
    {"key": "rob", "label": "Risk of bias", "run": _rob, "llm": True},
    {"key": "synthesize", "label": "Synthesis", "run": _synthesize, "llm": True},
    {"key": "report", "label": "Report (whole review)", "run": _report, "llm": False},
]
STAGES = [s["key"] for s in REGISTRY]


def enabled(ctx: Ctx) -> list[str]:
    skip = set(ctx.cfg["pipeline"].get("skip_stages") or [])
    return [s["key"] for s in REGISTRY if s["key"] not in skip]


def _waiting_for_pdfs(ctx: Ctx) -> bool:
    """pipeline.wait_for_pdfs: the first synthesis is written only once every requested full text has arrived or
    been declared not obtainable, so it is written once, on the settled set, instead of revised study by study.
    A synthesis that already exists is never held back (later PDFs propagate through the update engine)."""
    st = ctx.store
    if not ctx.cfg["pipeline"].get("wait_for_pdfs") or st.by_type("synthesis", active_only=True):
        st.meta.pop("waiting_for_pdfs", None)
        return False
    from .stages import fulltext
    pending = [x for x in fulltext.needed_pdfs(ctx) if not x["present"] and not x["declared_not_obtainable"]]
    if not pending:
        st.meta.pop("waiting_for_pdfs", None)
        return False
    fulltext.write_needed_pdfs(ctx)
    st.meta["waiting_for_pdfs"] = len(pending)
    ctx.save()
    ctx.log(f"[pipeline] the synthesis waits for {len(pending)} full text(s) (pipeline.wait_for_pdfs): add them to "
            f"{fulltext.fulltext_dir(ctx)} as <PMID>.pdf or mark them not obtainable, then run again (the list is "
            f"{fulltext.fulltext_dir(ctx) / 'needed_pdfs.csv'}).")
    return True


def run(ctx: Ctx, until: str = "report", stop_after_plan: bool = False):
    order = STAGES[: STAGES.index(until) + 1]
    skip = set(ctx.cfg["pipeline"].get("skip_stages") or [])
    state = {"protocol": None, "changed": 0}
    for stage in REGISTRY:
        key = stage["key"]
        if key not in order:
            break
        if key in skip:
            ctx.log(f"[pipeline] stage {key} skipped (pipeline.skip_stages)")
            continue
        if key == "synthesize" and _waiting_for_pdfs(ctx):
            return
        stage["run"](ctx, state)
        if key == "plan" and (stop_after_plan or until == "plan"):
            ctx.log(f"[pipeline] protocol written to {ctx.run_dir / 'protocol.yaml'} — review/edit it, then run again.")
            return
    ctx.save()
    ctx.log(f"[pipeline] done. cost so far: {ctx.hub.tracker.summary()['spent_usd']} USD")
