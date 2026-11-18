"""Extraction items that hold rows (`kind: rows`): one row per result, for a quantitative synthesis.

A topic field

  - name: outcome_data
    kind: rows
    description: "One row per outcome x comparison x dose x time point."
    columns:
      outcome_family: [sFlt-1, PlGF, invasion, other]      # a list: one of these values
      n_aspirin: number                                     # number | text
      challenge: {type: text, description: "none, hypoxia, H2O2, preeclampsia serum"}

is extracted as {"value": <summary written by the code>, "rows": [{<column>: value or null, "evidence": [ids],
"note": str, "check"/"spans"/"quote": the located sentences, "uncovered": [numbers not found as written]}]}.
The model copies numbers as the paper writes them and computes nothing; a result shown only in a figure is a row
with source "figure" and no numbers. The summary keeps every reader of `value` working (risk of bias, the report);
the synthesis and the out-of-date check read the rows too. A figure-only row can get its numbers from the page image
afterwards (sragent/figures.py): those columns are listed in row["estimated"]. `export_csv` writes every row of every included study
to <run_dir>/meta_rows.csv for the analysis.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

TYPES = ("choice", "number", "text")
ROW_KEYS = ("evidence", "note")          # what every row carries beside its columns


def is_rows(field: dict) -> bool:
    return field.get("kind") == "rows"


def columns_of(field: dict) -> list[dict]:
    """The columns of a rows field as [{name, type, values, description}], from the short or the long form."""
    out = []
    for name, spec in (field.get("columns") or {}).items():
        if isinstance(spec, list):
            c = {"type": "choice", "values": [str(v) for v in spec], "description": ""}
        elif isinstance(spec, str):
            c = {"type": spec, "values": [], "description": ""}
        elif isinstance(spec, dict):
            vals = [str(v) for v in spec.get("values") or []]
            c = {"type": spec.get("type") or ("choice" if vals else "text"), "values": vals,
                 "description": str(spec.get("description") or "")}
        else:
            c = {"type": None, "values": [], "description": ""}
        out.append({"name": str(name), **c})
    return out


def check_field(field: dict) -> list[str]:
    """Problems with a rows field's definition (empty: fine)."""
    cols = columns_of(field)
    if not cols:
        return [f"extraction field {field.get('name')}: kind rows needs columns"]
    errs = []
    for c in cols:
        if c["type"] not in TYPES:
            errs.append(f"extraction field {field.get('name')}: column {c['name']} is a list of values, number or text")
        elif c["type"] == "choice" and not c["values"]:
            errs.append(f"extraction field {field.get('name')}: column {c['name']} lists no values")
        if c["name"] in ROW_KEYS:
            errs.append(f"extraction field {field.get('name')}: column name {c['name']} is reserved")
    return errs


def prompt_text(field: dict) -> str:
    """The item's lines in the extraction prompt (only reviews with a rows field see them)."""
    L = [f"- {field['name']} [rows]: {field['description']}",
         "    For this item give {\"rows\": [...], \"note\": \"\"} instead of value and evidence: one object per result,",
         "    with these keys, each a string (\"\" when the paper does not report it):"]
    for c in columns_of(field):
        t = "one of " + " | ".join(c["values"]) if c["type"] == "choice" else "a number as written" if c["type"] == "number" else "text"
        L.append(f"      {c['name']}: {t}" + (f" ({c['description']})" if c["description"] else ""))
    L += ["      evidence: at most 2 sentence ids where the row's numbers are, or [] (optional: the numbers are found",
          "        in the text automatically); note: usually empty",
          "    The data are what matters here. Give every result the paper reports, completely: each comparison, dose,",
          "    time point and outcome is its own row, and so is each analyte, even when the paper reports several in",
          "    one sentence (\"VEGF, PlGF and sFlt-1 were unchanged\" is three rows). Never drop, shorten or merge a row because no sentence can be",
          "    cited for it, and spend no effort on choosing sentences. The limits on length and on citations of the",
          "    other items do not apply to rows.",
          "    Copy every number exactly as the paper writes it, with its own unit column if there is one; compute,",
          "    convert or round nothing (no SEM to SD, no percentages from counts). A result shown only in a figure",
          "    is a row with source \"figure\" and no numbers read off the plot. No row for a result the paper does",
          "    not report."]
    return "\n".join(L)


def schema(field: dict) -> dict:
    """The JSON shape of a rows item (structured output). Every column is a string, "" when not reported: the API
    allows at most 16 union-typed (nullable) parameters per request, and a number is copied as written anyway;
    `clean` turns it into a number."""
    from .schemas import STR, _EVIDENCE, _list, _obj
    props = {c["name"]: {"type": "string", "enum": c["values"] + [""]} if c["type"] == "choice" else STR
             for c in columns_of(field)}
    return _obj(rows=_list(_obj(**props, evidence=_EVIDENCE, note=STR)), note=STR)


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(",", "").replace("·", ".").replace("−", "-")
    try:
        return float(s) if any(ch in s for ch in ".eE") else int(s)
    except ValueError:
        return None


