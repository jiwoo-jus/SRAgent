"""Term validation against external biomedical databases.

  drugs     RxNorm (NLM RxNav), PubChem (PUG-REST), ChEMBL (EBI)
  diseases  MONDO (via EBI OLS4)

Used in three places:
  1. planning   validate PICO concepts and harvest synonyms/brand names for the search query
  2. extraction normalise intervention/condition strings to database identifiers
  3. report     unvalidated terms are shown as flags for the human reviewer

The result for each term:
  {"term", "kind", "status": validated|approximate|not_found,
   "preferred": str, "ids": {...}, "synonyms": [...], "sources": {db: detail}}
"""
from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import quote

from .sources import http

RXNAV = "https://rxnav.nlm.nih.gov/REST"
PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
CHEMBL = "https://www.ebi.ac.uk/chembl/api/data"
OLS = "https://www.ebi.ac.uk/ols4/api"


def _n(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _similar(a: str, b: str, cutoff: int = 80) -> bool:
    try:
        from rapidfuzz import fuzz
        return fuzz.token_set_ratio(_n(a), _n(b)) >= cutoff
    except ImportError:
        return _n(a) in _n(b) or _n(b) in _n(a)


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # network errors must not kill the pipeline
        return {"error": str(e)[:200]}


# ------------------------------------------------------------------ drugs
def rxnorm(term: str) -> dict:
    d = http.get(f"{RXNAV}/rxcui.json", {"name": term, "search": 2}) or {}
    ids = (d.get("idGroup") or {}).get("rxnormId") or []
    match = "exact" if ids else None
    if not ids:
        a = http.get(f"{RXNAV}/approximateTerm.json", {"term": term, "maxEntries": 3}) or {}
        cands = (a.get("approximateGroup") or {}).get("candidate") or []
        cands = [c for c in cands if c.get("rxcui")]
        if cands and float(cands[0].get("score", 0)) >= 8:
            ids, match = [cands[0]["rxcui"]], "approximate"
    if not ids:
        return {"found": False}
    cui = ids[0]
    p = (http.get(f"{RXNAV}/rxcui/{cui}/properties.json") or {}).get("properties") or {}
    # brand names / ingredients related to this concept (for search synonyms)
    rel = http.get(f"{RXNAV}/rxcui/{cui}/related.json", {"tty": "IN BN"}) or {}
    syn = []
    for g in (rel.get("relatedGroup") or {}).get("conceptGroup") or []:
        for cp in g.get("conceptProperties") or []:
            syn.append(cp["name"])
    name = p.get("name", "")
    if match == "approximate" and _n(name) == _n(term):
        match = "exact"
    return {"found": True, "rxcui": cui, "name": name, "tty": p.get("tty"), "match": match,
            "synonyms": sorted(set(syn))}


def pubchem(term: str) -> dict:
    d = http.get(f"{PUBCHEM}/compound/name/{quote(term)}/cids/JSON")
    cids = ((d or {}).get("IdentifierList") or {}).get("CID") or []
    if cids:
        s = http.get(f"{PUBCHEM}/compound/cid/{cids[0]}/synonyms/JSON") or {}
        syn = (((s.get("InformationList") or {}).get("Information") or [{}])[0].get("Synonym") or [])[:15]
        return {"found": True, "cid": cids[0], "record": "compound", "synonyms": syn, "match": "exact"}
    # biologics (e.g. monoclonal antibodies) are often only PubChem *substances*
    d = http.get(f"{PUBCHEM}/substance/name/{quote(term)}/sids/JSON")
    info = ((d or {}).get("InformationList") or {}).get("Information") or []
    sids = [s for i in info for s in i.get("SID", [])] or ((d or {}).get("IdentifierList") or {}).get("SID") or []
    if sids:
        return {"found": True, "sid": sids[0], "record": "substance", "n_sids": len(sids), "match": "exact"}
    return {"found": False}


def chembl(term: str) -> dict:
    d = http.get(f"{CHEMBL}/molecule/search.json", {"q": term, "limit": 5}) or {}
    mols = d.get("molecules") or []
    for m in mols:
        names = {_n(m.get("pref_name") or "")}
        for s in m.get("molecule_synonyms") or []:
            names.add(_n(s.get("molecule_synonym", "")))
        if _n(term) in names:
            return {"found": True, "chembl_id": m["molecule_chembl_id"], "name": m.get("pref_name"),
                    "max_phase": m.get("max_phase"), "type": m.get("molecule_type"), "match": "exact",
                    "synonyms": sorted({s.get("molecule_synonym") for s in m.get("molecule_synonyms") or []})[:15]}
    if mols and _similar(term, mols[0].get("pref_name") or ""):
        m = mols[0]
        return {"found": True, "chembl_id": m["molecule_chembl_id"], "name": m.get("pref_name"),
                "max_phase": m.get("max_phase"), "type": m.get("molecule_type"), "match": "approximate"}
    return {"found": False}


# ------------------------------------------------------------------ diseases
def mondo(term: str) -> dict:
    d = http.get(f"{OLS}/search", {"q": term, "ontology": "mondo", "rows": 8,
                                   "fieldList": "obo_id,label,synonym,description",
                                   "queryFields": "label,synonym"}) or {}
    docs = (d.get("response") or {}).get("docs") or []
    docs = [x for x in docs if str(x.get("obo_id", "")).startswith("MONDO:")]
    if not docs:
        return {"found": False}
    t = _n(term)
    for x in docs:
        names = {_n(x.get("label", ""))} | {_n(s) for s in x.get("synonym") or []}
        if t in names:
            return {"found": True, "mondo_id": x["obo_id"], "name": x.get("label"), "match": "exact",
                    "synonyms": (x.get("synonym") or [])[:15]}
    x = docs[0]
    if not any(_similar(term, n) for n in [x.get("label", "")] + list(x.get("synonym") or [])):
        return {"found": False, "nearest": x.get("label")}
    return {"found": True, "mondo_id": x["obo_id"], "name": x.get("label"), "match": "approximate",
            "synonyms": (x.get("synonym") or [])[:15]}


# ------------------------------------------------------------------ public API
@lru_cache(maxsize=4096)
def validate(term: str, kind: str) -> dict:
    term = (term or "").strip()
    res = {"term": term, "kind": kind, "status": "not_found", "preferred": None, "ids": {},
           "synonyms": [], "sources": {}}
    if not term or term.lower() in {"nr", "not reported", "none", "n/a", "na"}:
        res["status"] = "skipped"
        return res
    if kind == "drug":
        src = {"rxnorm": _safe(rxnorm, term), "pubchem": _safe(pubchem, term), "chembl": _safe(chembl, term)}
        res["sources"] = src
        exact = [k for k, v in src.items() if v.get("found") and v.get("match") == "exact"]
        anyf = [k for k, v in src.items() if v.get("found")]
        if src["rxnorm"].get("found"):
            res["ids"]["rxcui"] = src["rxnorm"]["rxcui"]
            res["preferred"] = src["rxnorm"].get("name")
        if src["pubchem"].get("found"):
            res["ids"]["pubchem"] = (f"CID:{src['pubchem']['cid']}" if src["pubchem"].get("cid")
                                     else f"SID:{src['pubchem'].get('sid')}")
        if src["chembl"].get("found"):
            res["ids"]["chembl"] = src["chembl"]["chembl_id"]
            res["preferred"] = res["preferred"] or src["chembl"].get("name")
        syn = set(src["rxnorm"].get("synonyms", []) or []) | set(src["chembl"].get("synonyms", []) or [])
        res["synonyms"] = sorted(s for s in syn if s and len(s) < 60)[:20]
        res["status"] = "validated" if exact else ("approximate" if anyf else "not_found")
        res["n_db_exact"] = len(exact)
    elif kind == "disease":
        m = _safe(mondo, term)
        res["sources"] = {"mondo": m}
        if m.get("found"):
            res["ids"]["mondo"] = m["mondo_id"]
            res["preferred"] = m.get("name")
            res["synonyms"] = m.get("synonyms", [])
            res["status"] = "validated" if m.get("match") == "exact" else "approximate"
    else:
        res["status"] = "skipped"
    return res


def split_terms(value: str) -> list[str]:
    """Split a free-text field like 'emicizumab vs. FVIII prophylaxis' into candidate terms."""
    if not value:
        return []
    parts = re.split(r";|,|\bvs\.?\b|\bversus\b|\band\b|\bplus\b|\+|/|\(|\)", value)
    out = []
    for p in parts:
        p = re.sub(r"\b\d+(\.\d+)?\s*(mg/kg|mg|µg|ug|iu|iu/kg|q\d+w|qw|weekly|every \d+ weeks?)\b.*", "", p,
                   flags=re.I).strip(" .-")
        if 2 < len(p) < 60:
            out.append(p)
    return out


_STEMS = re.compile(r"\b([A-Za-z][A-Za-z\-]{3,}(?:mab|cept|nib|parin|cog|vec|kinase|ase|vir|statin|pril|sartan|olol|azole|mycin|cillin|platin|taxel|tide))\b", re.I)
_SPECIAL = re.compile(r"\b(factor VIII|factor IX|factor VIIa|rFVIIa|FVIII|FEIBA|aPCC|bypassing agents?|tranexamic acid|"
                      r"recombinant factor VIIa|activated prothrombin complex concentrates?|placebo)\b", re.I)
_NOISE = re.compile(r"\d|\b(dose|doses|loading|maintenance|weekly|week|weeks|cohort|group|kg|mg|every|once|single|"
                    r"subcutaneous|intravenous|regimen|NR|not reported|none|no |control|historical|arm)\b", re.I)


def candidate_terms(value: str, kind: str, known: list[str]) -> list[str]:
    """Entity candidates from a free-text value when the extractor did not list entities."""
    if not value:
        return []
    out = []
    low = value.lower()
    for k in sorted(known, key=len, reverse=True):
        if k and len(k) > 3 and re.search(rf"(?<![\w-]){re.escape(k.lower())}(?![\w-])", low):
            out.append(k)
    if kind == "drug":
        out += [m.group(1) for m in _STEMS.finditer(value)]
        out += [m.group(1) for m in _SPECIAL.finditer(value)]
    short_val = value.strip().rstrip(".")
    if not out and len(short_val.split()) <= 5 and not _NOISE.search(short_val):
        out.append(short_val)
    # de-duplicate case-insensitively, prefer first occurrence
    seen, res = set(), []
    for t in out:
        if t.lower() not in seen and t.lower() not in ("placebo",):
            seen.add(t.lower())
            res.append(t)
    return res
