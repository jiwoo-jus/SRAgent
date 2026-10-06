"""Crossref fallback for records not indexed in PubMed (e.g. conference abstracts)."""
from __future__ import annotations

import re

from . import http


def by_doi(doi: str) -> dict | None:
    d = http.get(f"https://api.crossref.org/works/{doi}")
    if not d or "message" not in d:
        return None
    m = d["message"]
    abstract = re.sub(r"<[^>]+>", " ", m.get("abstract", "") or "")
    abstract = re.sub(r"\s+", " ", abstract).strip()
    year = ""
    for k in ("published-print", "published-online", "issued"):
        if m.get(k, {}).get("date-parts"):
            year = str(m[k]["date-parts"][0][0])
            break
    return {"pmid": "", "doi": doi.lower(), "title": " ".join(m.get("title", []) or []),
            "abstract": abstract, "year": year, "journal": " ".join(m.get("container-title", []) or []),
            "pub_types": [m.get("type", "")], "mesh": [], "pmcid": "",
            "authors": ", ".join(f"{a.get('family', '')} {a.get('given', '')[:1]}".strip()
                                 for a in (m.get("author") or [])[:6]),
            "source": "crossref"}
