"""Is the LLM judge trustworthy enough to replace most human scoring? (advisor's suggestion)

1. Known-answer controls: items whose correct label is known by construction
   (revised == gold -> "fixed"; revised == corrupted -> "not fixed"). Judge accuracy on these.
2. Inter-judge agreement: a second judge model re-labels a sample of the primary judge's
   decisions; Cohen's kappa per decision kind.
3. Light human audit: a small CSV, stratified towards judge disagreements, for a human to label.
   Only these items need human time.
"""
from __future__ import annotations

import csv
import json
import random

from .. import prompts
from ..context import Ctx
from .common import Judge, cohen_kappa, short, wilson


def controls(planted_items: list[dict]) -> list[dict]:
    items = []
    for p in planted_items:
        g, c = p.get("gold"), p.get("corrupted")
        if not isinstance(g, str) or not isinstance(c, str) or g == c:
            continue
        for revised, truth in ((g, "yes"), (c, "no")):
            items.append({"prompt": prompts.JUDGE_REVISION.format(node_id=p.get("location", ""), gold=g, corrupted=c,
                                                                  revised=revised, error=p.get("description", "")),
                          "truth": truth, "source": p.get("id", "")})
    return items


def run(ctx: Ctx, judge: Judge, out_dir, planted_items: list[dict], n_agree: int = 25, n_human: int = 15) -> dict:
    out = {}
    # 1. known-answer controls
    items = controls(planted_items)
    correct = 0
    for it in items:
        r = judge("control", it["prompt"], "fixed", {"truth": it["truth"]})
        it["label"] = r.get("fixed")
        correct += (it["label"] == it["truth"])
    out["known_answer_controls"] = {"n": len(items), "correct": correct,
                                    "accuracy": round(correct / len(items), 3) if items else None,
                                    "accuracy_95ci": wilson(correct, len(items)),
                                    "errors": [{"source": i["source"], "truth": i["truth"], "label": i["label"]}
                                               for i in items if i["label"] != i["truth"]]}
    # 2. inter-judge agreement
    log = [json.loads(l) for l in open(judge.log_path)] if judge.log_path.exists() else []
    log = [r for r in log if r["kind"] in ("match", "genuine", "revision", "unchanged", "control")]
    rnd = random.Random(5)
    sample = rnd.sample(log, min(n_agree, len(log)))
    j2 = Judge(ctx, out_dir, role="judge2")
    pairs = []
    for r in sample:
        r2 = j2(r["kind"], r["prompt"], r["label_key"], {"of": r["meta"]})
        pairs.append({"kind": r["kind"], "j1": r["label"], "j2": r2.get(r["label_key"]), "prompt": r["prompt"],
                      "j1_rationale": r["result"].get("rationale", ""), "j2_rationale": r2.get("rationale", "")})
    by_kind = {}
    for p in pairs:
        by_kind.setdefault(p["kind"], []).append(p)
    out["inter_judge"] = {"judge1": ctx.hub["judge"].name, "judge2": ctx.hub["judge2"].name, "n": len(pairs),
                          "raw_agreement": round(sum(str(p["j1"]) == str(p["j2"]) for p in pairs) / len(pairs), 3) if pairs else None,
                          "kappa_overall": cohen_kappa([p["j1"] for p in pairs], [p["j2"] for p in pairs]),
                          "by_kind": {k: {"n": len(v), "agree": sum(str(p["j1"]) == str(p["j2"]) for p in v),
                                          "kappa": cohen_kappa([p["j1"] for p in v], [p["j2"] for p in v])}
                                      for k, v in by_kind.items()}}
    # 3. human audit sheet (disagreements first)
    dis = [p for p in pairs if str(p["j1"]) != str(p["j2"])]
    agr = [p for p in pairs if str(p["j1"]) == str(p["j2"])]
    pick = dis[:n_human] + agr[: max(0, n_human - len(dis))]
    path = out_dir / "human_audit_sheet.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item", "kind", "judge1_label", "judge2_label", "judge1_rationale", "judge2_rationale",
                    "prompt_given_to_judges", "HUMAN_LABEL", "HUMAN_NOTE"])
        for i, p in enumerate(pick):
            w.writerow([i + 1, p["kind"], p["j1"], p["j2"], short(p["j1_rationale"], 500), short(p["j2_rationale"], 500),
                        short(p["prompt"], 6000), "", ""])
    out["human_audit"] = {"file": str(path), "n_items": len(pick), "n_disagreements": len(dis),
                          "status": "prepared_not_reviewed", "n_reviewed": 0,
                          "note": "Prepared for a person. It counts as done only after HUMAN_LABEL is filled in and imported "
                                  "with `sragent eval-audit`."}
    return out


def import_audit(sheet: str) -> dict:
    """Read a filled audit sheet. Status becomes 'reviewed' only when every item has a HUMAN_LABEL."""
    rows = list(csv.DictReader(open(sheet, newline="")))
    done = [r for r in rows if (r.get("HUMAN_LABEL") or "").strip()]
    agree1 = sum(1 for r in done if r["HUMAN_LABEL"].strip().lower() == str(r["judge1_label"]).strip().lower())
    agree2 = sum(1 for r in done if r["HUMAN_LABEL"].strip().lower() == str(r["judge2_label"]).strip().lower())
    status = "reviewed" if rows and len(done) == len(rows) else ("partially_reviewed" if done else "prepared_not_reviewed")
    return {"status": status, "n_items": len(rows), "n_reviewed": len(done),
            "human_vs_judge1_agreement": round(agree1 / len(done), 3) if done else None,
            "human_vs_judge2_agreement": round(agree2 / len(done), 3) if done else None,
            "human_vs_judge1_kappa": cohen_kappa([r["HUMAN_LABEL"].strip().lower() for r in done],
                                                 [str(r["judge1_label"]).strip().lower() for r in done]) if done else None}
