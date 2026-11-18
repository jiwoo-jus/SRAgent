"""Values that a paper shows only in a figure, estimated from the page image (rows items, sragent/rows.py).

A row extracted with source "figure" names its figure ("Fig 3a") and has no numbers: the text does not give them.
`read_study` renders the PDF page that carries each such figure, shows it to the extraction model with the rows of
that figure, and writes what it reads into the columns that were empty (or "not stated"). Every column filled this
way is listed in row["estimated"]: an estimate from a plot, never a reported number. It is shown as such on the
study page, exported in meta_rows.csv (`estimated_from_figure`), and can be left out of a sensitivity analysis.
Values a person entered and reported numbers are never touched. Studies whose text came without a PDF are skipped.
"""
from __future__ import annotations

import base64
import copy
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import rows

DPI = 170
MAX_PAGES = 2            # pages shown for one figure (its caption page first)
EMPTY = (None, "", "not stated")

PROMPT = """These page images are from a scientific paper (study {rid}) and show Figure {fig}.
The results below are shown in this figure only; the text gives no numbers for them. Read the values from the plot.

RESULTS (one per row; what is already known about each):
{rows}

For every row, estimate from the plot the columns listed under "to read":
{columns}

Return JSON: {{"rows": [{{"row": <row number>, "readable": true|false, "values": {{"<column>": "<value>", ...}},
                         "how": "<panel and bars or points used, axis and its unit, in one short sentence>"}}]}}
Rules:
- Find the panel and the bars, points or boxes that belong to the row (its groups, dose, condition, time point).
  If they cannot be matched with confidence, readable = false and values = {{}}.
- Read each value against the axis ticks, as precisely as the plot allows (interpolate between ticks; mind a
  logarithmic or broken axis). Give numbers only, in the axis unit; the unit goes in its own column if listed.
- An error bar: its length from the mean to the cap, in axis units, and what it is (SD, SEM, ...) only if the legend
  or caption says so.
- A group size only if the figure or its caption states it.
- Leave a column out when it cannot be read. Never take a number from memory or guess one the plot does not show."""


def pdf_of(ctx, rid: str) -> Path | None:
    """The study's PDF: a person's file, else the one the agent fetched."""
    from .stages.fulltext import fulltext_dir, local_file
    p = local_file(ctx, rid)
    if p is not None and p.suffix.lower() == ".pdf":
        return p
    a = fulltext_dir(ctx) / "auto" / f"{rid}.pdf"
    return a if a.exists() and a.stat().st_size > 0 else None


def figure_of(row: dict, cols: list[dict]) -> str | None:
    """The figure number a row names in one of its text columns ("Fig 3a", "Figure 2B and C" -> "3", "2")."""
    for c in cols:
        v = row.get(c["name"])
        if c["type"] == "text" and isinstance(v, str):
            m = re.search(r"\bfig(?:ure|\.)?\s*(S?\d+)", v, re.I)
            if m:
                return m.group(1).upper()
    return None


def page_texts(pdf: Path) -> list[str]:
    try:
        out = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"], capture_output=True, timeout=120,
                             check=True).stdout.decode("utf-8", "ignore")
    except (subprocess.SubprocessError, OSError):
        return []
    return out.split("\f")


def pages_of_figure(pages: list[str], fig: str) -> list[int]:
    """1-based pages that carry the figure: where its caption starts a line, else where it is first mentioned."""
    cap = re.compile(rf"^\s*f\s?i\s?g(?:\s?u\s?r\s?e|\.)?\s*{re.escape(fig)}\b(?!\s*[,)])", re.I | re.M)   # also "F I G U R E 3"
    any_ = re.compile(rf"\bfig(?:ure|\.)?\s*{re.escape(fig)}\b", re.I)
    hit = []
    for i, t in enumerate(pages, 1):
        m = cap.search(t)
        if m:
            hit.append(i)
            if i > 1 and m.start() < 0.2 * max(1, len(t)) and i - 1 not in hit:
                hit.append(i - 1)      # a caption at the top of a page: the plot may be on the page before
    return (hit or [i for i, t in enumerate(pages, 1) if any_.search(t)])[:MAX_PAGES]


def _images(pdf: Path, pages: list[int]) -> list[str]:
    out = []
    with tempfile.TemporaryDirectory() as td:
        for pg in pages:
            subprocess.run(["pdftoppm", "-f", str(pg), "-l", str(pg), "-r", str(DPI), "-png", str(pdf), f"{td}/p{pg}"],
                           check=True, capture_output=True, timeout=120)
            img = next(Path(td).glob(f"p{pg}*.png"))
            out.append(base64.b64encode(img.read_bytes()).decode())
    return out


def _wanted(row: dict, cols: list[dict]) -> list[dict]:
    """The columns of a figure row a plot can give: its empty number columns, and the empty text or "not stated"
    choice columns that say what those numbers are (unit, kind of value, kind of error bar)."""
    if not any(c["type"] == "number" and row.get(c["name"]) is None for c in cols):   # (a value kept as text counts as given)
        return []
    return [c for c in cols if row.get(c["name"]) in EMPTY and c["name"] not in row.get("estimated", [])]


def todo(field: dict, item: dict) -> dict[str, list[int]]:
    """{figure number: [row indexes]} of a rows item's figure-only rows that still lack numbers."""
    cols = rows.columns_of(field)
    src = next((c["name"] for c in cols if c["type"] == "choice" and "figure" in c["values"]), None)
    out: dict[str, list[int]] = {}
    if src is None or item.get("edited"):
        return out
    for i, r in enumerate(item.get("rows") or []):
        if r.get(src) == "figure" and not r.get("figure_read") and _wanted(r, cols):      # read once: not asked again
            fig = figure_of(r, cols)
            if fig:
                out.setdefault(fig, []).append(i)
    return out


