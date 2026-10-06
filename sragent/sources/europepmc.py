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


def _walk(el, out: list[str], depth=0):
    tag = el.tag.split("}")[-1]
    if tag in {"ref-list", "xref", "fn-group", "ack", "license", "permissions", "funding-group"}:
        if tag != "xref":
            return
    if tag == "title" and depth > 0:
        t = "".join(el.itertext()).strip()
        if t:
            out.append(f"\n## {t}\n")
        return
    if tag in {"p", "td", "th", "caption"}:
        t = re.sub(r"\s+", " ", "".join(el.itertext())).strip()
        if t:
            out.append(t + ("\n" if tag != "td" and tag != "th" else " | "))
        return
    if tag == "tr":
        out.append("\n")
    for ch in el:
        _walk(ch, out, depth + 1)


def fulltext(pmcid: str, max_chars: int = 60000) -> str:
    if not pmcid:
        return ""
    xml = http.get(f"{BASE}/{pmcid}/fullTextXML", as_json=False)
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
        _walk(ab, parts, 1)
    body = root.find(".//body")
    if body is not None:
        _walk(body, parts, 0)
    # tables/figure captions in floats-group
    fg = root.find(".//floats-group")
    if fg is not None:
        parts.append("\n## Tables and figures\n")
        _walk(fg, parts, 1)
    text = re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()
    return text[:max_chars]
