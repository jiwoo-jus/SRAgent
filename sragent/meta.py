"""Quantitative synthesis of a rows item (sragent/rows.py): effect sizes and pooled estimates, computed by code.

Nothing here is specific to one review. A review says which columns of its rows item play which role, which rows
count and how they are grouped, in its topic file:

  meta_analysis:
    field: outcome_data
    include: {comparison_type: [aspirin vs control]}      # rows used: column -> accepted values
    group_by: [outcome_family, model_tier]                # one pooled estimate per combination
    min_studies: 3
    columns:                                              # the columns that hold each quantity
      treated: {n: n_aspirin, value: value_aspirin, spread: dispersion_aspirin, upper: dispersion_aspirin_upper}
      control: {n: n_control, value: value_control, spread: dispersion_control, upper: dispersion_control_upper}
      value_type: value_type            # mean | geometric mean | number of events | percent or proportion | ...
      spread_type: dispersion_type      # SD | SEM | 95% CI | ...
      normalised: {column: measure_basis, value: normalised to control}     # optional
      binary_only_if: {column: unit_of_n, values: [participants]}           # optional: when a percentage counts events
      risk_of_bias_high: high           # optional: the overall rating left out in the sensitivity analysis, or
                                        #   {judgment: high, domains: [confounding_and_allocation, ...]} for those domains only
      direction: direction              # optional: the column the synthesis counts for groups that are not pooled
    classify:                             # optional: a group_by dimension the rows do not hold, derived by rules
      - set: readout                      #   the group_by name it fills ("what was measured")
        column: analyte_name              #   the text the patterns are matched against (any case)
        also: [measure_level]             #   more columns added to that text
        rules: [{label: release / circulating level, pattern: "serum|plasma|medium|secret"}, ...]
        otherwise: {column: measure_level, map: {protein: protein level}}      # when no rule matches
    refine:                               # optional: finer groups from a text column, by rules (first match wins)
      - when: {outcome_family: clinical outcome}
        column: analyte_name              # the text the patterns are matched against (regular expressions, any case)
        set: outcome_family               # the group_by column whose value becomes "<old value>: <label>"
        rules: [{label: preeclampsia, pattern: "pre-?eclampsia"}, ...]
        otherwise: other
    exclude: [{column: model, pattern: "mouse|mice|rat", label: animal model}]   # optional: rows left out by a rule on a text column
    same_study: [[37058400, 38151219]]    # optional: papers that report one trial; in a group only the first with data counts

Method (all on the log scale, treated / control):
  medians      a median with its quartiles or range is turned into an approximate mean and SD (Wan et al. 2014), marked.
  continuous   log ratio of means, ln(mt/mc), variance sd_t^2/(n_t mt^2) + sd_c^2/(n_c mc^2) (delta method). SD from
               SEM (x sqrt n) or from a 95% CI ((upper - lower) sqrt n / 3.92). Both means must be positive. A control
               normalised to 1 or 100% with no spread gets the treated group's coefficient of variation (marked).
  binary       log risk ratio from events and group sizes (a percentage is turned into events), 0.5 added to every
               cell when one is zero. With columns.binary_only_if, a percentage counts events only for those rows
               (participants); elsewhere it is a mean (% apoptotic cells in 3 experiments).
  one effect per study and group: several rows of a study in one group (doses, time points, analytes) are averaged,
               the variance too (as if fully correlated: no study counts more for reporting more rows).
  pooling      random effects, DerSimonian-Laird tau^2, Hartung-Knapp confidence interval (t, k-1 df; never narrower
               than the classical one), I^2, and a prediction interval where enough studies support one (PREDICTION_MIN).
  sensitivity  without values estimated from a figure; without studies rated at high risk of bias.
Continuous and binary effects are never pooled together. Rows that cannot be used are counted by reason. Two studies
with the same effect and variance in a group are listed as possible duplicates (for `same_study`).
"""
from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import rows

