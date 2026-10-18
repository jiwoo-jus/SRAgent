"""Stage 3c: citation chasing (backward and forward snowballing) from the included studies, via OpenAlex.

For every study included after the full-text review: the works it cites (backward) and the works that
cite it (forward, up to the search's end date) are collected. Records already in the review are only
marked as also found this way. New works pass a deterministic pre-filter (title or abstract mentions
an intervention concept or one of its database synonyms) and then go through the normal route: rules,
title/abstract screening, full-text review. `meta.found_via` records how every record was found, so
the end-to-end evaluation can count studies found by citation chasing.
"""
from __future__ import annotations

from .. import rules
from ..context import Ctx
from ..sources import openalex, pubmed, synergy
from . import screening


def _terms(protocol: dict) -> list[str]:
    out = []
    for kc in protocol.get("key_concepts", []):
        if kc.get("kind") == "drug" or kc.get("role") in ("intervention", "exposure"):
            out.append(kc["term"])
            out += (kc.get("validation") or {}).get("synonyms", []) or []
    return [t.lower() for t in out if len(t) > 3] or [t.lower() for t in rules.key_terms(protocol) if len(t) > 3]


def snowball(ctx: Ctx, protocol: dict) -> dict:
    st = ctx.store
    p = ctx.cfg["pipeline"]
    if not p.get("citation_chasing", True):
        return {"skipped": True}
    from .fulltext import contact_email, secrets
    openalex.configure(contact_email(ctx), secrets(ctx).get("openalex_api_key", ""))
    meta = st.meta.setdefault("snowball", {"seeds": {}, "added": [], "rounds": 0})
    fv = st.meta.setdefault("found_via", {})
    until = (ctx.topic.get("records") or {}).get("maxdate")
    until = until.replace("/", "-") if until else None
    terms = _terms(protocol)
    retrieved = set(((st.get("search") or {}).get("data") or {}).get("retrieved_pmids") or [])
    for n in st.by_type("record"):
        if n["data"].get("pmid") in retrieved and "search" not in fv.setdefault(n["id"].split(":", 1)[1], []):
            fv[n["id"].split(":", 1)[1]].append("search")
    by_pmid = {n["data"].get("pmid"): n["id"].split(":", 1)[1] for n in st.by_type("record") if n["data"].get("pmid")}
    by_doi = {(n["data"].get("doi") or "").lower(): n["id"].split(":", 1)[1] for n in st.by_type("record") if n["data"].get("doi")}
    # records added before the date check existed: exclude those published after the search end date
    maxdate = (ctx.topic.get("records") or {}).get("maxdate")
    added = [r for r in meta.get("added", []) if (st.get(f"rec:{r}") or {}).get("data", {}).get("pmid")]
    if maxdate and added:
        ok = pubmed.within_dates([st.get(f"rec:{r}")["data"]["pmid"] for r in added], maxdate=maxdate)
        for r in added:
            if st.get(f"rec:{r}")["data"]["pmid"] not in ok:
                rules.exclude(ctx, r, "outside_date_range", f"Published after the search end date ({maxdate}).")
    total_new = []
    for rnd in range(int(p.get("citation_chasing_rounds", 1))):
        seeds = [r for r in st.included_ids() if r not in meta["seeds"]]
        if not seeds:
            break
        cands, filtered = {}, 0
        for rid in seeds:
            rec = st.get(f"rec:{rid}")["data"]
            src = "openalex"
            try:
                w = openalex.work(rec.get("pmid", ""), rec.get("doi", ""))
                back = [openalex.to_record(x) for x in openalex.works_by_ids(openalex.references(w))] if w else []
                fwd = [openalex.to_record(x) for x in openalex.citing(w, max_n=int(p.get("citation_chasing_max_citing", 400)),
                                                                        until=until)] if w else []
            except RuntimeError as e:
                # OpenAlex refuses (no key / daily budget): fall back to NCBI links (PMC-deposited references and citations)
                if not rec.get("pmid"):
                    ctx.log(f"[snowball] {rid}: OpenAlex unavailable ({str(e)[:60]}) and no PMID for NCBI links")
                    continue
                src = "ncbi_elink"
                try:
                    refs = pubmed.elink(rec["pmid"], "pubmed_pubmed_refs")
                    cit = pubmed.elink(rec["pmid"], "pubmed_pubmed_citedin")
                    recs = {x["pmid"]: x for x in pubmed.efetch(refs + cit)} if refs or cit else {}
                except RuntimeError as e2:
                    ctx.log(f"[snowball] {rid}: no citation source reachable ({str(e2)[:60]})")
                    continue
                back = [recs[x] for x in refs if x in recs]
                fwd = [recs[x] for x in cit if x in recs]
            meta["seeds"][rid] = {"source": src, "references": len(back), "citing": len(fwd)}
            for r, how in [(x, f"backward:{rid}") for x in back] + [(x, f"forward:{rid}") for x in fwd]:
                r = {**r, "pmid": r.get("pmid", ""), "doi": (r.get("doi") or "").lower(), "abstract": r.get("abstract") or "",
                     "title": r.get("title") or "", "year": str(r.get("year") or "")}
                hit = by_pmid.get(r["pmid"]) or by_doi.get(r["doi"])
                if hit:
                    if f"citation" not in fv.setdefault(hit, []):
                        fv[hit].append("citation")
                    continue
                if until and r["year"] and r["year"] > until[:4]:
                    continue
                blob = (r["title"] + " " + r["abstract"]).lower()
                if not any(t in blob for t in terms):
                    filtered += 1
                    continue
                key = r["pmid"] or r["doi"] or r.get("openalex", "")
                cands.setdefault(key, (r, how))
        # the search's date limit applies to citation chasing too (PubMed's own date filter for PMIDs)
        maxdate = (ctx.topic.get("records") or {}).get("maxdate")
        if maxdate:
            pm_all = [r["pmid"] for r, _ in cands.values() if r["pmid"]]
            ok = pubmed.within_dates(pm_all, maxdate=maxdate)
            late = [k for k, (r, _) in cands.items() if r["pmid"] and r["pmid"] not in ok]
            for k in late:
                cands.pop(k)
            if late:
                ctx.log(f"[snowball] {len(late)} candidates published after the search end date {maxdate} skipped")
        # prefer PubMed metadata (publication types matter for the screening rules)
        pm = [r["pmid"] for r, _ in cands.values() if r["pmid"]]
        full = {x["pmid"]: x for x in (pubmed.efetch(pm) if pm else [])}
        new = []
        for key, (r, how) in cands.items():
            rec = dict(full.get(r["pmid"], r))
            if r.get("openalex"):
                rec.setdefault("openalex", r.get("openalex"))
            rec["rid"] = synergy.rid_for(rec)
            if st.get(f"rec:{rec['rid']}"):
                continue
            rec["found_via"] = how
            st.upsert(f"rec:{rec['rid']}", "record", rec, depends_on=["search"], reason=f"citation chasing ({how})")
            fv[rec["rid"]] = ["citation"]
            new.append(rec["rid"])
        meta["added"] += new
        meta["rounds"] += 1
        total_new += new
        ctx.log(f"[snowball] round {rnd + 1}: {len(seeds)} seeds -> {len(cands)} new candidates "
                f"({filtered} without an intervention term skipped); added {len(new)}")
        if new:
            rules.apply(ctx, protocol)
            todo = [r for r in new if st.get(f"screen:{r}") is None]
            ctx.map(lambda r: screening.screen_one(ctx, protocol, r, "title_abstract"), todo)
            screening.fulltext_review(ctx, protocol)
        ctx.save()
    return {"added": len(total_new), "seeds": len(meta["seeds"])}
