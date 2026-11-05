"""End-to-end orchestration. The stages are a registry (REGISTRY): each knows how to run itself on a review,
skips finished work, and can be left out with `pipeline.skip_stages`. `sragent run --until <stage>` stops
after that stage; the order is the registry's order."""
from __future__ import annotations

from . import stagespec
from . import state as review_state
from .context import Ctx
from .stages import extraction, fulltext, planning, report, rob, screening, search, synthesis


def _protocol(ctx, state):
    with ctx.at("protocol"):
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


def _retrieve(ctx, state):
    with ctx.at("retrieve"):
        screening.retrieve_all(ctx)                           # the full text of every forwarded record (no model calls)


def _eligibility(ctx, state):
    with ctx.at("eligibility"):
        screening.eligibility_all(ctx, state["protocol"])     # every retrieved record screened on its full text


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


def _synthesis(ctx, state):
    st = ctx.store
    considered = set(review_state.considered(st) or [])
    human_edited = any(n["data"].get("edited") for n in st.by_type("synthesis", active_only=True))
    if human_edited and (state["changed"] or considered != set(st.included_ids())):
        ctx.log("[pipeline] the synthesis has human edits; it is not re-written (use 'Update outputs' in the To-do list)")
    elif not st.by_type("synthesis", active_only=True):
        with ctx.at("synthesis"):
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


# what runs each stage of stagespec.STAGES (key, label, model: stagespec)
_RUN = {"protocol": _protocol, "search": _search, "screen": _screen, "retrieve": _retrieve, "eligibility": _eligibility,
        "snowball": _snowball, "extract": _extract, "rob": _rob, "synthesis": _synthesis, "report": _report}
REGISTRY: list[dict] = [{"key": s.key, "label": s.label, "run": _RUN[s.key], "llm": s.llm} for s in stagespec.STAGES]
STAGES = list(stagespec.KEYS)


def enabled(ctx: Ctx) -> list[str]:
    skip = set(ctx.cfg["pipeline"].get("skip_stages") or [])
    return [s["key"] for s in REGISTRY if s["key"] not in skip]


def _waiting_for_search(ctx: Ctx) -> bool:
    """pipeline.confirm_search: the search runs only with a query the review team confirmed (To-do list: the agent's
    query, alternatives with their PubMed counts, changes asked in plain words). Once the search exists, nothing waits."""
    st = ctx.store
    if not ctx.cfg["pipeline"].get("confirm_search") or st.get("search"):
        return False
    if review_state.confirmed_query(st):
        return False
    ctx.save()
    ctx.log("[pipeline] the search waits for a confirmed query (pipeline.confirm_search): choose or write one in the "
            "To-do list (Search strategy), then run again.")
    return True


def _waiting_for_pdfs(ctx: Ctx) -> bool:
    """pipeline.wait_for_pdfs: the first synthesis is written only once every requested full text has arrived or
    been declared not obtainable, so it is written once, on the settled set, instead of revised study by study.
    A synthesis that already exists is never held back (later PDFs propagate through the update engine)."""
    n = fulltext.waiting_for_pdfs(ctx)
    if not n:
        return False
    fulltext.write_needed_pdfs(ctx)
    ctx.save()
    ctx.log(f"[pipeline] the synthesis waits for {n} full text(s) (pipeline.wait_for_pdfs): add them to "
            f"{fulltext.fulltext_dir(ctx)} as <PMID>.pdf or mark them not obtainable, then run again (the list is "
            f"{fulltext.fulltext_dir(ctx) / 'needed_pdfs.csv'}).")
    return True


def run(ctx: Ctx, until: str = "report", stop_after_plan: bool = False):
    until = stagespec.run_until(until)        # an earlier key (plan, fulltext, synthesize) is accepted
    order = STAGES[: STAGES.index(until) + 1]
    run_now = set(enabled(ctx))
    state = {"protocol": None, "changed": 0}
    for stage in REGISTRY:
        key = stage["key"]
        if key not in order:
            break
        if key not in run_now:
            ctx.log(f"[pipeline] stage {key} skipped (pipeline.skip_stages)")
            continue
        if key == "synthesis" and _waiting_for_pdfs(ctx):
            return
        if key == "search" and _waiting_for_search(ctx):
            return
        stage["run"](ctx, state)
        if key == "protocol" and (stop_after_plan or until == "protocol"):
            ctx.log(f"[pipeline] protocol written to {ctx.run_dir / 'protocol.yaml'} — review/edit it, then run again.")
            return
    ctx.save()
    ctx.log(f"[pipeline] done. cost so far: {ctx.hub.tracker.summary()['spent_usd']} USD")
