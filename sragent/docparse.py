"""Turn a full-text file into the plain, section-headed text the agent reads.

Parsers (pipeline.pdf_parser):
  azure_cu   Azure AI Content Understanding (api 2025-11-01). `prebuilt-documentSearch` returns markdown
             with section headings, tables (HTML) and, for figures, a generated description plus the
             plotted data where it can read it. `prebuilt-layout` gives text, tables and figure captions
             only (no generative model). Results are cached next to the file, so a document is analysed
             (and billed) once.
  pdftotext  local, free: poppler / pypdf text with "## Page N" markers. Tables lose their column
             structure; figures are not read. Optional `pipeline.vision_pages` sends page images that
             contain tables or figures to the agent's (multimodal) model for transcription.
  auto       azure_cu when its endpoint and key are configured, otherwise pdftotext.

Machine-read figure values are labelled as approximate in the text, so the agent cites them as such.
Supplements: files named <rid>_supp*.pdf|txt|md|csv in the full-text folder are appended as
"## Supplement: <file>" sections.
"""
from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path

import requests

from .provenance import file_sha

API_VERSION = "2025-11-01"


def azure_settings(cfg: dict, secrets: dict) -> dict | None:
    p = cfg.get("pipeline", {})
    ep = (secrets.get("azure_cu_endpoint") or secrets.get("AZURE_CONTENT_AI_ENDPOINT") or p.get("azure_cu_endpoint") or "").strip()
    key = (secrets.get("azure_cu_key") or secrets.get("AZURE_CONTENT_AI_API_KEY") or "").strip()
    if not ep or not key:
        return None
    return {"endpoint": ep.rstrip("/"), "key": key, "analyzer": p.get("azure_cu_analyzer", "prebuilt-documentSearch"),
            "api_version": secrets.get("azure_cu_api_version") or p.get("azure_cu_api_version") or API_VERSION}


def azure_analyze(path: Path, az: dict, log=print, timeout_s: int = 600) -> dict:
    cache = path.parent / ".parsed" / f"{path.name}.{file_sha(path)}.{az['analyzer']}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    url = f"{az['endpoint']}/contentunderstanding/analyzers/{az['analyzer']}:analyzeBinary?api-version={az['api_version']}"
    ctype = "application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream"
    r = requests.post(url, headers={"Ocp-Apim-Subscription-Key": az["key"], "Content-Type": ctype},
                      data=path.read_bytes(), timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"Azure Content Understanding {r.status_code}: {r.text[:300]}")
    loc = r.headers["Operation-Location"]
    t0 = time.time()
    while True:
        time.sleep(2)
        j = requests.get(loc, headers={"Ocp-Apim-Subscription-Key": az["key"]}, timeout=60).json()
        if j.get("status") in ("Succeeded", "Failed"):
            break
        if time.time() - t0 > timeout_s:
            raise RuntimeError("Azure Content Understanding timed out")
    if j.get("status") != "Succeeded":
        raise RuntimeError(f"Azure Content Understanding failed: {str(j.get('error'))[:300]}")
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(json.dumps(j))
    log(f"[parse] {path.name}: Azure {az['analyzer']} usage {j.get('usage')}")
    return j


_TABLE = re.compile(r"<table>(.*?)</table>", re.S | re.I)
_FIGRX = r'!\[[^\]]*\]\([^\s)]*(?:\s+"(?:[^"\\]|\\.)*")?\)'
_FIG = re.compile(r'!\[([^\]]*)\]\(([^\s)]*)(?:\s+"((?:[^"\\]|\\.)*)")?\)', re.S)
_CHART = re.compile(r"```(chart|mermaid)\n(.*?)```", re.S)


def _table_to_text(m: re.Match, cur: str) -> str:
    body = m.group(1)
    cap = re.search(r"<caption>(.*?)</caption>", body, re.S | re.I)
    capt = html.unescape(re.sub(r"<[^>]+>", " ", cap.group(1))).strip() if cap else "Table"
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", body, re.S | re.I):
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S | re.I)]
        rows.append(" | ".join(re.sub(r"\s+", " ", c) for c in cells))
    return f"\n## {capt[:120]}\n{capt}\n" + "\n".join(rows) + (f"\n\n## {cur}\n" if cur else "\n")


