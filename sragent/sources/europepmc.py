"""Europe PMC: open-access full text (JATS XML -> plain text with section headers)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from . import http

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def pmcid_for(pmid: str = "", doi: str = "") -> str:
    q = f"EXT_ID:{pmid} AND SRC:MED" if pmid else f'DOI:"{doi}"'
    d = http.get(f"{BASE}/search", {"query": q, "format": "json", "resultType": "lite", "pageSize": 1}) or {}
    res = (d.get("resultList") or {}).get("result") or []
    if res and res[0].get("isOpenAccess") == "Y":
        return res[0].get("pmcid", "") or ""
    return res[0].get("pmcid", "") if res else ""


def _txt(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _walk(el, out: list[str], cur: list[str]):
    """JATS -> text. Tables and figures get their own section ('Table 2: caption'), placed where they
    occur, and the surrounding section heading is restored afterwards so sentence locations stay right."""
    tag = el.tag.split("}")[-1]
    if tag in {"ref-list", "fn-group", "ack", "license", "permissions", "funding-group", "back"}:
        return
    if tag == "sec":
        t = el.find("{*}title") if el.find("{*}title") is not None else el.find("title")
        title = _txt(t) if t is not None else ""
        if title:
            out.append(f"\n## {title}\n")
        cur.append(title or (cur[-1] if cur else ""))
        for ch in el:
            if ch.tag.split("}")[-1] != "title":
                _walk(ch, out, cur)
        cur.pop()
        if cur and cur[-1]:
            out.append(f"\n## {cur[-1]}\n")
        return
    if tag in {"table-wrap", "fig"}:
        lab = el.find(".//{*}label") if el.find(".//{*}label") is not None else el.find(".//label")
        cap = el.find(".//{*}caption") if el.find(".//{*}caption") is not None else el.find(".//caption")
        name = (_txt(lab) if lab is not None else ("Table" if tag == "table-wrap" else "Figure"))
        out.append(f"\n## {name}: {(_txt(cap) if cap is not None else '')[:120]}\n")
        if cap is not None:
            out.append(_txt(cap) + "\n")
        for tr in el.iter():
            if tr.tag.split("}")[-1] == "tr":
                cells = [_txt(c) for c in tr if c.tag.split("}")[-1] in ("td", "th")]
                if any(cells):
                    out.append(" | ".join(cells) + "\n")
        for fn in el.iter():
            if fn.tag.split("}")[-1] in ("table-wrap-foot",):
                out.append(_txt(fn) + "\n")
        if tag == "fig":
            out.append("(Figure image not read; only the caption is available.)\n")
        if cur and cur[-1]:
            out.append(f"\n## {cur[-1]}\n")
        return
    if tag == "supplementary-material":
        return
    if tag in {"p", "caption"}:
        t = _txt(el)
        if t:
            out.append(t + "\n")
        return
    for ch in el:
        _walk(ch, out, cur)


def supplements(root) -> list[dict]:
    out = []
    for sm in root.iter():
        if sm.tag.split("}")[-1] == "supplementary-material":
            href = next((v for k, v in sm.attrib.items() if k.endswith("href")), "")
            for m in sm.iter():
                if m.tag.split("}")[-1] == "media":
                    href = href or next((v for k, v in m.attrib.items() if k.endswith("href")), "")
            lab = next((_txt(x) for x in sm.iter() if x.tag.split("}")[-1] in ("label", "title")), "")
            out.append({"label": lab[:80], "file": href, "caption": _txt(sm)[:200]})
    return out


LAST_SUPPLEMENTS: dict[str, list] = {}
LAST_SOURCE: dict[str, str] = {}      # pmcid -> "Europe PMC" | "NCBI PMC (Europe PMC down)": where the article came from


NCBI_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def _efetch_xml(pmcid: str) -> str:
    """The same JATS article from NCBI's PMC (efetch): the fallback when Europe PMC is down."""
    from . import pubmed
    try:
        xml = http.get(NCBI_EFETCH, {"db": "pmc", "id": pmcid.replace("PMC", ""), "retmode": "xml", "tool": "sragent",
                                     **getattr(pubmed, "_extra", {})}, as_json=False)
    except RuntimeError:
        return ""
    return xml if xml and "<body" in xml else ""


def fulltext(pmcid: str, max_chars: int = 400000) -> str:
    if not pmcid:
        return ""
    try:
        xml = http.get(f"{BASE}/{pmcid}/fullTextXML", as_json=False)
        LAST_SOURCE[pmcid] = "Europe PMC"
    except RuntimeError as e:          # Europe PMC unavailable (server errors after retries): try NCBI's copy
        xml = _efetch_xml(pmcid)
        if not xml:
            raise RuntimeError(f"NCBI efetch gave no article either; {e}") from e
        LAST_SOURCE[pmcid] = "NCBI PMC (Europe PMC down)"
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    parts: list[str] = []
    title = root.find(".//article-title")
    if title is not None:
        parts.append("# " + "".join(title.itertext()).strip() + "\n")
    ab = root.find(".//abstract")
    if ab is not None:
        parts.append("\n## Abstract\n")
        _walk(ab, parts, ["Abstract"])
    body = root.find(".//body")
    if body is not None:
        _walk(body, parts, [""])
    fg = root.find(".//floats-group")   # tables / figures stored outside the body
    if fg is not None:
        _walk(fg, parts, [""])
    LAST_SUPPLEMENTS[pmcid] = supplements(root)
    text = re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()
    return text[:max_chars]