CONTINUOUS = ("mean", "geometric mean")
BINARY = ("number of events", "percent or proportion")


# ---------------------------------------------------------------- small statistics (no numpy / scipy in this project)
def _betacf(a: float, b: float, x: float) -> float:
    tiny, qab, qap, qam = 1e-300, a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + aa * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + aa / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.0) < 1e-12:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0 or x >= 1:
        return 0.0 if x <= 0 else 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x))
    return bt * _betacf(a, b, x) / a if x < (a + 1.0) / (a + b + 2.0) else 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_cdf(t: float, df: float) -> float:
    p = 0.5 * _betai(df / 2.0, 0.5, df / (df + t * t))
    return 1.0 - p if t > 0 else p


def t_quantile(p: float, df: float) -> float:
    lo, hi = 0.0, 1e3
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if t_cdf(mid, df) < p else (lo, mid)
    return (lo + hi) / 2


PREDICTION_MIN = 5    # a prediction interval below this many studies rests on a tau2 nobody can estimate


def pool(effects: list[tuple[float, float]], prediction_min: int = PREDICTION_MIN) -> dict:
    """Random-effects pooled estimate of (yi, vi) pairs on the log scale.

    The prediction interval needs an estimate of the between-study variance, which a handful of studies cannot give:
    with three or four studies it spans orders of magnitude and says nothing. It is therefore not computed below
    `prediction_min` studies, and the reason is kept (`prediction_note`) so a table can print it."""
    k = len(effects)
    y = [e[0] for e in effects]
    w = [1.0 / e[1] for e in effects]
    sw = sum(w)
    fixed = sum(wi * yi for wi, yi in zip(w, y)) / sw
    q = sum(wi * (yi - fixed) ** 2 for wi, yi in zip(w, y))
    c = sw - sum(wi * wi for wi in w) / sw
    tau2 = max(0.0, (q - (k - 1)) / c) if k > 1 and c > 0 else 0.0
    ws = [1.0 / (e[1] + tau2) for e in effects]
    sws = sum(ws)
    mu = sum(wi * yi for wi, yi in zip(ws, y)) / sws
    se = math.sqrt(1.0 / sws)
    out = {"k": k, "log": mu, "tau2": tau2, "q": q, "i2": max(0.0, (q - (k - 1)) / q) * 100 if k > 1 and q > 0 else 0.0,
           "weights": [100 * wi / sws for wi in ws]}
    if k > 1:   # Hartung-Knapp, not narrower than the classical interval
        hk = math.sqrt(max(1.0, sum(wi * (yi - mu) ** 2 for wi, yi in zip(ws, y)) / (k - 1))) * se
        half = t_quantile(0.975, k - 1) * hk
        out["p"] = 2 * (1 - t_cdf(abs(mu) / hk, k - 1)) if hk > 0 else None
    else:
        half = 1.959964 * se
        out["p"] = None
    out["ci"] = [mu - half, mu + half]
    if k >= max(3, prediction_min):
        pi = t_quantile(0.975, k - 2) * math.sqrt(tau2 + se * se)
        out["prediction"] = [mu - pi, mu + pi]
    else:
        out["prediction_note"] = f"not estimable with fewer than {max(3, prediction_min)} studies"
    out.update(ratio=math.exp(mu), ratio_ci=[math.exp(x) for x in out["ci"]],
               ratio_prediction=[math.exp(x) for x in out["prediction"]] if "prediction" in out else None)
    return out