def read_study(ctx, rid: str) -> dict:
    """Estimate the figure-only rows of one study. Returns {rows, filled, figures, skipped: reason or None}."""
    st = ctx.store
    ext = st.get(f"ext:{rid}")
    fields = [f for f in ctx.topic.get("extraction_fields") or [] if rows.is_rows(f)]
    if not ext or ext["status"] != "active" or not fields:
        return {"rid": rid, "rows": 0, "filled": 0, "figures": 0, "skipped": "no extraction with rows"}
    plan = {f["name"]: todo(f, (ext["data"]["fields"].get(f["name"]) or {})) for f in fields}
    n_rows = sum(len(v) for p in plan.values() for v in p.values())
    if not n_rows:
        return {"rid": rid, "rows": 0, "filled": 0, "figures": 0, "skipped": None}
    pdf = pdf_of(ctx, rid)
    if pdf is None or not shutil.which("pdftoppm"):
        return {"rid": rid, "rows": n_rows, "filled": 0, "figures": 0,
                "skipped": "no PDF of this study (its text came from PubMed Central); add <id>.pdf to read its figures"}
    pages = page_texts(pdf)
    data = copy.deepcopy(ext["data"])
    filled = figs = 0
    for f in fields:
        cols = rows.columns_of(f)
        by_name = {c["name"]: c for c in cols}
        got = data["fields"][f["name"]].get("rows") or []
        for fig, idx in plan[f["name"]].items():
            pgs = pages_of_figure(pages, fig)
            if not pgs:
                continue
            want = list(dict.fromkeys(c["name"] for i in idx for c in _wanted(got[i], cols)))
            col_txt = "\n".join(f"- {n}: " + ("one of " + " | ".join(by_name[n]["values"]) if by_name[n]["type"] == "choice"
                                              else "a number" if by_name[n]["type"] == "number" else "text")
                                + (f" ({by_name[n]['description']})" if by_name[n]["description"] else "") for n in want)
            row_txt = "\n".join(f"row {i + 1}: {rows.row_text(got[i], cols)}\n   to read: "
                                + ", ".join(c["name"] for c in _wanted(got[i], cols)) for i in idx)
            content = [{"type": "text", "text": PROMPT.format(rid=rid, fig=fig, rows=row_txt, columns=col_txt)}]
            content += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in _images(pdf, pgs)]
            out = ctx.writer.chat([{"role": "user", "content": content}], tag="extract_figures")
            figs += 1
            for x in out.get("rows") or []:
                try:
                    i = int(x.get("row")) - 1
                except (TypeError, ValueError):
                    continue
                if i not in idx or not x.get("readable") or not isinstance(x.get("values"), dict):
                    continue
                r = got[i]
                allowed = {c["name"] for c in _wanted(r, cols)}
                new = rows.clean(f, [{k: v for k, v in x["values"].items() if k in allowed}])
                took = [k for k in allowed if new and new[0].get(k) is not None and k not in (new[0].get("off_list") or [])]
                if not any(by_name[k]["type"] == "number" for k in took):
                    continue
                for k in took:
                    r[k] = new[0][k]
                r["estimated"] = sorted(set(r.get("estimated", [])) | set(took))
                r["figure_read"] = {"figure": fig, "pages": pgs, "how": str(x.get("how") or "")[:300]}
                filled += 1
        data["fields"][f["name"]]["value"] = rows.summary(f, got)
    if filled:
        st.write(ext["id"], "extraction", data, depends_on=ext["depends_on"], evidence=ext["evidence"],
                 reason=f"{filled} row(s) estimated from {figs} figure(s) (approximate, marked)")
    return {"rid": rid, "rows": n_rows, "filled": filled, "figures": figs, "skipped": None}


def pending(ctx, rids: list[str] | None = None) -> dict:
    """What `read_all` would do, without doing it: {rows, figures, studies, no_pdf: [study ids]}. No model calls."""
    st = ctx.store
    fields = [f for f in ctx.topic.get("extraction_fields") or [] if rows.is_rows(f)]
    out = {"rows": 0, "figures": 0, "studies": 0, "no_pdf": []}
    for rid in rids or st.included_ids():
        ext = st.get(f"ext:{rid}")
        if not ext or ext["status"] != "active":
            continue
        plans = [todo(f, (ext["data"]["fields"].get(f["name"]) or {})) for f in fields]
        n = sum(len(v) for p in plans for v in p.values())
        if not n:
            continue
        if pdf_of(ctx, rid) is None:
            out["no_pdf"].append(rid)
            continue
        out["rows"] += n
        out["figures"] += sum(len(p) for p in plans)
        out["studies"] += 1
    return out


def read_all(ctx, rids: list[str] | None = None) -> dict:
    st = ctx.store
    todo_ids = [r for r in (rids or st.included_ids()) if (st.get(f"ext:{r}") or {}).get("status") == "active"]
    res = ctx.map(lambda r: read_study(ctx, r), todo_ids, label="Reading figures")
    ctx.save()
    return {"studies": len(todo_ids), "rows": sum(x["rows"] for x in res), "filled": sum(x["filled"] for x in res),
            "figures": sum(x["figures"] for x in res), "skipped": {x["rid"]: x["skipped"] for x in res if x["skipped"]}}
