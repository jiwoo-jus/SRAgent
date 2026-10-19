"""Per-step status for the UI's step view (no LLM calls)."""
from __future__ import annotations

from collections import Counter

from .. import provenance
from ..context import Ctx
from ..todos import STEPS


def steps(ctx: Ctx, todos: list[dict]) -> list[dict]:
    st = ctx.store
    tcount = Counter(t["step"] for t in todos if not t.get("snoozed"))
    screens = st.by_type("screen")
    dec = Counter(n["data"]["decision"] for n in screens)
    ta = [n for n in screens if n["data"]["stage"] == "title_abstract"]
    ft_stage = [n for n in screens if n["data"]["stage"] == "full_text"]
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
    proto = st.get("protocol")
    human_dec = sum(1 for n in screens if n["data"].get("human_decision"))

    info = {
        "plan": (bool(proto), "approved" if st.meta.get("protocol_approved") else
                 ("drafted by the agent (agent-only: used as drafted)" if provenance.mode(ctx) == "agent_only"
                  else "drafted, waiting for your approval") if proto else "not started", "plan"),
        "search": (bool(s), f"{s.get('hits', 0)} PubMed hits; {s.get('n_identified', 0)} records, "
                   f"{s.get('n_duplicates', 0)} duplicates removed" if s else "not started", "search"),
        "screen": (bool(screens) and not any(n["data"]["stage"] == "title_abstract" and n["data"]["decision"] != "exclude" for n in screens),
                   f"{len(screens)} screened · {dec.get('include', 0)} included after full-text review · {dec.get('exclude', 0)} excluded · "
                   f"{dec.get('awaiting', 0)} undecided · {dec.get('forward', 0)} waiting for full-text review · "
                   f"{human_dec} decided by a person" if screens else "not started",
                   "fulltext"),
        "fulltext": (bool(fts), f"{len(fts)} forwarded studies · {n_full} with full text ({n_user} from your PDFs) · "
                     f"{dec.get('fulltext_unavailable', 0)} not retrieved (PDF requested) · {dec.get('parse_failed', 0)} unreadable"
                     if fts else "not started", "fulltext"),
        "extract": (bool(inc) and len(ext) == len(inc), f"{len(ext)}/{len(inc)} included studies extracted" if inc else "waits for screening", "extract"),
        "rob": (bool(robs) and len(robs) == len(inc), f"{len(robs)}/{len(inc)} drafted · {conf} confirmed by you" if inc else "waits for extraction", "rob"),
        "synthesis": (bool(syn), f"{len(syn)} statements · {syn_flag} flagged by the self-check" if syn else "not written yet", "synthesize"),
        "feedback": (True, "flag anything in the report or the to-do list; proposals appear here", None),
    }
    out = []
    for key, label in STEPS:
        done, summary, run_until = info[key]
        n = tcount.get(key, 0)
        status = "needs you" if n else ("done" if done else "to do")
        out.append({"key": key, "label": label, "status": status, "summary": summary, "todos": n,
                    "run_until": run_until})
    return out
