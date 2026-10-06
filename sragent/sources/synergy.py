"""SYNERGY benchmark loader (De Bruin et al., 2023; github.com/asreview/synergy-dataset).

SYNERGY gives, for 26 published systematic reviews, every record screened by the
original authors and the final inclusion label. We use it as gold standard for
screening, and the included papers as the study set for extraction/synthesis.

Metadata is resolved via PubMed (batch DOI lookup) with Crossref fallback, because
the OpenAlex free tier is IP-rate-limited on shared networks.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from . import crossref, http, pubmed

RAW = "https://raw.githubusercontent.com/asreview/synergy-dataset/master/datasets/{k}/{k}_ids.csv"


def rid_for(rec: dict) -> str:
    if rec.get("pmid"):
        return rec["pmid"]
    return "d" + hashlib.sha1((rec.get("doi") or rec.get("title", "")).encode()).hexdigest()[:10]


def load(key: str, out_path: str | Path, log=print) -> list[dict]:
    out_path = Path(out_path)
    if out_path.exists():
        return [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    text = http.get(RAW.format(k=key), as_json=False)
    rows = list(csv.DictReader(io.StringIO(text)))
    rows = [r for r in rows if r.get("doi") or r.get("pmid")]
    log(f"[synergy] {key}: {len(rows)} rows with an identifier")
    dois = [r["doi"].replace("https://doi.org/", "").lower() for r in rows if r.get("doi")]
    doi2pmid = pubmed.dois_to_pmids(dois)
    log(f"[synergy] resolved {len(doi2pmid)}/{len(dois)} DOIs to PubMed")
    pmids = sorted(set(doi2pmid.values()) | {r["pmid"].rstrip("/").split("/")[-1] for r in rows if r.get("pmid")})
    meta = {m["pmid"]: m for m in pubmed.efetch(pmids)}
    recs, seen = [], set()
    for r in rows:
        doi = r["doi"].replace("https://doi.org/", "").lower() if r.get("doi") else ""
        pmid = r["pmid"].rstrip("/").split("/")[-1] if r.get("pmid") else doi2pmid.get(doi, "")
        m = meta.get(pmid) if pmid else None
        if m is None and doi:
            try:
                m = crossref.by_doi(doi)
            except RuntimeError:
                m = None
        if m is None:
            continue
        m = dict(m)
        m["doi"] = m.get("doi") or doi
        m["label_included"] = int(r["label_included"])
        m["rid"] = rid_for(m)
        if m["rid"] in seen:
            continue
        seen.add(m["rid"])
        recs.append(m)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for m in recs:
            f.write(json.dumps(m) + "\n")
    n_inc = sum(m["label_included"] for m in recs)
    log(f"[synergy] wrote {len(recs)} records ({n_inc} included) -> {out_path}")
    return recs
