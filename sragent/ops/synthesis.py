"""Operations of stages 9-10: synthesis and report."""
from __future__ import annotations

import copy
import time

from . import operation


@operation("synthesis.write", "synthesis", "llm")
def write(ctx, fresh: bool = False) -> dict:
    """Write the synthesis (or bring an existing one up to date) and the report."""
    from .. import pipeline
    from .stage import fresh_answers
    with fresh_answers(ctx, fresh):
        pipeline.run(ctx, until="report")
    return {"message": "synthesis and report written"}


@operation("synthesis.edit_statement", "synthesis", "human", reach=True)
def edit_statement(ctx, node: str, text: str | None = None, cites: list[str] | str | None = None, scope: str | None = None,
                   note: str = "") -> dict:
    """Rewrite a statement and its citations by hand (citations are reconciled with the studies it names)."""
    from ..stages import synthesis
    st = ctx.store
    n = st.get(node)
    if not n or n["type"] != "synthesis":
        raise ValueError(f"no statement {node}")
    d = copy.deepcopy(n["data"])
    text = (text or d["text"]).strip()
    cites = d["cites"] if cites is None else cites
    if isinstance(cites, str):
        cites = [c.strip() for c in cites.replace(";", ",").split(",") if c.strip()]
    cites, fixes = synthesis.reconcile_citations(st, text, cites)
    d.update({"text": text, "cites": cites, "scope": d.get("scope", "") if scope is None else scope})
    d.pop("verification", None)
    d["edited"] = {"by": "user", "before": n["data"]["text"], "note": note, "ts": time.time()}
    if fixes:
        d["citation_fixes"] = fixes
    st.annotate(node, d, ntype="synthesis", depends_on=synthesis.statement_deps(st, cites), actor="user",
                reason="statement edited by a person." + (f" Note: {note}" if note else ""))
    for f in st.get(node)["flags"]:
        if f["status"] == "open" and f.get("source") == "synthesis_verify":
            st.resolve_flag(node, f["id"], "statement edited by a person", actor="user")
    return {"message": "statement saved" + (f" ({'; '.join(fixes)})" if fixes else "")}


@operation("synthesis.revise", "synthesis", "llm", reach=True)
def revise(ctx, node: str, instruction: str) -> dict:
    """Ask the agent to revise a statement (checked against the cited data; a proposal)."""
    from .. import feedback
    cs = feedback.handle_feedback(ctx, instruction, target=node, auto_accept=False)
    return {"message": f"proposal {cs['id']}: {cs.get('status')}", "changeset": cs["id"], "status": cs.get("status")}


@operation("report.render", "report", "deterministic")
def render(ctx) -> dict:
    """Write report.md and report_evidence.html from the review as it is."""
    from ..stages import report
    report.write_reports(ctx)
    return {"message": "report written"}


@operation("synthesis.meta_analysis", "synthesis", "deterministic")
def meta_analysis(ctx) -> dict:
    """Compute effect sizes and pooled estimates from the rows items (topic.meta_analysis); writes <run folder>/meta/."""
    from .. import meta
    r = meta.write(ctx)
    return {"message": f"{r['rows_used']} of {r['rows']} rows gave an effect; {r['pooled_groups']} of {r['groups']} groups pooled", **r}


@operation("report.manuscript", "report", "deterministic")
def manuscript(ctx) -> dict:
    """Render the report for readers from its plan (report_plan.yaml beside the run file); no model call."""
    from .. import reportplan
    return reportplan.render(ctx)


@operation("report.inventory", "report", "deterministic")
def inventory(ctx) -> dict:
    """What the data can show for a planned report: studies per model and concept, pooled groups, figure types."""
    from .. import reportplan
    plan = reportplan.load(ctx) if reportplan.plan_path(ctx).exists() else {}
    return {"message": "what a report plan can draw on", **reportplan.inventory(ctx, plan)}