# ---------------------------------------------------------------- one row -> one effect
def _f(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sd(spread, upper, n, kind: str | None):
    if spread is None or n is None or n <= 0:
        return None
    if kind == "SD":
        return spread
    if kind == "SEM":
        return spread * math.sqrt(n)
    if kind == "95% CI" and upper is not None and upper > spread:
        return (upper - spread) * math.sqrt(n) / 3.92
    return None


def _from_median(m, lo, hi, n, kind: str | None):
    """(mean, SD) from a median with its quartiles (IQR) or its minimum and maximum (range): Wan et al., BMC Med Res
    Methodol 2014. None when the two bounds are not given."""
    if m is None or lo is None or hi is None or not lo <= m <= hi or hi <= lo:
        return None
    if kind == "IQR":
        return (lo + m + hi) / 3, (hi - lo) / 1.35
    if kind == "range":
        sd = (hi - lo) / (4 if n <= 70 else 6) if n > 15 else math.sqrt(((lo - 2 * m + hi) ** 2 / 4 + (hi - lo) ** 2) / 12)
        return (lo + 2 * m + hi) / 4, sd
    return None


def effect_of(row: dict, col: dict) -> tuple[dict | None, str]:
    """(effect, "") or (None, why the row cannot be used). effect: {measure, yi, vi, notes}."""
    t, c = col["treated"], col["control"]
    nt, nc = _f(row.get(t["n"])), _f(row.get(c["n"]))
    vt, vc = _f(row.get(t["value"])), _f(row.get(c["value"]))
    kind = row.get(col.get("value_type"))
    if vt is None or vc is None:
        return None, "no values for both groups"
    if nt is None or nc is None or nt <= 0 or nc <= 0:
        return None, "group size missing"
    only = col.get("binary_only_if") or {}
    counts = not only or row.get(only.get("column")) in (only.get("values") or [])
    if kind == "percent or proportion" and not counts:
        kind = "mean"          # a percentage measured per experiment (% apoptotic cells, n = 3) is a mean, not events
    if kind in BINARY:
        a, b = (vt, vc) if kind == "number of events" else (vt / 100 * nt, vc / 100 * nc)
        if not (0 <= a <= nt and 0 <= b <= nc):
            return None, "events outside the group size"
        if a == 0 and b == 0:
            return None, "no events in either group"
        notes = []
        if a == 0 or b == 0 or a == nt or b == nc:
            a, b, nt, nc = a + 0.5, b + 0.5, nt + 1, nc + 1
            notes.append("0.5 added (a zero cell)")
        return {"measure": "risk ratio", "yi": math.log((a / nt) / (b / nc)), "vi": 1 / a - 1 / nt + 1 / b - 1 / nc, "notes": notes}, ""
    notes = []
    st_kind = row.get(col.get("spread_type"))
    if kind == "median":      # a median with its quartiles or range -> an approximate mean and SD (Wan et al. 2014)
        got = [_from_median(v, _f(row.get(g.get("spread"))), _f(row.get(g.get("upper"))), n, st_kind)
               for v, g, n in ((vt, t, nt), (vc, c, nc))]
        if None in got:
            return None, "a median without its quartiles or range"
        (vt, sdt), (vc, sdc) = got
        notes.append(f"median and {st_kind} converted to mean and SD")
        if vt <= 0 or vc <= 0:
            return None, "a mean is zero or negative (no ratio)"
        return {"measure": "ratio of means", "yi": math.log(vt / vc),
                "vi": sdt ** 2 / (nt * vt ** 2) + sdc ** 2 / (nc * vc ** 2), "notes": notes}, ""
    if kind not in CONTINUOUS:
        return None, f"value type not pooled ({kind or 'not stated'})"
    if vt <= 0 or vc <= 0:
        return None, "a mean is zero or negative (no ratio)"
    sdt = _sd(_f(row.get(t.get("spread"))), _f(row.get(t.get("upper"))), nt, st_kind)
    sdc = _sd(_f(row.get(c.get("spread"))), _f(row.get(c.get("upper"))), nc, st_kind)
    norm = col.get("normalised") or {}
    if sdt is not None and not sdc and norm and row.get(norm.get("column")) == norm.get("value"):
        sdc = sdt / vt * vc                      # the control was set to 1 or 100%: it has no spread of its own
        notes.append("control spread taken from the treated group's coefficient of variation")
    if sdt is None or sdc is None or sdt <= 0 or sdc <= 0:
        return None, "no usable spread (SD, SEM or 95% CI) for both groups"
    return {"measure": "ratio of means", "yi": math.log(vt / vc),
            "vi": sdt ** 2 / (nt * vt ** 2) + sdc ** 2 / (nc * vc ** 2), "notes": notes}, ""


# ---------------------------------------------------------------- the analysis of a review
def plan_of(topic: dict) -> dict | None:
    p = topic.get("meta_analysis")
    if not isinstance(p, dict):
        return None
    f = next((x for x in topic.get("extraction_fields") or [] if x.get("name") == p.get("field") and rows.is_rows(x)), None)
    if f is None:
        raise ValueError(f"meta_analysis.field: {p.get('field')!r} is not an extraction item of kind rows")
    names = {c["name"] for c in rows.columns_of(f)}
    col = p.get("columns") or {}
    derived = {str(c.get("set")) for c in p.get("classify") or []}         # group_by names the rules fill, not columns
    used = [v for g in ("treated", "control") for v in (col.get(g) or {}).values()] + [g for g in p.get("group_by") or [] if g not in derived] + \
        list((p.get("include") or {})) + [col.get("value_type"), col.get("spread_type"), (col.get("normalised") or {}).get("column"),
                                         (col.get("binary_only_if") or {}).get("column"), col.get("direction")] + \
        [c.get("column") for c in p.get("classify") or []] + [k for c in p.get("classify") or [] for k in (c.get("also") or [])] + \
        [(c.get("otherwise") or {}).get("column") for c in p.get("classify") or []]
    bad = sorted({str(u) for u in used if u and u not in names})
    if bad:
        raise ValueError(f"meta_analysis: not columns of {f['name']}: {', '.join(bad)}")
    for g in ("treated", "control"):
        if not {"n", "value"} <= set(col.get(g) or {}):
            raise ValueError(f"meta_analysis.columns.{g} needs n and value")
    if not p.get("group_by") or not col.get("value_type"):
        raise ValueError("meta_analysis needs group_by and columns.value_type")
    for i, x in enumerate(p.get("exclude") or []):
        try:
            re.compile(str(x["pattern"]))
            ok = x["column"] in names
        except (re.error, KeyError, TypeError):
            ok = False
        if not ok:
            raise ValueError(f"meta_analysis.exclude[{i}]: needs a column of {f['name']} and a pattern (a regular expression)")
    for i, c in enumerate(p.get("classify") or []):
        if str(c.get("set")) not in (p.get("group_by") or []):
            raise ValueError(f"meta_analysis.classify[{i}]: set {c.get('set')!r} is not one of group_by")
        for x in c.get("rules") or []:
            try:
                re.compile(str(x.get("pattern")))
            except (re.error, AttributeError) as e:
                raise ValueError(f"meta_analysis.classify[{i}]: pattern {x!r}: {e}")
    for i, r in enumerate(p.get("refine") or []):
        miss = sorted({str(x) for x in [r.get("column"), r.get("set"), *(r.get("when") or {})] if x not in names})
        if miss or r.get("set") not in p["group_by"]:
            raise ValueError(f"meta_analysis.refine[{i}]: column, set and when name columns of {f['name']}, and set is one of group_by"
                             + (f" (not columns: {', '.join(miss)})" if miss else ""))
        for x in r.get("rules") or []:
            try:
                re.compile(str(x.get("pattern")))
            except (re.error, AttributeError) as e:
                raise ValueError(f"meta_analysis.refine[{i}]: pattern {x!r}: {e}")
    return p


def left_out(row: dict, plan: dict) -> str:
    """Why the plan leaves a row out ("" when it counts): not one of the `include` values, or an `exclude` rule matches."""
    if any(row.get(k) not in (v if isinstance(v, list) else [v]) for k, v in (plan.get("include") or {}).items()):
        return "not the comparison analysed"
    for x in plan.get("exclude") or []:
        if re.search(str(x["pattern"]), str(row.get(x["column"]) or ""), re.I):
            return f"left out by rule: {x.get('label') or x['column']}"
    return ""


def classes(row: dict, plan: dict) -> dict:
    """The row's derived group values (meta_analysis.classify): what kind of measurement it is, read from the wording
    of the measurement, falling back to a column of the row. Blood and expression are not the same measurement, so a
    dimension like this keeps them in separate analyses."""
    out = {}
    for c in plan.get("classify") or []:
        text = " ".join(str(row.get(k) or "") for k in [c["column"], *(c.get("also") or [])])
        lab = next((x["label"] for x in c.get("rules") or [] if re.search(str(x["pattern"]), text, re.I)), None)
        if lab is None:
            o = c.get("otherwise") or {}
            v = row.get(o.get("column")) if o.get("column") else None
            lab = (o.get("map") or {}).get(v) or v or o.get("label") or "not stated"
        out[str(c["set"])] = str(lab)
    return out


def refined(row: dict, plan: dict) -> dict:
    """The row's group values after the plan's classify and refine rules ("clinical outcome" -> "clinical outcome:
    preeclampsia")."""
    derived = classes(row, plan)
    out = {g: derived.get(g) or str(row.get(g) or "not stated") for g in plan["group_by"]}
    for r in plan.get("refine") or []:
        if any(row.get(k) not in (v if isinstance(v, list) else [v]) for k, v in (r.get("when") or {}).items()):
            continue
        text = str(row.get(r["column"]) or "")
        lab = next((x["label"] for x in r.get("rules") or [] if re.search(str(x["pattern"]), text, re.I)), r.get("otherwise"))
        if lab:
            out[r["set"]] = f"{out[r['set']]}: {lab}"
    return out


def _high_risk(rob: dict | None, rule) -> bool:
    """columns.risk_of_bias_high: an overall rating ("high"), or {judgment: high, domains: [...]}: any of these domains
    rated so (internal validity only, when other domains rate how well the study applies to the question)."""
    if not rule or not rob or rob["status"] != "active":
        return False
    if isinstance(rule, dict):
        doms = set(rule.get("domains") or [])
        return any(d.get("judgment") == rule.get("judgment", "high") and (not doms or d.get("domain") in doms)
                   for d in rob["data"].get("domains") or [])
    return rob["data"].get("overall") == rule


def _label(rec: dict) -> str:
    a = re.split(r"[,;]", str(rec.get("authors") or ""))[0].strip()
    return f"{a.split()[0] if a else rec.get('pmid') or '?'} {rec.get('year') or ''}".strip()


def analyse(ctx) -> dict:
    """Every group's per-study effects and pooled estimates; rows left out, by reason. No model calls."""
    from .store import record_of
    st = ctx.store
    p = plan_of(ctx.topic)
    if p is None:
        raise ValueError("this review has no meta_analysis section in its topic file")
    col, inc, gb = p["columns"], p.get("include") or {}, p["group_by"]
    kmin = int(p.get("min_studies", 3))
    high = col.get("risk_of_bias_high")
    skipped: Counter = Counter()
    cells: dict = defaultdict(lambda: defaultdict(list))     # (group, measure) -> rid -> [effect]
    n_rows = 0
    for rid in st.included_ids():
        ext = st.get(f"ext:{rid}")
        if not ext or ext["status"] != "active":
            continue
        for i, r in enumerate(((ext["data"]["fields"].get(p["field"]) or {}).get("rows")) or [], 1):
            n_rows += 1
            why = left_out(r, p)
            if why:
                skipped[why] += 1
                continue
            e, why = effect_of(r, col)
            if e is None:
                skipped[why] += 1
                continue
            e.update(row=i, estimated=bool(r.get("estimated")))
            cells[(tuple(refined(r, p).values()), e["measure"])][rid].append(e)
    first = {str(x): str(grp[0]) for grp in p.get("same_study") or [] for x in grp}       # paper -> the trial's first paper
    groups, dups = [], []
    for (g, measure), by_study in sorted(cells.items()):
        for rid in [r for r in by_study if first.get(r, r) != r]:
            trial = [x for x in by_study if first.get(x) == first[rid]]
            keep = first[rid] if first[rid] in trial else sorted(trial)[0]
            if rid != keep:                       # another paper of the same trial is in this group: it counts once
                skipped["same trial as another included paper"] += len(by_study.pop(rid))
        seen: dict = {}
        for rid, es in by_study.items():
            k = (round(sum(e["yi"] for e in es) / len(es), 4), round(sum(e["vi"] for e in es) / len(es), 5))
            if k in seen:
                dups.append({"studies": [seen[k], rid], "group": dict(zip(gb, g)), "measure": measure})
            seen.setdefault(k, rid)
        studies = []
        for rid, es in by_study.items():
            rob = st.get(f"rob:{rid}")
            studies.append({"rid": rid, "label": _label(record_of(st, rid)), "rows": [e["row"] for e in es],
                            "yi": sum(e["yi"] for e in es) / len(es), "vi": sum(e["vi"] for e in es) / len(es),
                            "estimated": any(e["estimated"] for e in es), "all_estimated": all(e["estimated"] for e in es),
                            "reported": [(e["yi"], e["vi"]) for e in es if not e["estimated"]],
                            "notes": sorted({n for e in es for n in e["notes"]}),
                            "high_risk": _high_risk(rob, high)})
        studies.sort(key=lambda s: s["label"])
        for s in studies:
            s.update(ratio=math.exp(s["yi"]), ratio_ci=[math.exp(s["yi"] - 1.959964 * math.sqrt(s["vi"])),
                                                        math.exp(s["yi"] + 1.959964 * math.sqrt(s["vi"]))])
        out = {"group": dict(zip(gb, g)), "measure": measure, "k": len(studies), "studies": studies, "pooled": None, "sensitivity": {}}
        if len(studies) >= kmin:
            out["pooled"] = pool([(s["yi"], s["vi"]) for s in studies])
            for s, wt in zip(studies, out["pooled"].pop("weights")):
                s["weight"] = wt
            rep = [(sum(y for y, _ in s["reported"]) / len(s["reported"]), sum(v for _, v in s["reported"]) / len(s["reported"]))
                   for s in studies if s["reported"]]
            if len(rep) != len(studies) or any(s["estimated"] for s in studies):
                out["sensitivity"]["reported numbers only"] = _brief(pool(rep)) if len(rep) >= kmin else {"k": len(rep)}
            low = [(s["yi"], s["vi"]) for s in studies if not s["high_risk"]]
            if high and len(low) != len(studies):
                out["sensitivity"]["without high risk of bias"] = _brief(pool(low)) if len(low) >= kmin else {"k": len(low)}
        for s in studies:
            s.pop("reported")
        groups.append(out)
    groups.sort(key=lambda x: (x["pooled"] is None, -x["k"], str(x["group"])))
    n_used = sum(len(s["rows"]) for g in groups for s in g["studies"])
    return {"field": p["field"], "group_by": gb, "min_studies": kmin, "rows": n_rows, "rows_used": n_used,
            "not_used": dict(skipped.most_common()), "possible_duplicates": dups, "groups": groups}


def _brief(pl: dict) -> dict:
    return {k: pl[k] for k in ("k", "ratio", "ratio_ci", "i2", "p")}


# ---------------------------------------------------------------- outputs
def _name(group: dict, measure: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (" ".join(group.values()) + " " + measure).lower()).strip("_")


def forest_svg(g: dict) -> str:
    """A forest plot of one group (log scale), as SVG text."""
    S, P = g["studies"], g["pooled"]
    vals = [x for s in S for x in s["ratio_ci"]] + (P["ratio_ci"] if P else [])
    lo, hi = max(min(vals), 0.01), min(max(vals), 100.0)
    lo, hi = min(lo, 0.8), max(hi, 1.25)
    x0, x1, row, top = 250, 610, 22, 46
    h = top + row * (len(S) + (2 if P else 0)) + 40

    def X(v):
        v = min(max(v, lo), hi)
        return x0 + (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo)) * (x1 - x0)
    title = " · ".join(g["group"].values()) + f" ({g['measure']})"
    L = [f'<svg xmlns="http://www.w3.org/2000/svg" width="860" height="{h}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
         f'<rect width="860" height="{h}" fill="white"/><text x="10" y="18" font-size="14" font-weight="bold">{_x(title)}</text>',
         f'<text x="10" y="{top - 8}" font-weight="bold">Study</text><text x="{x1 + 20}" y="{top - 8}" font-weight="bold">Ratio (95% CI)</text>'
         f'<text x="800" y="{top - 8}" font-weight="bold">Weight</text>',
         f'<line x1="{X(1):.1f}" y1="{top - 2}" x2="{X(1):.1f}" y2="{h - 34}" stroke="#888" stroke-dasharray="3 3"/>']
    for i, s in enumerate(S):
        y = top + row * i + 12
        mark = " ≈" if s["estimated"] else ""
        size = 4 + 6 * math.sqrt(s.get("weight", 10) / 100)
        L.append(f'<text x="10" y="{y + 4}">{_x(s["label"] + mark)}</text>'
                 f'<line x1="{X(s["ratio_ci"][0]):.1f}" y1="{y}" x2="{X(s["ratio_ci"][1]):.1f}" y2="{y}" stroke="#333"/>'
                 f'<rect x="{X(s["ratio"]) - size / 2:.1f}" y="{y - size / 2:.1f}" width="{size:.1f}" height="{size:.1f}" fill="{"#999" if s["estimated"] else "#1f4e79"}"/>'
                 f'<text x="{x1 + 20}" y="{y + 4}">{s["ratio"]:.2f} ({s["ratio_ci"][0]:.2f}–{s["ratio_ci"][1]:.2f})</text>'
                 + (f'<text x="800" y="{y + 4}">{s["weight"]:.1f}%</text>' if "weight" in s else ""))
    if P:
        y = top + row * (len(S) + 1)
        a, m, b = X(P["ratio_ci"][0]), X(P["ratio"]), X(P["ratio_ci"][1])
        L.append(f'<polygon points="{a:.1f},{y} {m:.1f},{y - 7} {b:.1f},{y} {m:.1f},{y + 7}" fill="#b03a2e"/>'
                 f'<text x="10" y="{y + 4}" font-weight="bold">Random effects (k = {P["k"]}, I² = {P["i2"]:.0f}%)</text>'
                 f'<text x="{x1 + 20}" y="{y + 4}" font-weight="bold">{P["ratio"]:.2f} ({P["ratio_ci"][0]:.2f}–{P["ratio_ci"][1]:.2f})</text>')
    for v in (0.01, 0.1, 0.25, 0.5, 1, 2, 4, 10, 100):
        if lo <= v <= hi:
            L.append(f'<line x1="{X(v):.1f}" y1="{h - 34}" x2="{X(v):.1f}" y2="{h - 29}" stroke="#333"/>'
                     f'<text x="{X(v):.1f}" y="{h - 16}" text-anchor="middle">{v:g}</text>')
    L.append(f'<line x1="{x0}" y1="{h - 34}" x2="{x1}" y2="{h - 34}" stroke="#333"/>'
             f'<text x="{x0}" y="{h - 3}" font-size="11">lower with treatment</text><text x="{x1}" y="{h - 3}" font-size="11" text-anchor="end">higher with treatment</text>'
             f'<text x="850" y="{h - 3}" font-size="11" fill="#666" text-anchor="end">≈ includes values estimated from a figure</text></svg>')
    return "\n".join(L)


