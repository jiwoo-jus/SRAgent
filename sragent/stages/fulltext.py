"""Stage 3b: full texts.

Order of preference for a study with record id <rid> (the PMID whenever the record has one):
  1. a PDF you supplied        <run_dir>/fulltext/<rid>.pdf       (converted to text, page-marked)
  2. a text file you supplied  <run_dir>/fulltext/<rid>.txt
  3. Europe PMC open-access full text (JATS XML)
  4. a legal open-access PDF found through Unpaywall / OpenAlex
  5. the abstract (fallback)

`needed_pdfs.csv` in the same folder lists exactly which files would help, with the expected file
name, title, PMID and DOI, so PDFs can be collected in one go. Run `sragent pdfs` (or use the UI)
after adding files; it converts them and re-screens / re-extracts the affected studies.
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..context import Ctx
from ..sources import europepmc


def fulltext_dir(ctx: Ctx) -> Path:
    o = (ctx.cfg.get("pipeline") or {}).get("fulltext_dir")
    d = Path(o) if o else ctx.run_dir / "fulltext"
    d.mkdir(parents=True, exist_ok=True)
    readme = d / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Put full-text PDFs here, named by record id:  <rid>.pdf   (rid = PMID when the record has one)\n"
            "Example: 30157389.pdf\n"
            "The exact file names that would help are listed in needed_pdfs.csv (column 'filename').\n"
            "Plain-text files (<rid>.txt) are accepted too. Then run `sragent pdfs --config ...` or press\n"
            "'Process added PDFs' in the UI.\n")
    return d


def local_file(ctx: Ctx, rid: str) -> Path | None:
    d = fulltext_dir(ctx)
    rec = (ctx.store.get(f"rec:{rid}") or {}).get("data", {})
    names = [rid]
    if rec.get("pmid") and rec["pmid"] != rid:
        names.append(rec["pmid"])
    for n in names:
        for ext in (".pdf", ".PDF", ".txt"):
            p = d / f"{n}{ext}"
            if p.exists() and p.stat().st_size > 0:
                return p
    return None


def secrets(ctx: Ctx) -> dict:
    from ..config import load_secrets, role_cfg
    return load_secrets(role_cfg(ctx.cfg, "agent").get("api_env"), ctx.cfg.get("_config_dir"))


def contact_email(ctx: Ctx) -> str:
    return ctx.cfg.get("contact_email") or secrets(ctx).get("contact_email") or ""


def _download_oa(ctx: Ctx, rid: str, locs: list[dict]) -> Path | None:
    """Try the legal open-access copies (repository first). Publisher sites often refuse scripts;
    then the URLs are listed in the PDF request so a person can download them in a browser."""
    import requests
    d = fulltext_dir(ctx) / "auto"
    for l in locs:
        for url in [l.get("pdf"), l.get("url") if str(l.get("url", "")).lower().endswith(".pdf") else None]:
            if not url:
                continue
            try:
                r = requests.get(url, timeout=60, allow_redirects=True,
                                 headers={"User-Agent": f"sragent/0.2 (systematic review research; mailto:{contact_email(ctx)})"})
            except requests.RequestException:
                continue
            if r.status_code == 200 and r.content[:5] == b"%PDF-":
                d.mkdir(parents=True, exist_ok=True)
                p = d / f"{rid}.pdf"
                p.write_bytes(r.content)
                l["downloaded"] = True
                return p
            l["download_status"] = r.status_code
    return None


def _parse(ctx: Ctx, path: Path) -> tuple[str, str]:
    from ..docparse import parse_file
    try:
        return parse_file(path, ctx.cfg, secrets(ctx), llm=ctx.agent if ctx.cfg["pipeline"].get("vision_pages") else None,
                          log=ctx.log)
    except Exception as e:  # unreadable / scanned PDF
        ctx.log(f"[fulltext] {rid_of(path)}: could not read {path.name}: {str(e)[:120]}")
        return "", "failed"


def rid_of(p: Path) -> str:
    return p.stem.split("_supp")[0]


def fetch_one(ctx: Ctx, rid: str) -> dict:
    """Full text for one record: your file > Europe PMC open access (JATS) > a legal open-access PDF
    (Unpaywall / OpenAlex) > the abstract. The whole text is stored; prompts select from it (evidence.packed)."""
    from ..docparse import supplement_files
    st = ctx.store
    rec = st.get(f"rec:{rid}")["data"]
    maxc = int(ctx.cfg["pipeline"].get("max_stored_chars", 400000))
    local = local_file(ctx, rid)
    text, kind, pmcid, origin, parser = "", "abstract", rec.get("pmcid", ""), "abstract", ""
    parse_failed = False
    oa_locs: list[dict] = []
    listed_supp: list[dict] = []
    if local is not None:
        from .. import provenance
        dig = provenance.file_digest(local)
        if not any(e.get("sha256") == dig["sha256"] for e in provenance.events(ctx.run_dir)):
            provenance.record(ctx, "pdf_provision", "supplied", rid=rid, **dig)
        raw, parser = _parse(ctx, local)
        if len(raw) > 1500:
            text, kind, origin = raw, "fulltext", f"user file {local.name}"
        else:
            parse_failed = True
            ctx.log(f"[fulltext] {rid}: {local.name} has almost no extractable text (scanned PDF?)")
    if kind != "fulltext" and ctx.cfg["pipeline"].get("fulltext", True):
        try:
            if not pmcid:
                pmcid = europepmc.pmcid_for(rec.get("pmid", ""), rec.get("doi", ""))
            t = europepmc.fulltext(pmcid, maxc) if pmcid else ""
            if len(t) > 2000:
                text, kind, origin, parser = t, "fulltext", f"Europe PMC {pmcid}", "JATS XML"
                listed_supp = europepmc.LAST_SUPPLEMENTS.get(pmcid, [])
        except RuntimeError as e:
            ctx.log(f"[fulltext] {rid}: Europe PMC unavailable ({str(e)[:80]})")
    if kind != "fulltext" and ctx.cfg["pipeline"].get("fulltext", True) and ctx.cfg["pipeline"].get("open_access_lookup", True):
        from ..sources import openalex, unpaywall
        email = contact_email(ctx)
        oa_locs = unpaywall.locations(rec.get("doi", ""), email) if email else []
        if not oa_locs:
            oa_locs = openalex.oa_locations(openalex.work(rec.get("pmid", ""), rec.get("doi", "")))
        got = _download_oa(ctx, rid, oa_locs) if oa_locs else None
        if got is not None:
            raw, parser = _parse(ctx, got)
            if len(raw) > 1500:
                text, kind, origin = raw, "fulltext", f"open-access copy ({next((l['host'] for l in oa_locs if l.get('downloaded')), '')})"
    supp_used = []
    if kind == "fulltext":
        for sp in supplement_files(fulltext_dir(ctx), rid):
            raw, _ = _parse(ctx, sp)
            if raw.strip():
                text += f"\n\n## Supplement: {sp.name}\n" + raw
                supp_used.append(sp.name)
    if kind != "fulltext":
        text = f"{rec.get('title', '')}\n\n{rec.get('abstract', '')}"
    data = {"kind": kind, "origin": origin, "parser": parser, "pmcid": pmcid, "chars": len(text), "text": text[:maxc],
            "truncated": len(text) > maxc,
            "parse_failed": parse_failed and kind != "fulltext",
            "oa_locations": [{k: l.get(k) for k in ("url", "pdf", "host", "version", "license", "source", "download_status")}
                             for l in oa_locs][:5],
            "supplements_listed": listed_supp[:10], "supplements_used": supp_used,
            "local_file": local.name if local else None,
            "local_mtime": local.stat().st_mtime if local else None}
    st.upsert(f"ft:{rid}", "fulltext", data, depends_on=[f"rec:{rid}"], reason=f"retrieved ({origin}; {parser or 'abstract'})")
    return data


def needed_pdfs(ctx: Ctx) -> list[dict]:
    """Only the records whose full-text review is blocked: no full text obtained, or the supplied file
    could not be parsed (plus, in human-in-the-loop mode, undecided studies a person asked a PDF for)."""
    st = ctx.store
    out = []
    unavailable = set(st.meta.get("pdf_unavailable", []))
    for n in st.by_type("screen"):
        rid = n["id"].split(":", 1)[1]
        dec = n["data"]["decision"]
        if dec == "fulltext_unavailable":
            why, prio = "needed for the full-text eligibility review; no open-access copy was found", 1
        elif dec == "parse_failed":
            why, prio = "the supplied file has no usable text (scanned PDF?); please add a text PDF or a .txt", 1
        elif dec == "awaiting" and st.meta.get("pdf_wanted", {}).get(rid):
            why, prio = "a reviewer asked for another copy to decide eligibility", 2
        else:
            continue
        rec = st.get(f"rec:{rid}")["data"]
        oa = (st.get(f"ft:{rid}") or {}).get("data", {}).get("oa_locations") or []
        out.append({"rid": rid, "filename": f"{rid}.pdf", "priority": prio, "why": why, "status": dec,
                    "open_access_url": next((l.get("pdf") or l.get("url") for l in oa if l.get("pdf") or l.get("url")), ""),
                    "pmid": rec.get("pmid", ""), "doi": rec.get("doi", ""), "title": rec.get("title", ""),
                    "year": rec.get("year", ""), "journal": rec.get("journal", ""),
                    "present": local_file(ctx, rid) is not None, "declared_not_obtainable": rid in unavailable})
    return sorted(out, key=lambda r: (r["priority"], r["rid"]))


def write_needed_pdfs(ctx: Ctx) -> Path:
    rows = needed_pdfs(ctx)
    p = fulltext_dir(ctx) / "needed_pdfs.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "rid", "pmid", "doi", "status", "why", "open_access_url", "title", "year",
                                          "journal", "present", "declared_not_obtainable"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})
    return p


def new_local_files(ctx: Ctx) -> list[str]:
    """Record ids with a user file that has not been processed yet (new or changed)."""
    st = ctx.store
    out = []
    for n in st.by_type("record"):
        rid = n["id"].split(":", 1)[1]
        f = local_file(ctx, rid)
        if f is None:
            continue
        ft = (st.get(f"ft:{rid}") or {}).get("data", {})
        if ft.get("local_file") != f.name or (ft.get("local_mtime") or 0) < f.stat().st_mtime - 1:
            out.append(rid)
    return out
