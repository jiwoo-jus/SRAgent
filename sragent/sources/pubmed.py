"""PubMed via NCBI E-utilities: search, fetch, DOI lookup."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from . import http

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_extra: dict = {}


def configure(email: str = "", api_key: str = ""):
    _extra.clear()
    if email:
        _extra["email"] = email
    if api_key:
        _extra["api_key"] = api_key


def esearch(term: str, retmax: int = 500, mindate: str | None = None, maxdate: str | None = None) -> dict:
    p = {"db": "pubmed", "term": term, "retmax": retmax, "retmode": "json", "tool": "sragent", **_extra}
    if mindate or maxdate:
        p.update({"datetype": "pdat", "mindate": mindate or "1900", "maxdate": maxdate or "3000"})
    d = http.get(f"{EUTILS}/esearch.fcgi", p) or {}
    r = d.get("esearchresult", {})
    return {"count": int(r.get("count", 0)), "pmids": r.get("idlist", []),
            "translation": r.get("querytranslation", "")}


def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def efetch(pmids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(pmids), 150):
        chunk = pmids[i:i + 150]
        xml = http.get(f"{EUTILS}/efetch.fcgi", {"db": "pubmed", "id": ",".join(chunk), "retmode": "xml",
                                                 "tool": "sragent", **_extra}, as_json=False)
        if not xml:
            continue
        root = ET.fromstring(xml)
        for art in root.findall(".//PubmedArticle"):
            pmid = _text(art.find(".//MedlineCitation/PMID"))
            title = _text(art.find(".//ArticleTitle"))
            parts = []
            for ab in art.findall(".//Abstract/AbstractText"):
                lab = ab.get("Label")
                parts.append(f"{lab}: {_text(ab)}" if lab else _text(ab))
            ids = {a.get("IdType"): _text(a) for a in art.findall("./PubmedData/ArticleIdList/ArticleId")}
            if not ids.get("doi"):
                for el in art.findall(".//Article/ELocationID"):
                    if el.get("EIdType") == "doi":
                        ids["doi"] = _text(el)
            year = _text(art.find(".//JournalIssue/PubDate/Year")) or _text(art.find(".//JournalIssue/PubDate/MedlineDate"))[:4]
            out.append({
                "pmid": pmid, "title": title, "abstract": "\n".join(parts),
                "doi": (ids.get("doi") or "").lower(), "pmcid": ids.get("pmc", ""),
                "year": year, "journal": _text(art.find(".//Journal/Title")),
                "pub_types": [_text(p) for p in art.findall(".//PublicationType")],
                "mesh": [_text(m.find("DescriptorName")) for m in art.findall(".//MeshHeading")],
                "authors": ", ".join(f"{_text(a.find('LastName'))} {_text(a.find('Initials'))}".strip()
                                     for a in art.findall(".//AuthorList/Author")[:6]),
                "source": "pubmed",
            })
    return out


def dois_to_pmids(dois: list[str]) -> dict[str, str]:
    """Batch DOI -> PMID lookup (OR-ed [doi] queries)."""
    out: dict[str, str] = {}
    dois = [d.lower() for d in dois if d]
    for i in range(0, len(dois), 40):
        chunk = dois[i:i + 40]
        term = " OR ".join(f'"{d}"[doi]' for d in chunk)
        res = esearch(term, retmax=200)
        if not res["pmids"]:
            continue
        for rec in efetch(res["pmids"]):
            if rec["doi"] in chunk:
                out[rec["doi"]] = rec["pmid"]
    return out
