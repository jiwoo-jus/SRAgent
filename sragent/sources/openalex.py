"""OpenAlex: open-access locations and the citation graph (references and citing works)."""
from __future__ import annotations

from . import http

BASE = "https://api.openalex.org"
MAILTO = {"mailto": "", "api_key": ""}


def configure(email: str = "", api_key: str = ""):
    MAILTO["mailto"] = email or ""
    MAILTO["api_key"] = api_key or ""


def _p(extra: dict | None = None) -> dict:
    p = {k: v for k, v in MAILTO.items() if v}
    p.update(extra or {})
    return p


def work(pmid: str = "", doi: str = "") -> dict | None:
    key = f"pmid:{pmid}" if pmid else f"doi:{doi}" if doi else ""
    if not key:
        return None
    try:
        return http.get(f"{BASE}/works/{key}", _p())
    except RuntimeError:
        return None


def oa_locations(w: dict | None) -> list[dict]:
    if not w:
        return []
    out = []
    for l in w.get("locations") or []:
        if l.get("is_oa") and (l.get("pdf_url") or l.get("landing_page_url")):
            src = (l.get("source") or {}).get("type")
            out.append({"pdf": l.get("pdf_url"), "url": l.get("landing_page_url"),
                        "host": "repository" if src == "repository" else "publisher", "version": l.get("version"),
                        "license": l.get("license"), "source": "openalex"})
    return out


def _short(wid: str) -> str:
    return wid.rsplit("/", 1)[-1]


def references(w: dict) -> list[str]:
    return [_short(x) for x in (w.get("referenced_works") or [])]


def works_by_ids(ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        d = http.get(f"{BASE}/works", _p({"filter": "openalex:" + "|".join(chunk), "per-page": 50})) or {}
        out += d.get("results") or []
    return out


def citing(w: dict, max_n: int = 400, until: str | None = None) -> list[dict]:
    flt = f"cites:{_short(w['id'])}"
    if until:
        flt += f",to_publication_date:{until}"
    out, cursor = [], "*"
    while cursor and len(out) < max_n:
        d = http.get(f"{BASE}/works", _p({"filter": flt, "per-page": 200, "cursor": cursor})) or {}
        out += d.get("results") or []
        cursor = (d.get("meta") or {}).get("next_cursor")
        if not d.get("results"):
            break
    return out[:max_n]


def abstract(w: dict) -> str:
    inv = w.get("abstract_inverted_index") or {}
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def to_record(w: dict) -> dict:
    ids = w.get("ids") or {}
    pmid = (ids.get("pmid") or "").rsplit("/", 1)[-1]
    doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
    auth = ", ".join(a["author"]["display_name"] for a in (w.get("authorships") or [])[:6] if a.get("author"))
    src = ((w.get("primary_location") or {}).get("source") or {})
    return {"pmid": pmid, "doi": doi, "title": w.get("title") or w.get("display_name") or "",
            "abstract": abstract(w), "year": str(w.get("publication_year") or ""), "journal": src.get("display_name") or "",
            "authors": auth, "pub_types": [w.get("type")] if w.get("type") else [], "openalex": _short(w.get("id", "")),
            "source": "openalex"}