def clean(field: dict, raw) -> list[dict]:
    """The model's (or a person's) rows, in the field's columns: numbers as numbers, a value off a choice list kept
    and marked (`off_list`), unknown keys dropped."""
    cols = columns_of(field)
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        row, off = {}, []
        for c in cols:
            v = r.get(c["name"])
            if c["type"] == "number":
                n = _num(v)
                if n is None and str(v if v is not None else "").strip() not in ("", "NR", "null"):
                    off.append(c["name"])          # not one number ("75-150", "10^-4"): kept as written and marked
                    n = str(v).strip()
                row[c["name"]] = n
            elif v is None or str(v).strip() in ("", "NR", "null"):
                row[c["name"]] = None
            else:
                s = str(v).strip()
                if c["type"] == "choice":
                    hit = next((x for x in c["values"] if x.lower() == s.lower()), None)
                    if hit is None:
                        off.append(c["name"])
                    s = hit or s
                row[c["name"]] = s
        if all(row[c["name"]] is None for c in cols):
            continue
        row["evidence"] = [str(x) for x in r.get("evidence") or [] if isinstance(x, (str, int))]
        if isinstance(r.get("note"), str) and r["note"].strip():
            row["note"] = r["note"].strip()[:300]
        if off:
            row["off_list"] = off
        if isinstance(r.get("estimated"), list) and r["estimated"]:    # columns read from a figure (sragent/figures.py)
            row["estimated"] = [str(x) for x in r["estimated"] if x in row]
            if isinstance(r.get("figure_read"), dict):
                row["figure_read"] = r["figure_read"]
        out.append(row)
    return out


def summary(field: dict, rows: list[dict]) -> str:
    """The item's value: how many rows, by the first choice column, and how many have no numbers."""
    if not rows:
        return "NR"
    cols = columns_of(field)
    key = next((c["name"] for c in cols if c["type"] == "choice"), None)
    numeric = [c["name"] for c in cols if c["type"] == "number"]
    bare = sum(1 for r in rows if not any(isinstance(r.get(n), (int, float)) for n in numeric))
    s = f"{len(rows)} row{'s' if len(rows) != 1 else ''}"
    if key:
        s += ": " + ", ".join(f"{k} ×{n}" if n > 1 else k for k, n in Counter(r.get(key) or "unspecified" for r in rows).items())
    return s + (f" ({bare} without numbers)" if bare else "")


def row_text(row: dict, cols: list[dict]) -> str:
    """One row as compact text for prompts and for the number check: the reported columns only."""
    return "; ".join(f"{c['name']}={row[c['name']]}" for c in cols if row.get(c["name"]) is not None)


def numbers_of(row: dict, cols: list[dict]) -> str:
    """The row's numbers as written, for the check that each is in a cited sentence."""
    out = []
    for c in cols:
        v = row.get(c["name"])
        if c["type"] == "number" and isinstance(v, (int, float)) and c["name"] not in (row.get("estimated") or []):
            out.append(str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
    return " ".join(out)


def as_text(field: dict, item: dict) -> str:
    """A rows item for a prompt: the summary, then one line per row."""
    cols = columns_of(field)
    rows = item.get("rows") or []
    return item.get("value", "NR") if not rows else \
        item.get("value", "") + "\n" + "\n".join(f"  row {i}: {row_text(r, cols)}" for i, r in enumerate(rows, 1))


def from_person(field: dict, value) -> list[dict]:
    """A person's rows: a JSON list, or one JSON object per line."""
    if isinstance(value, list):
        return clean(field, value)
    s = str(value or "").strip()
    if not s or s.upper() == "NR":
        return []
    try:
        data = json.loads(s)
    except ValueError:
        try:
            data = [json.loads(line) for line in s.splitlines() if line.strip()]
        except ValueError as e:
            raise ValueError(f"{field['name']}: rows are JSON, one object per line ({e})")
    return clean(field, data if isinstance(data, list) else [data])


def export_csv(ctx) -> tuple[Path, int]:
    """Every row of every included study's rows fields to <run_dir>/meta_rows.csv: the study, the field, the row's
    columns as extracted, its cited sentence ids and whether each number was found as written. Returns (path, rows)."""
    from .store import record_of
    st = ctx.store
    fields = [f for f in ctx.topic.get("extraction_fields") or [] if is_rows(f)]
    cols = {f["name"]: columns_of(f) for f in fields}
    names = list(dict.fromkeys(c["name"] for f in fields for c in cols[f["name"]]))
    head = ["study_id", "pmid", "first_author", "year", "title", "field", "row"] + names + \
           ["evidence", "numbers_not_found", "off_list", "estimated_from_figure", "figure", "edited_by_you", "note"]
    path = Path(ctx.run_dir) / "meta_rows.csv"
    n = 0
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        for rid in st.included_ids():
            ext = st.get(f"ext:{rid}")
            if not ext or ext["status"] != "active":
                continue
            rec = record_of(st, rid)
            author = str(rec.get("authors") or "").split(",")[0].split(";")[0].strip()
            for f in fields:
                item = (ext["data"].get("fields") or {}).get(f["name"]) or {}
                for i, r in enumerate(item.get("rows") or [], 1):
                    w.writerow([rid, rec.get("pmid", ""), author, rec.get("year", ""), rec.get("title", ""), f["name"], i]
                               + ["" if r.get(c) is None else r.get(c) for c in names]
                               + [" ".join(r.get("evidence") or []), " ".join(r.get("uncovered") or []),
                                  " ".join(r.get("off_list") or []), " ".join(r.get("estimated") or []),
                                  (r.get("figure_read") or {}).get("figure", ""), "yes" if item.get("edited") else "", r.get("note", "")])
                    n += 1
    return path, n
