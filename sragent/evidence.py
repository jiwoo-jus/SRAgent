"""Evidence as located sentences (cheap, trackable, never an impossible mission).

Design
  * Every source text (abstract or full text) is split once, deterministically, into numbered
    sentences  s1, s2, ...  Each sentence keeps its section heading and character offsets.
  * Prompts show the numbered text, and the model cites sentence ids for every claim
    (up to 4, anywhere in the paper; evidence is usually spread over several places).
  * Resolution is a lookup, not a search: a valid id IS an exact location.
    If a model returns text instead of ids (some local models do), the best-matching sentence
    is found with one fuzzy pass; nothing more is attempted.

Status per claim (soft; no claim is rejected for imperfect evidence)
  located   all cited ids resolved to sentences
  partial   some resolved
  none      a value was given but no location could be resolved   -> soft flag for the reviewer
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from functools import lru_cache

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover
    fuzz = None

_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?;])\s+(?=[\"'(\[]?[A-Z0-9])")
MAX_IDS = 8   # sentences a value may cite (the extractor is asked for up to 8; coverage may add up to that)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("‐", "-").replace("‑", "-").replace("–", "-").replace("—", "-")
    s = s.replace("−", "-").replace(" ", " ").replace("“", '"').replace("”", '"')
    s = s.replace("’", "'").replace("‘", "'").replace("µ", "μ")
    return _WS.sub(" ", s).strip().lower()


# ----------------------------------------------------------------------------- segmentation
@lru_cache(maxsize=512)
def _segment_cached(h: str, text: str) -> tuple:
    segs = []
    section = "Title/Abstract"
    pos = 0
    for line in text.split("\n"):
        start_line = text.find(line, pos)
        pos = start_line + len(line) if start_line >= 0 else pos
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            section = s.lstrip("# ").strip()[:60] or section
            continue
        m = re.match(r"^(BACKGROUND|INTRODUCTION|OBJECTIVES?|AIMS?|METHODS?|RESULTS|CONCLUSIONS?|PURPOSE|FINDINGS|"
                     r"INTERPRETATION|DESIGN|SETTING|PARTICIPANTS|INTERVENTIONS?|MEASUREMENTS?|FUNDING)\s*:\s*", s, re.I)
        if m:
            section = m.group(1).capitalize()
        off = start_line
        for piece in _SENT.split(s):
            piece = piece.strip()
            if not piece:
                continue
            st = text.find(piece, off)
            st = st if st >= 0 else off
            # merge fragments that are too short to stand alone (e.g. "n = 12).")
            if segs and len(piece) < 25 and segs[-1][1] == section and " | " not in piece and " | " not in segs[-1][4]:
                sid, sec, a, _, t = segs[-1]
                segs[-1] = (sid, sec, a, st + len(piece), t + " " + piece)
            else:
                segs.append((f"s{len(segs) + 1}", section, st, st + len(piece), piece))
            off = st + len(piece)
    return tuple(segs)


def segment(text: str) -> list[dict]:
    h = hashlib.sha1((text or "").encode()).hexdigest()
    return [{"sid": a, "section": b, "start": c, "end": d, "text": e}
            for a, b, c, d, e in _segment_cached(h, text or "")]


# ----------------------------------------------------------------------------- long sources
# Sections that never carry study data. They are left out of prompts (ids of other sentences unchanged).
_SKIP = re.compile(r"^(references?|bibliography|literature cited|acknowledg|funding|financial support|conflicts? of interest|"
                   r"competing interests?|disclosures?|declaration of (competing )?interests?|author(s'|s)? contributions?|"
                   r"contributions|data (sharing|availability)|abbreviations|orcid|correspondence|ethics (approval|statement)|"
                   r"consent for publication|publisher'?s note|supplementary (material|information|data)$)", re.I)
_PRIO = [(re.compile(r"abstract|title|summary", re.I), 0),
         (re.compile(r"table|figure|results?|pharmacokinetic|exposure|efficacy|safety|outcome|population pk|model|"
                     r"concentration|parameter|simulation|supplement", re.I), 1),
         (re.compile(r"method|patients|participants|subjects|study design|design|procedure|sampling|bioanaly|assay|"
                     r"statistic|dosing|treatment|intervention|eligib|population", re.I), 2),
         (re.compile(r"page \d+", re.I), 3),
         (re.compile(r"discussion|conclusion|limitation", re.I), 4),
         (re.compile(r"introduction|background", re.I), 5)]


def section_priority(name: str) -> int:
    for rx, p in _PRIO:
        if rx.search(name or ""):
            return p
    return 3


def _render(segs: list[dict], notes: list[str] | None = None) -> str:
    out, sec = [], None
    for s in segs:
        if s.get("gap"):
            out.append(f"[... {s['gap']} sentences of '{s['section']}' not shown here]")
            continue
        if s["section"] != sec:
            sec = s["section"]
            out.append(f"\n## {sec}")
        out.append(f"[{s['sid']}] {s['text']}")
    if notes:
        out.append("\n[Not shown: " + "; ".join(notes) + "]")
    return "\n".join(out).strip()


def kept_segments(text: str) -> tuple[list[dict], list[str]]:
    """Sentences minus non-data sections (references, acknowledgements, funding, ...)."""
    keep, dropped = [], []
    for s in segment(text):
        if _SKIP.match(s["section"].strip()):
            if s["section"] not in dropped:
                dropped.append(s["section"])
            continue
        keep.append(s)
    return keep, dropped


def packed(text: str, max_chars: int | None = None) -> str:
    """Numbered source for a prompt WITHOUT blind truncation.

    Non-data sections are dropped first. If the rest still exceeds max_chars, whole sections are kept
    by priority (abstract, results, tables, methods before discussion and introduction) and the
    remainder of a section that does not fit is marked as omitted. Original sentence ids are kept,
    so cited evidence always resolves against the full stored text."""
    segs, dropped = kept_segments(text)
    notes = [f"section '{d}' (no study data)" for d in dropped]
    total = sum(len(s["text"]) for s in segs)
    if not max_chars or total <= max_chars:
        return _render(segs, notes)
    order, groups = [], {}
    for s in segs:
        if s["section"] not in groups:
            order.append(s["section"])
            groups[s["section"]] = []
        groups[s["section"]].append(s)
    budget, chosen = max_chars, {}
    for sec in sorted(order, key=lambda x: (section_priority(x), order.index(x))):
        take = []
        for s in groups[sec]:
            if len(s["text"]) > budget:
                break
            take.append(s)
            budget -= len(s["text"])
        chosen[sec] = take
        if budget <= 0:
            break
    out = []
    for sec in order:
        take = chosen.get(sec, [])
        out += take
        missing = len(groups[sec]) - len(take)
        if missing:
            out.append({"gap": missing, "section": sec})
    return _render(out, notes)


def chunks(text: str, max_chars: int, overlap: int = 2) -> list[str]:
    """Consecutive numbered chunks (data sections only) for map-style extraction of long papers."""
    segs, _ = kept_segments(text)
    out, cur, size = [], [], 0
    for s in segs:
        if cur and size + len(s["text"]) > max_chars:
            out.append(_render(cur))
            cur = cur[-overlap:] if overlap else []
            size = sum(len(x["text"]) for x in cur)
        cur.append(s)
        size += len(s["text"])
    if cur:
        out.append(_render(cur))
    return out


def data_chars(text: str) -> int:
    return sum(len(s["text"]) for s in kept_segments(text)[0])


# ----------------------------------------------------------------------------- resolution
def _ids_from(cites) -> tuple[list[str], list[str]]:
    """Split model output into sentence ids and free-text quotes."""
    if cites is None:
        return [], []
    if isinstance(cites, str):
        cites = [cites]
    ids, texts = [], []
    for c in cites:
        c = str(c).strip()
        if not c:
            continue
        found = re.findall(r"\bs(\d+)\b", c, flags=re.I)
        if found and len(c) <= 40:
            ids += [f"s{x}" for x in found]
        elif re.fullmatch(r"\d+", c):
            ids.append(f"s{c}")
        else:
            texts.append(c)
    return list(dict.fromkeys(ids)), texts


def resolve(cites, text: str) -> dict:
    """cites: list of sentence ids (preferred) and/or quotes. Returns spans + soft status."""
    segs = segment(text)
    by_id = {s["sid"]: s for s in segs}
    ids, texts = _ids_from(cites)
    spans, asked = [], 0
    for i in ids[:MAX_IDS]:
        asked += 1
        if i in by_id:
            spans.append(by_id[i])
    for q in texts[: max(0, MAX_IDS - len(spans))]:
        asked += 1
        s = best_sentence(q, segs)
        if s and s["sid"] not in {x["sid"] for x in spans}:
            spans.append({**s, "matched_from_quote": True})
        elif s:
            asked -= 1
    if asked == 0:
        status = "none"
    elif len(spans) == asked:
        status = "located"
    elif spans:
        status = "partial"
    else:
        status = "none"
    return {"spans": [{k: s[k] for k in ("sid", "section", "start", "end", "text")} for s in spans],
            "check": {"status": status, "n": len(spans), "asked": asked}}


def best_sentence(quote: str, segs: list[dict], threshold: int = 80) -> dict | None:
    q = norm(quote)
    if not q or not segs:
        return None
    for s in segs:
        if q in norm(s["text"]):
            return s
    if fuzz is None:
        return None
    best, score = None, 0
    for s in segs:
        sc = fuzz.partial_ratio(q, norm(s["text"]))
        if sc > score:
            best, score = s, sc
    return best if score >= threshold else None


def joined(spans: list[dict], max_each: int = 400) -> str:
    """Evidence text for prompts and tables, with locations."""
    return " | ".join(f"[{s['sid']}, {s['section']}] {s['text'][:max_each]}" for s in spans)


def ids_of(spans: list[dict]) -> list[str]:
    return [s["sid"] for s in spans]


# ----------------------------------------------------------------------------- summary
def grounding_summary(items: list[dict]) -> dict:
    """items: evidence dicts with a `check` sub-dict (status located | partial | none)."""
    from collections import Counter
    c = Counter(it.get("check", {}).get("status", "none") for it in items)
    n = len(items)
    ok = c["located"] + c["partial"]
    out = dict(c)
    out["n"] = n
    out["located_rate"] = round(ok / n, 3) if n else None
    spans = [it.get("check", {}).get("n", 0) for it in items if "n" in it.get("check", {})]
    if spans:
        out["mean_locations_per_claim"] = round(sum(spans) / len(spans), 2)
        out["multi_location_rate"] = round(sum(1 for x in spans if x > 1) / len(spans), 3)
    return out


def attach(item: dict, cites, text: str, claim: str, src_node: str) -> dict:
    """Resolve `cites` against `text`, store the result on `item`, and return an evidence record.

    item gets: evidence (ids), spans (sid, section, start, end, text), quote (located text, for
    prompts/tables), check (soft status)."""
    r = resolve(cites, text)
    item["evidence"] = ids_of(r["spans"]) or ([str(c) for c in cites] if isinstance(cites, list) else [])
    item["spans"] = r["spans"]
    item["quote"] = joined(r["spans"])
    item["check"] = r["check"]
    return {"claim": claim, "source": src_node, "spans": r["spans"], "check": r["check"]}


def cites_of(d: dict) -> list:
    """What the model cited: sentence ids (or, from some local models, sentence text)."""
    ev = d.get("evidence")
    if not ev:
        return []
    return ev if isinstance(ev, list) else [ev]
