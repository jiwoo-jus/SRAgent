"""Operations of stage 2: the search strategy and the search."""
from __future__ import annotations

from . import operation


def _protocol(ctx):
    p = (ctx.store.get("protocol") or {}).get("data")
    if not p:
        raise ValueError("draft the protocol first")
    if ctx.store.get("search") is not None:
        raise ValueError("the search has already run; a new query needs a fresh run folder")
    return p


@operation("search.suggest_variants", "search", "llm")
def suggest_variants(ctx) -> dict:
    """The agent's query (drafted if needed) and alternatives, each with its PubMed count."""
    from ..stages import search
    with ctx.at("search"):
        m = search.suggest_queries(ctx, _protocol(ctx))
    return {"message": f"{len(m['variants']) - 1} alternatives proposed", "variants": m["variants"]}


@operation("search.refine", "search", "llm")
def refine(ctx, query: str, instruction: str) -> dict:
    """Change a query as asked in plain words; the revised query with its count and why."""
    from ..stages import search
    if not query or not instruction:
        raise ValueError("a query and a request are needed")
    with ctx.at("search"):
        return search.refine_query(ctx, _protocol(ctx), query, instruction)


@operation("search.count", "search", "read")
def count(ctx, query: str) -> dict:
    """PubMed hit count of any query (no model call); kept as the draft."""
    from .. import state
    from ..stages import search
    if not query:
        raise ValueError("write a query first")
    r = search.count(ctx, query)
    m = state.strategy(ctx.store)
    m["draft"] = {"query": query, "count": r.get("count")}
    state.save_strategy(ctx.store, m, actor="user", reason="query counted")
    ctx.save()
    return {"query": query, **r}


@operation("search.confirm_strategy", "search", "human", reach=True)
def confirm_strategy(ctx, query: str, note: str = "") -> dict:
    """The query the search will run (instead of the agent's), recorded with its count and who chose it."""
    from ..stages import search
    _protocol(ctx)
    if not query:
        raise ValueError("choose or write a query first")
    c = search.confirm_query(ctx, query, note=note)
    return {"message": f"query confirmed ({c.get('count')} hits); run the search when ready", "confirmed": c}


@operation("search.clear_strategy", "search", "human", reach=True)
def clear_strategy(ctx, note: str = "") -> dict:
    """Withdraw the confirmed query (the agent's own query is used unless another is confirmed)."""
    from .. import state
    m = state.strategy(ctx.store)
    m["confirmed"] = None
    state.save_strategy(ctx.store, m, actor="user", reason="confirmation removed")
    return {"message": "confirmation removed"}


@operation("search.run", "search", "llm")
def run(ctx) -> dict:
    """Run the search with the confirmed query (else the agent's) and keep the records."""
    from .. import pipeline
    pipeline.run(ctx, until="search")
    return {"message": "search done"}


@operation("search.bibliography", "search", "deterministic")
def bibliography(ctx, only_included: bool = True) -> dict:
    """Complete the reference details of the included studies from PubMed (journal, volume, issue, pages); no model
    call. A reference list needs them and the search did not keep them. Screening, extraction and quality
    assessment do not read them (graph.READS), so nothing goes out of date."""
    from ..sources import pubmed
    st = ctx.store
    ids = list(st.included_ids()) if only_included else [k.split(":", 1)[1] for k in st.nodes if k.startswith("rec:")]
    todo = [r for r in ids if (st.get(f"rec:{r}") or {}).get("data", {}).get("pmid")
            and not (st.get(f"rec:{r}") or {}).get("data", {}).get("pages")]
    pubmed.configure(ctx.cfg.get("contact_email", ""), ctx.cfg.get("ncbi_api_key", ""))
    got, n = {r["pmid"]: r for r in pubmed.efetch([(st.get(f"rec:{r}") or {})["data"]["pmid"] for r in todo])}, 0
    for rid in todo:
        node = st.get(f"rec:{rid}")
        new = got.get(node["data"].get("pmid"))
        add = {k: new[k] for k in ("journal", "volume", "issue", "pages") if new and new.get(k)} if new else {}
        if add and any(node["data"].get(k) != v for k, v in add.items()):
            st.annotate(f"rec:{rid}", {**node["data"], **add}, actor="agent", reason="reference details from PubMed")
            n += 1
    ctx.save()
    return {"message": f"reference details completed for {n} of {len(todo)} studies that lacked them",
            "updated": n, "looked_up": len(todo)}
