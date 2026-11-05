"""Operations of stages 3-5: title/abstract screening, full texts, full-text eligibility."""
from __future__ import annotations

import time

from . import operation


def _pipeline(ctx, until: str, fresh: bool = False) -> dict:
    from .. import pipeline
    from .stage import fresh_answers
    with fresh_answers(ctx, fresh):
        pipeline.run(ctx, until=until)
    return {"message": f"ran up to {until}" + (" (fresh model answers)" if fresh else "")}


# ---------------------------------------------------------------- 3 · title/abstract screening
@operation("screen.run", "screen", "llm")
def screen_run(ctx, fresh: bool = False) -> dict:
    """Screen every record not screened yet on its title and abstract (and the stages before, if needed)."""
    return _pipeline(ctx, "screen", fresh)


@operation("screen.restore_rule_exclusion", "screen", "human", reach=True)
def restore_rule_exclusion(ctx, rids: list[str], note: str = "") -> dict:
    """Undo rule exclusions: the records go to full-text review (they can only be included there)."""
    from .. import rules
    done = []
    for rid in rids:
        sc = ctx.store.get(f"screen:{rid}")
        if not sc or not sc["data"].get("rule"):
            continue
        rules.restore(ctx, rid, note)
        done.append(rid)
    return {"ok": bool(done) or not rids, "message": f"{len(done)} rule exclusion(s) undone", "count": len(done)}


@operation("screen.apply_rules", "screen", "deterministic", reach=True)
def apply_rules(ctx) -> dict:
    """Apply the screening rules (publication type, off-topic without abstract); no model calls."""
    from .. import rules
    ch = rules.apply(ctx)
    return {"message": f"{len(ch)} record(s) changed by the rules", "count": len(ch)}


@operation("screen.rederive", "screen", "deterministic", reach=True)
def rederive(ctx) -> dict:
    """Re-derive every decision from the stored assessments under the current protocol; no model calls."""
    from ..stages import screening
    r = screening.rescreen_rule_only(ctx, ctx.store.get("protocol")["data"])
    return {"message": f"{r['changed']} decision(s) changed; {r['included']} included", **r}


def _rerun(ctx, rid, step, note, propagate=True, fresh=True):
    from .. import actions
    cs = actions.rerun(ctx, rid, step, note=note or "", propagate=propagate, fresh=fresh)
    return {"message": f"proposal {cs['id']}: {cs.get('explanation', '')}", "changeset": cs["id"], "status": cs.get("status")}


@operation("screen.recompute", "screen", "llm", reach=True)
def screen_recompute(ctx, rid: str, note: str = "", propagate: bool = True, fresh: bool = True) -> dict:
    """Screen one record again, at the stage it is at (a proposal)."""
    return _rerun(ctx, rid, "screen", note, propagate, fresh)


@operation("screen.verify", "screen", "llm")
def screen_verify(ctx, rids: list[str] | None = None) -> dict:
    """A second reading by the verifier of the agent's title/abstract exclusions (and full-text decisions)."""
    from ..stages.screening import verify_existing
    r = verify_existing(ctx, ctx.store.get("protocol")["data"], mode="all", only=rids or None)
    return {"message": f"{r['checked']} read again: {r['outcomes']}", **r}


# ---------------------------------------------------------------- 4 · full texts
@operation("retrieve.run", "retrieve", "llm")
def retrieve_run(ctx, fresh: bool = False) -> dict:
    """Get the full text of every forwarded record (your files first, then open-access copies)."""
    return _pipeline(ctx, "retrieve", fresh)


@operation("retrieve.request", "retrieve", "human")
def request(ctx, rid: str, note: str = "") -> dict:
    """Ask for a (better) full text of an undecided study: it waits in the Full texts list."""
    from .. import state
    from ..stages import fulltext
    if not ctx.store.get(f"rec:{rid}"):
        raise ValueError(f"no record {rid}")
    state.set_pdf_request(ctx.store, rid, wanted=True)
    return {"message": f"waiting for {rid}.pdf in {fulltext.fulltext_dir(ctx)}"}


@operation("retrieve.declare_unobtainable", "retrieve", "human", category="pdf_provision")
def declare_unobtainable(ctx, rids: list[str], note: str = "") -> dict:
    """Full texts that cannot be obtained: they stop being requested (the studies stay not retrieved)."""
    from .. import state
    done = [rid for rid in rids if ctx.store.get(f"rec:{rid}")]
    for rid in done:
        state.set_pdf_request(ctx.store, rid, wanted=False, unavailable=True)
    return {"ok": bool(done) or not rids, "message": f"marked {len(done)} as not obtainable", "count": len(done)}