def _x(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write(ctx) -> dict:
    """Run the analysis and write <run_dir>/meta/: results.json, effects.csv (one line per study and group),
    summary.md and a forest plot per pooled group."""
    res = analyse(ctx)
    d = Path(ctx.run_dir) / "meta"
    d.mkdir(exist_ok=True)
    for old in d.glob("forest_*.svg"):
        old.unlink()
    (d / "results.json").write_text(json.dumps(res, indent=1))
    gb = res["group_by"]
    with (d / "effects.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(gb + ["measure", "study_id", "study", "rows", "log_ratio", "variance", "ratio", "ci_low", "ci_high", "weight_percent",
                         "estimated_from_figure", "high_risk_of_bias", "notes"])
        for g in res["groups"]:
            for s in g["studies"]:
                w.writerow([g["group"][k] for k in gb] + [g["measure"], s["rid"], s["label"], " ".join(map(str, s["rows"])),
                                                           round(s["yi"], 5), round(s["vi"], 6), round(s["ratio"], 4),
                                                           round(s["ratio_ci"][0], 4), round(s["ratio_ci"][1], 4),
                                                           round(s["weight"], 2) if "weight" in s else "",
                                                           "yes" if s["estimated"] else "", "yes" if s["high_risk"] else "", "; ".join(s["notes"])])
    pooled = [g for g in res["groups"] if g["pooled"]]
    L = ["# Quantitative synthesis", "",
         f"{res['rows_used']} of {res['rows']} extracted rows gave an effect size; {len(pooled)} groups have at least "
         f"{res['min_studies']} studies and were pooled (random effects, Hartung-Knapp interval). A ratio below 1 means lower with treatment.",
         "", "| " + " | ".join(gb) + " | measure | studies | ratio (95% CI) | I² | prediction interval | reported numbers only | without high risk of bias |",
         "|" + "---|" * (len(gb) + 7)]

    def br(x):
        return "same as the main estimate" if not x else f"not pooled, k = {x['k']}" if "ratio" not in x else \
            f"{x['ratio']:.2f} ({x['ratio_ci'][0]:.2f}–{x['ratio_ci'][1]:.2f}), k = {x['k']}"
    for g in pooled:
        P = g["pooled"]
        (d / f"forest_{_name(g['group'], g['measure'])}.svg").write_text(forest_svg(g))
        pr = P.get("ratio_prediction")
        L.append("| " + " | ".join(g["group"][k] for k in gb) + f" | {g['measure']} | {P['k']} | {P['ratio']:.2f} ({P['ratio_ci'][0]:.2f}–{P['ratio_ci'][1]:.2f}) "
                 f"| {P['i2']:.0f}% | {f'{pr[0]:.2f}–{pr[1]:.2f}' if pr else P.get('prediction_note', '')} | {br(g['sensitivity'].get('reported numbers only'))} "
                 f"| {br(g['sensitivity'].get('without high risk of bias'))} |")
    few = [g for g in res["groups"] if not g["pooled"]]
    L += ["", f"Not pooled (fewer than {res['min_studies']} studies): {len(few)} groups, "
          + "; ".join(" · ".join(g["group"].values()) + f" ({g['k']})" for g in few[:40]) + ("; …" if len(few) > 40 else ""),
          "", "Rows not used: " + "; ".join(f"{k}: {v}" for k, v in res["not_used"].items()) + "."]
    if res["possible_duplicates"]:
        L += ["", "Possible duplicates (the same effect and variance in one group; if they report one trial, list them under "
              "`meta_analysis.same_study`): " + "; ".join(f"{' and '.join(x['studies'])} in {' · '.join(x['group'].values())}"
                                                          for x in res["possible_duplicates"]) + "."]
    (d / "summary.md").write_text("\n".join(L) + "\n")
    return {"dir": str(d), "rows": res["rows"], "rows_used": res["rows_used"], "pooled_groups": len(pooled), "groups": len(res["groups"]),
            "not_used": res["not_used"], "possible_duplicates": res["possible_duplicates"]}
