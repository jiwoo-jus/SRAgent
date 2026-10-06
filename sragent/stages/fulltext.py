"""Stage 3b: full texts.

Order of preference for a study with record id <rid> (the PMID whenever the record has one):
  1. a PDF you supplied        <run_dir>/fulltext/<rid>.pdf       (converted to text, page-marked)
  2. a text file you supplied  <run_dir>/fulltext/<rid>.txt
  3. Europe PMC open-access full text (JATS XML)
  4. the abstract (fallback)

`needed_pdfs.csv` in the same folder lists exactly which files would help, with the expected file
name, title, PMID and DOI, so PDFs can be collected in one go. Run `sragent pdfs` (or use the UI)
after adding files; it converts them and re-screens / re-extracts the affected studies.
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..context import Ctx
from ..pdf import pdf_to_text
from ..sources import europepmc


def fulltext_dir(ctx: Ctx) -> Path:
    d = ctx.run_dir / "fulltext"
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


def fetch_one(ctx: Ctx, rid: str) -> dict:
    st = ctx.store
    rec = st.get(f"rec:{rid}")["data"]
    maxc = int(ctx.cfg["pipeline"].get("max_fulltext_chars", 60000))
    local = local_file(ctx, rid)
    text, kind, pmcid, origin = "", "abstract", rec.get("pmcid", ""), "abstract"
    if local is not None:
        try:
            raw = pdf_to_text(local) if local.suffix.lower() == ".pdf" else local.read_text(errors="ignore")
        except Exception as e:  # unreadable / scanned PDF
            ctx.log(f"[fulltext] {rid}: could not read {local.name}: {e}")
            raw = ""
        if len(raw) > 1500:
            text, kind, origin = raw[:maxc], "fulltext", f"user file {local.name}"
        else:
            ctx.log(f"[fulltext] {rid}: {local.name} has almost no extractable text (scanned PDF?)")
    if kind != "fulltext" and ctx.cfg["pipeline"].get("fulltext", True):
        try:
            if not pmcid:
                pmcid = europepmc.pmcid_for(rec.get("pmid", ""), rec.get("doi", ""))
            t = europepmc.fulltext(pmcid, maxc) if pmcid else ""
            if len(t) > 2000:
                text, kind, origin = t, "fulltext", f"Europe PMC {pmcid}"
        except RuntimeError as e:
            ctx.log(f"[fulltext] {rid}: Europe PMC unavailable ({str(e)[:80]})")
    if kind != "fulltext":
        text = f"{rec.get('title', '')}\n\n{rec.get('abstract', '')}"
    data = {"kind": kind, "origin": origin, "pmcid": pmcid, "chars": len(text), "text": text,
            "local_file": local.name if local else None,
            "local_mtime": local.stat().st_mtime if local else None}
    st.upsert(f"ft:{rid}", "fulltext", data, depends_on=[f"rec:{rid}"], reason=f"retrieved ({origin})")
    return data


def fetch_all(ctx: Ctx) -> dict:
    st = ctx.store
    todo = []
    for n in st.by_type("screen"):
        rid = n["id"].split(":", 1)[1]
        if n["data"]["decision"] in ("include", "uncertain") and st.get(f"ft:{rid}") is None:
            todo.append(rid)
    res = [fetch_one(ctx, rid) for rid in todo]
    n_ft = sum(1 for r in res if r["kind"] == "fulltext")
    ctx.log(f"[fulltext] {n_ft}/{len(todo)} full texts available; others use the abstract")
    write_needed_pdfs(ctx)
    ctx.save()
    return {"requested": len(todo), "fulltext": n_ft}


def needed_pdfs(ctx: Ctx) -> list[dict]:
    """Records where a full text would change something: awaiting classification, or included but
    abstract-only (extraction would improve). Ordered by priority."""
    st = ctx.store
    out = []
    for n in st.by_type("screen"):
        rid = n["id"].split(":", 1)[1]
        dec = n["data"]["decision"]
        ft = (st.get(f"ft:{rid}") or {}).get("data", {})
        if ft.get("kind") == "fulltext":
            continue
        if dec == "awaiting":
            why, prio = "eligibility could not be decided from the abstract", 1
        elif dec == "include":
            why, prio = "included, but data were extracted from the abstract only", 2
        else:
            continue
        rec = st.get(f"rec:{rid}")["data"]
        out.append({"rid": rid, "filename": f"{rid}.pdf", "priority": prio, "why": why,
                    "pmid": rec.get("pmid", ""), "doi": rec.get("doi", ""), "title": rec.get("title", ""),
                    "year": rec.get("year", ""), "journal": rec.get("journal", ""),
                    "present": local_file(ctx, rid) is not None})
    return sorted(out, key=lambda r: (r["priority"], r["rid"]))


def write_needed_pdfs(ctx: Ctx) -> Path:
    rows = needed_pdfs(ctx)
    p = fulltext_dir(ctx) / "needed_pdfs.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "rid", "pmid", "doi", "title", "year", "journal", "why", "present"])
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