def cu_markdown_to_text(md: str) -> str:
    """Content Understanding markdown -> our text: tables become 'Table ...' sections with ' | ' rows,
    figures become 'Figure' sections with the machine description and data marked as approximate."""
    out, cur = [], ""
    md = md.replace("<!-- PageBreak -->", "")
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    pos = 0
    tokens = []
    for m in re.finditer(r"<table>.*?</table>|" + _FIGRX + r"|```(?:chart|mermaid)\n.*?```|^#+ [^\n]*$", md, re.S | re.M):
        tokens.append((md[pos:m.start()], None))
        tokens.append((None, m))
        pos = m.end()
    tokens.append((md[pos:], None))
    for txt, m in tokens:
        if txt is not None:
            out.append(txt)
            continue
        s = m.group(0)
        if s.startswith("#"):
            cur = s.lstrip("# ").strip()
            out.append(f"\n## {cur}\n")
        elif s.lower().startswith("<table"):
            out.append(_table_to_text(_TABLE.match(s), cur))
        elif s.startswith("!["):
            f = _FIG.match(s)
            alt, desc = (f.group(1) if f else ""), (f.group(3).replace('\\"', '"') if f and f.group(3) else "")
            out.append(f"\n## Figure (machine-read): {alt[:80]}\n"
                       f"Figure text: {alt}\n" + (f"Machine description of the figure (approximate): {desc}\n" if desc else "")
                       + (f"\n## {cur}\n" if cur else ""))
        else:
            c = _CHART.match(s)
            try:
                data = json.loads(c.group(2)) if c and c.group(1) == "chart" else None
            except ValueError:
                data = None
            if data:
                ds = data.get("data", {})
                series = "; ".join(f"{d.get('label', 'series')}: " + ", ".join(f"({x}, {y})" for x, y in zip(ds.get("labels", []), d.get("data", [])))
                                   for d in ds.get("datasets", []))
                out.append(f"\n## Figure data (machine-read, approximate)\nValues read from the plot (approximate, not reported numbers): {series}\n"
                           + (f"\n## {cur}\n" if cur else ""))
    t = html.unescape("".join(out))
    t = re.sub(r"\\([.\-*_#])", r"\1", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def vision_pages(path: Path, llm, max_pages: int = 6, log=print) -> str:
    """Optional fallback without Azure: page images with tables/figures -> transcription by the agent model."""
    import base64
    import shutil
    import subprocess
    import tempfile
    from .pdf import pdf_to_text
    if not shutil.which("pdftoppm"):
        return ""
    pages = re.split(r"\n## Page (\d+)\n", "\n" + pdf_to_text(path))
    want = []
    for i in range(1, len(pages) - 1, 2):
        if re.search(r"\b(Table|Fig\.?|Figure)\s*\d", pages[i + 1]):
            want.append(int(pages[i]))
    out = []
    with tempfile.TemporaryDirectory() as td:
        for pg in want[:max_pages]:
            subprocess.run(["pdftoppm", "-f", str(pg), "-l", str(pg), "-r", "110", "-png", str(path), f"{td}/p"],
                           check=True, capture_output=True, timeout=120)
            img = next(Path(td).glob("p*.png"))
            b64 = base64.b64encode(img.read_bytes()).decode()
            img.unlink()
            res = llm.chat([{"role": "user", "content": [
                {"type": "text", "text": "Transcribe every table on this page of a scientific paper row by row as 'cell | cell | cell', "
                                         "with its caption. For each figure, give its caption and the values that can be read from it, "
                                         "marked 'approximate'. Return JSON {\"tables\": [str], \"figures\": [str]}."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}], tag="vision_page")
            for t in res.get("tables", []) or []:
                out.append(f"\n## Table (page {pg}, transcribed from the page image)\n{t}\n")
            for f in res.get("figures", []) or []:
                out.append(f"\n## Figure (page {pg}, machine-read, approximate)\n{f}\n")
    if out:
        log(f"[parse] {path.name}: {len(out)} tables/figures transcribed from page images")
    return "".join(out)


def parse_file(path: Path, cfg: dict, secrets: dict, llm=None, log=print) -> tuple[str, str]:
    """-> (text, parser)."""
    from .pdf import pdf_to_text
    suf = path.suffix.lower()
    if suf in (".txt", ".md"):
        return path.read_text(errors="ignore"), "text file"
    if suf == ".csv":
        return "## Table: " + path.name + "\n" + path.read_text(errors="ignore").replace(",", " | "), "csv"
    mode = cfg.get("pipeline", {}).get("pdf_parser", "auto")
    az = azure_settings(cfg, secrets) if mode in ("auto", "azure_cu") else None
    if az:
        try:
            j = azure_analyze(path, az, log=log)
            md = "\n".join(c.get("markdown", "") for c in j["result"]["contents"])
            return cu_markdown_to_text(md), f"Azure Content Understanding ({az['analyzer']})"
        except Exception as e:  # fall back to the local parser
            log(f"[parse] {path.name}: Azure failed ({str(e)[:160]}); using pdftotext")
    text = pdf_to_text(path)
    if cfg.get("pipeline", {}).get("vision_pages") and llm is not None and len(text) > 1500:
        try:
            text += vision_pages(path, llm, int(cfg["pipeline"].get("vision_max_pages", 6)), log)
        except Exception as e:
            log(f"[parse] {path.name}: page-image reading failed ({str(e)[:120]})")
    return text, "pdftotext" + (" + page images" if cfg.get("pipeline", {}).get("vision_pages") else "")


def supplement_files(folder: Path, rid: str) -> list[Path]:
    return sorted(p for p in folder.glob(f"{rid}_supp*") if p.suffix.lower() in (".pdf", ".txt", ".md", ".csv") and p.stat().st_size)
