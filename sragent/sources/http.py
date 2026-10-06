"""Polite HTTP GET with on-disk caching and rate limiting (for public biomedical APIs)."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import requests

from ..llm import DiskCache

_cache: DiskCache | None = None
_lock = threading.Lock()
_last: dict[str, float] = {}
# minimal spacing between requests per host (seconds)
RATE = {"eutils.ncbi.nlm.nih.gov": 0.37, "rxnav.nlm.nih.gov": 0.06, "pubchem.ncbi.nlm.nih.gov": 0.21,
        "www.ebi.ac.uk": 0.1, "api.crossref.org": 0.1}
UA = "sragent/0.1 (systematic review research prototype)"


def init_cache(cache_dir: str | Path):
    global _cache
    _cache = DiskCache(Path(cache_dir) / "http_cache.sqlite")


def get(url: str, params: dict | None = None, *, as_json: bool = True, timeout: int = 40,
        retries: int = 3, use_cache: bool = True):
    key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
    if use_cache and _cache is not None:
        hit = _cache.get(key)
        if hit is not None:
            return hit["body"]
    host = url.split("/")[2]
    err = None
    for attempt in range(retries + 1):
        with _lock:
            wait = RATE.get(host, 0.05) - (time.time() - _last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            _last[host] = time.time()
        try:
            r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": UA})
            if r.status_code == 404:
                body = None
            elif r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code}")
            else:
                r.raise_for_status()
                body = r.json() if as_json else r.text
            if use_cache and _cache is not None:
                _cache.set(key, {"body": body})
            return body
        except (requests.RequestException, ValueError) as e:
            err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed {url} {params}: {err}")
