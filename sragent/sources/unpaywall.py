"""Unpaywall: legal open-access copies of an article (publisher or repository), by DOI.
Requires a contact email (Unpaywall's terms)."""
from __future__ import annotations

from . import http

BASE = "https://api.unpaywall.org/v2/"


def locations(doi: str, email: str) -> list[dict]:
    if not doi or not email:
        return []
    try:
        d = http.get(BASE + doi, {"email": email}) or {}
    except RuntimeError:
        return []
    out = []
    for l in d.get("oa_locations") or []:
        out.append({"pdf": l.get("url_for_pdf"), "url": l.get("url_for_landing_page") or l.get("url"),
                    "host": l.get("host_type"), "version": l.get("version"), "license": l.get("license"),
                    "source": "unpaywall"})
    # repositories first (they usually allow direct download), then publishers
    return sorted(out, key=lambda x: (x["host"] != "repository", not x["pdf"]))
