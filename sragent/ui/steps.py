"""Per-step status for the UI's step view (no LLM calls)."""
from __future__ import annotations

from collections import Counter

from .. import provenance, state
from ..stages import fulltext
from ..context import Ctx
from ..todos import STEPS


def steps(ctx: Ctx, todos: list[dict]) -> list[dict]:
    st = ctx.store
    tcount = Counter(t["step"] for t in todos if not t.get("snoozed") and not t.get("optional"))
    screens = st.by_type("screen")
    dec = Counter(n["data"]["decision"] for n in screens)
    ta = [n for n in screens if n["data"]["stage"] == "title_abstract"]
    inc = st.included_ids()
    fts = st.by_type("fulltext")
    n_full = sum(1 for n in fts if n["data"].get("kind") == "fulltext")
    n_user = sum(1 for n in fts if str(n["data"].get("origin", "")).startswith("user file"))
    ext = [r for r in inc if st.get(f"ext:{r}")]
    robs = [st.get(f"rob:{r}") for r in inc if st.get(f"rob:{r}")]
    conf = sum(1 for n in robs if n["data"].get("human_confirmed"))
    syn = st.by_type("synthesis", active_only=True)
    syn_flag = sum(1 for n in syn for f in n["flags"] if f["status"] == "open")
    s = (st.get("search") or {}).get("data", {})
    conf_q = state.confirmed_query(st) or {}
    proto = st.get("protocol")
    human_dec = sum(1 for n in screens if n["data"].get("human_decision"))

    ft_dec = [n for n in screens if n["data"]["stage"] == "full_text"]
    fdec = Counter(n["data"]["decision"] for n in ft_dec)
    retrieval = [n for n in screens if n["data"]["stage"] == "retrieval"]
    sb = state.snowball(st) or {}
    chasing = ctx.cfg["pipeline"].get("citation_chasing", True)
    rep = st.get("report")
    # key -> (done, summary, run until); the keys and their order are stagespec.STAGES (+ feedback, across stages)
    info = {
        "protocol": (bool(proto), "approved" if state.approval(st) else
                     ("drafted by the agent (agent-only: used as drafted)" if provenance.mode(ctx) == "agent_only"
                      else "drafted, waiting for your approval") if proto else "not started", "protocol"),
        "search": (bool(s), f"{s.get('hits', 0)} PubMed hits; {s.get('n_identified', 0)} records, "
                   f"{s.get('n_duplicates', 0)} duplicates removed" if s else
                   (f"query confirmed by you ({conf_q.get('count')} PubMed hits); run up to stage 2 or later"
                    if conf_q.get("query") else "not started"), "search"),
        "screen": (bool(screens) and len(screens) >= len(st.by_type("record")),
                   f"{len(screens)} screened · {len(ta)} decided at title/abstract · "
                   f"{sum(1 for n in ta if n['data']['decision'] == 'exclude')} excluded there · "
                   f"{dec.get('forward', 0)} waiting for full-text review · {human_dec} decided by a person"
                   if screens else "not started", "screen"),
        "retrieve": (bool(fts) and not retrieval, f"{len(fts)} forwarded studies · {n_full} with full text ({n_user} from your PDFs) · "
                     f"{dec.get('fulltext_unavailable', 0)} not retrieved (PDF requested) · {dec.get('parse_failed', 0)} unreadable"
                     if fts else "not started", "retrieve"),
        "eligibility": (bool(ft_dec) and not fdec.get("awaiting") and not dec.get("forward"),
                        f"{len(ft_dec)} screened on the full text · {fdec.get('include', 0)} included · "
                        f"{fdec.get('exclude', 0)} excluded · {fdec.get('awaiting', 0)} undecided"
                        if ft_dec else "not started", "eligibility"),
        "snowball": (bool(sb) or not chasing,
                     f"{len(sb.get('added') or [])} records found from {len(sb.get('seeds') or {})} included studies "
                     f"({sb.get('rounds', 0)} round(s))" if sb else ("off (pipeline.citation_chasing)" if not chasing else "not started"),
                     "snowball"),
        "extract": (bool(inc) and len(ext) == len(inc), f"{len(ext)}/{len(inc)} included studies extracted" if inc else "waits for screening", "extract"),
        "rob": (bool(robs) and len(robs) == len(inc), f"{len(robs)}/{len(inc)} drafted · {conf} confirmed by you" if inc else "waits for extraction", "rob"),
        "synthesis": (bool(syn), f"{len(syn)} statements · {syn_flag} flagged by the self-check" if syn else "not written yet", "synthesis"),
        "report": (bool(rep), "report.md and report_evidence.html written" if rep else "not written yet", "report"),
        "feedback": (True, "flag anything in the report or the to-do list; proposals appear here", None),
    }
    ready = {"search": not s and bool(conf_q.get("query")),                               # query confirmed, not run yet
             "synthesis": not syn and bool(inc) and len(ext) == len(inc) and len(robs) == len(inc)
                          and not fulltext.waiting_for_pdfs(ctx)}                      # inputs complete, not written yet
    out = []
    for key, label in STEPS:
        done, summary, run_until = info[key]
        n = tcount.get(key, 0)
        status = "needs you" if n else ("done" if done else ("ready to run" if ready.get(key) else "to do"))
        out.append({"key": key, "label": label, "status": status, "summary": summary, "todos": n,
                    "run_until": run_until})
    return out
