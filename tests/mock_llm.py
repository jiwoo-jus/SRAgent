"""Deterministic mock LLM: returns schema-valid JSON for every prompt in sragent.prompts.

Used to test all code paths offline (no API cost). Not a model of quality.
"""
from __future__ import annotations

import json
import re


def _json_after(text: str, marker: str):
    i = text.find(marker)
    if i < 0:
        return None
    j = text.find("{", i)
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[j:])
        return obj
    except ValueError:
        return None


def _ids(text: str):
    return list(dict.fromkeys(re.findall(r"^\[([\w]+)\]", text, flags=re.M)))


def mock_fn(messages, mode):
    u = messages[-1]["content"]
    if "auditing an AI-generated systematic" in u:
        ids = re.findall(r"\[ext:(\w+)\]", u)[:2] or ["x"]
        return json.dumps({"errors": [{"location": f"ext:{i} sample_size", "error_type": "extraction", "description": "mismatch",
                                       "proposed_correction": "fix"} for i in ids] +
                                     [{"location": "S2", "error_type": "synthesis", "description": "overgeneralised", "proposed_correction": "restore"}]})
    if "Draft a review protocol" in u:
        return json.dumps({"title": "T", "question": "Q", "pico": {"population": "humans", "intervention_or_exposure": "emicizumab",
                           "comparator": "NR", "outcomes": ["PK"], "study_designs": ["any"]},
                           "criteria": [{"id": "I1", "type": "inclusion", "text": "about emicizumab", "stage": "title_abstract"},
                                        {"id": "I2", "type": "inclusion", "text": "human data", "stage": "title_abstract"},
                                        {"id": "I3", "type": "inclusion", "text": "PK data", "stage": "title_abstract"},
                                        {"id": "P1", "type": "practical", "text": "full text in English", "stage": "full_text"}],
                           "key_concepts": [{"term": "emicizumab", "kind": "drug"}]})
    if "Build a PubMed search" in u:
        return json.dumps({"concept_blocks": [], "query": "emicizumab[tiab]", "rationale": "mock"})
    if "Assess this record against each eligibility criterion" in u:
        rec = u.split("RECORD", 1)[1]
        ok = "emicizumab" in rec.lower() or "ace910" in rec.lower()
        q = rec.split("Title:", 1)[-1][:40].strip()
        return json.dumps({"criteria": [{"id": i, "assessment": "met" if ok else "not_met", "evidence": ["s1"]} for i in ("I1", "I2", "I3")],
                           "decision": "include" if ok else "exclude", "reason": "mock", "confidence": 0.9})
    if "Extract the following items" in u:
        src = u.split('"""', 1)[1]
        q = src.strip()[:60]
        names = re.findall(r"^- (\w+):", u.split("ITEMS:", 1)[1].split("SOURCE", 1)[0], flags=re.M)
        return json.dumps({"fields": {n: {"value": f"{n} value 12 participants", "evidence": ["s1", "s2"]} for n in names}})
    if "risk-of-bias" in u:
        doms = re.findall(r"^- (\w+):", u.split("DOMAINS:", 1)[1].split("STUDY EXTRACTION", 1)[0], flags=re.M)
        src = u.split('"""', 1)[1].strip()[:50]
        return json.dumps({"domains": [{"domain": d, "judgment": "low", "rationale": "mock", "evidence": ["s2"]} for d in doms],
                           "overall": "low"})
    if "Write the narrative synthesis" in u:
        ids = _ids(u.split("INCLUDED STUDIES", 1)[1])
        return json.dumps({"statements": [{"id": f"S{k + 1}", "text": f"Statement {k + 1} reports 12 participants in adults.",
                                           "cites": ids[k::3] or ids[:1], "scope": "adults"} for k in range(3)]})
    if "Check whether a synthesis statement" in u:
        return json.dumps({"support": "supported", "issues": [], "overgeneralization": False})
    if "check the feedback against the source" in u:
        node = _json_after(u, "TARGET OUTPUT")
        if "should not have been excluded" in u and node and "criteria" in node:
            for a in node["criteria"]:
                a["assessment"] = "met"
        elif node and "fields" in node:
            k = next(iter(node["fields"]))
            node["fields"][k]["value"] = node["fields"][k]["value"] + " (corrected)"
        wrong = "WRONG" in u
        return json.dumps({"verdict": "unsupported" if wrong else "supported", "explanation": "mock",
                           "evidence_quote": "", "revised": None if wrong else node})
    if "Apply it." in u:
        node = _json_after(u, "TARGET OUTPUT")
        return json.dumps({"revised": node, "explanation": "applied"})
    if "Decide which related outputs" in u:
        ids = re.findall(r"^\[([^\]]+)\]", u.split("CANDIDATE RELATED OUTPUTS:", 1)[1], flags=re.M)
        return json.dumps({"updates": [{"id": i, "needs_update": i.startswith("syn:"), "reason": "mock"} for i in ids]})
    if "Revise one output" in u:
        node = _json_after(u, "OUTPUT TO REVISE")
        if node and "text" in node:
            node["text"] = node["text"] + " [revised]"
        return json.dumps({"revised": node, "explanation": "mock"})
    if "A new study was added" in u:
        st = json.loads(u.split("CURRENT STATEMENTS:", 1)[1].split("ALL INCLUDED STUDIES", 1)[0])
        rid = re.search(r"NEW STUDY \[(\w+)\]", u).group(1)
        if st:
            st[0]["cites"].append(rid)
        return json.dumps({"statements": st, "changed_ids": [st[0]["id"]] if st else []})
    if "Audit a systematic review" in u:
        return json.dumps({"contradictions": []})
    if "OVER-GENERALISES" in u:
        t = re.search(r"STATEMENT: (.*)", u).group(1)
        return json.dumps({"text": t.replace("in adults", "in all patients") + " overall", "what_changed": "dropped adults"})
    if "confident but WRONG" in u:
        return json.dumps({"feedback": "WRONG: the sample size is 999.", "false_claim": "999"})
    if "planted in" in u:
        return json.dumps({"identifies_error": True, "correction_correct": False, "rationale": "mock"})
    if "GENUINE error" in u:
        return json.dumps({"genuine_error": False, "rationale": "mock"})
    if "Grade a revision" in u:
        rev = u.split("REVISED version (after feedback):", 1)[1].split("THE ERROR", 1)[0].strip()
        gold = u.split("GOLD (correct) version:", 1)[1].split("CORRUPTED", 1)[0].strip()
        return json.dumps({"fixed": "yes" if rev == gold else "no", "introduced_new_error": False, "rationale": "mock"})
    if "was changed even though it was correct" in u:
        return json.dumps({"introduced_new_error": False, "rationale": "mock"})
    if "For each extracted item" in u:
        names = re.findall(r"- name: (\w+)", u)
        return json.dumps({"items": [{"name": n, "label": "supported", "issue": ""} for n in names]})
    if "Identify which output it targets" in u:
        return json.dumps({"target": "NEW_PAPER", "field": "", "pmid": ""})
    return json.dumps({"ok": True})