@operation("retrieve.decide_without", "retrieve", "human")
def decide_without(ctx, rids: list[str], note: str = "") -> dict:
    """Decide these studies without waiting for another full text."""
    from .. import state
    for rid in rids:
        state.set_pdf_request(ctx.store, rid, wanted=False)
    return {"message": f"{len(rids)} decided without another full text", "count": len(rids)}


@operation("retrieve.process_added_files", "retrieve", "llm", reach=True)
def process_added_files(ctx) -> dict:
    """Read the files added to the full-text folder: screen those studies on them, re-extract included ones."""
    from .. import sync
    r = sync.process_pdfs(ctx)
    return {"message": f"{len(r['processed'])} file(s) processed", **{k: r[k] for k in ("processed", "rescreened", "reextracted", "unreadable")}}


@operation("retrieve.fetch", "retrieve", "deterministic")
def fetch(ctx) -> dict:
    """Keep the agent-fetched texts and PDFs of every full text that has none yet."""
    from ..stages.fulltext import backfill_fetched_files
    r = backfill_fetched_files(ctx)
    return {"message": f"{r['studies']} studies, {r['pdfs']} PDFs", **r}


# ---------------------------------------------------------------- 5 · full-text eligibility
@operation("eligibility.run", "eligibility", "llm")
def eligibility_run(ctx, fresh: bool = False) -> dict:
    """Screen every retrieved record on its full text."""
    return _pipeline(ctx, "eligibility", fresh)


@operation("eligibility.decide", "eligibility", "human", reach=True)
def decide(ctx, rid: str, decision: str, criterion: str | None = None, note: str = "") -> dict:
    """A person's eligibility decision (include, exclude, or awaiting); include needs the full text. It is recorded on
    elig:<rid> and kept over every later reading by the agent."""
    from ..selection import view
    from ..stages.screening import person_decides
    st = ctx.store
    if decision not in ("include", "exclude", "awaiting"):
        raise ValueError(f"unknown decision {decision}")
    if not st.get(f"screen:{rid}") and not st.get(f"rec:{rid}"):
        raise ValueError(f"no record {rid}")
    if decision == "include" and (st.get(f"ft:{rid}") or {}).get("data", {}).get("kind") != "fulltext":
        raise ValueError(f"{rid} cannot be included without a full-text review; supply {rid}.pdf first.")
    before = (view(st, rid) or {}).get("decision")
    n = person_decides(st, rid, decision, [criterion or "human"] if decision == "exclude" else [],
                       {"decision": decision, "note": note, "reason": criterion, "ts": time.time()},
                       f"eligibility decided by a person: {before} -> {decision}" + (f". Note: {note}" if note else ""))
    for f in n["flags"]:   # a person decided: the screening notices on it are settled (not a proposal waiting for them)
        if f["status"] == "open" and f.get("source") in ("screening", "screening_unclear", None, "feedback", "eligibility_doubt") \
                and "pending review" not in f.get("text", ""):
            st.resolve_flag(n["id"], f["id"], f"decided by a person: {decision}", actor="user")
    msg = f"{rid}: {before} → {decision}"
    if before != decision and st.by_type("extraction"):
        msg += ". Extraction, risk of bias and synthesis follow with 'Update outputs'."
    return {"message": msg}


@operation("eligibility.decide_many", "eligibility", "human", reach=True, log=False)
def decide_many(ctx, items: list[dict]) -> dict:
    """Several eligibility decisions saved at once (the eligibility table): each is an eligibility.decide, logged as such."""
    done, refused = 0, []
    for it in items or []:
        r = decide(ctx, rid=it["rid"], decision=it.get("decision") or it.get("option"), criterion=it.get("criterion"),
                   note=it.get("note") or it.get("text") or "")
        if r.get("ok"):
            done += 1
        else:
            refused.append(f"{it['rid']}: {r.get('message')}")
    return {"ok": done > 0 or not items, "count": done,
            "message": f"{done} of {len(items or [])} saved" + (f"; refused: {'; '.join(refused)}" if refused else "")}


@operation("eligibility.recompute", "eligibility", "llm", reach=True)
def eligibility_recompute(ctx, rid: str, note: str = "", propagate: bool = True, fresh: bool = True) -> dict:
    """Screen one study again on its full text (a proposal; it replaces a person's decision only if accepted)."""
    return _rerun(ctx, rid, "screen", note, propagate, fresh)


@operation("eligibility.verify", "eligibility", "llm")
def eligibility_verify(ctx, rids: list[str] | None = None, mode: str = "pending") -> dict:
    """A second reading by the verifier: the undecided full-text decisions ('pending'), or all of them ('full_text')."""
    from ..stages.screening import verify_existing
    r = verify_existing(ctx, ctx.store.get("protocol")["data"], mode=mode, only=rids or None)
    return {"message": f"{r['checked']} read again: {r['outcomes']}", **r}
