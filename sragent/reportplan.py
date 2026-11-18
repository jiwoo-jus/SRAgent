"""A planned report: what the review's readers should see, decided before it is written.

The standard report (stages/report.py) lists everything for verification. A report for readers is planned: which
message, which sections in which order, which figures and tables carry them. The plan is a YAML file beside the run
file (`report_plan.yaml`), proposed by the agent from the question and from what the extracted data can show, changed
by the review team, and rendered here with no model call:

  title, kicker, summary: [key messages]
  concepts:                        # how extracted results are grouped for this review's figures
    column: analyte_name           # the text column of the rows item the patterns are matched against
    domains: [{key, label, color}]
    items:   [{name, domain, pattern}]            # regular expressions, any case; a result can match several
  dose: {value: aspirin_dose, unit: aspirin_dose_unit, factors: {uM: 1, mM: 1000, "ug/mL": 5.55}, band: [10, 100], band_label}
  sections:
    - title: ...
      content:
        - p: "Text with **bold**, a citation [@31004367], an author citation [@@31004367], a computed pooled result
              {{pool:PlGF · pregnant women|ratio of means}}, a count {{n:included}}, and a reference to {fig:map}."
        - list: ["...", "..."]
        - figure: {id: map, type: evidence_map, caption: "..."}      # types: FIGURES below
        - table: {id: targets, caption: "...", columns: [...], rows: [[...], ...]}     # cells are text like p
        - table: {id: pooled, type: pooled, caption: "..."}                             # computed tables: TABLES below

Numbers in the text that the system can compute are written as tokens, so they follow the data. A citation must be
an included study; `check` lists what does not hold before anything is rendered.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from . import figlib, meta, rows as rowlib

SCALES = ("concentration", "clinical")      # the dose scales a plan may hold: see scale_of

PLAN_FILE = "report_plan.yaml"
OUT_FILE = "manuscript.html"
FIG_DIR = "manuscript_figures"
TIERS_DEFAULT = None


def plan_path(ctx) -> Path:
    return Path(ctx.cfg["_config_dir"]) / PLAN_FILE


def load(ctx, path: str | Path | None = None) -> dict:
    p = Path(path) if path else plan_path(ctx)
    if not p.exists():
        raise ValueError(f"no report plan: {p} (a report for readers is written from a plan; see sragent/reportplan.py)")
    return yaml.safe_load(p.read_text()) or {}


# ---------------------------------------------------------------- the data a plan draws on
def _number(v):
    """A dose as a number: 100, '9 250 000' (spaced thousands), '10-4' and '10^-4' (powers of ten as papers print
    them), '5 × 10-4'; the first of a range. "10-50" is a range, not 10 to the minus fifty, so a power of ten is
    only read where the exponent is one a paper would print."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = re.sub(r"(?<=\d)[   ](?=\d\d\d\b)", "", str(v or "").replace("−", "-").replace("–", "-").strip())
    m = re.match(r"^(?:(\d+(?:\.\d+)?)\s*[x×*]\s*)?10\s*\^?\s*(-\d{1,2})\b", s)
    if m and 1 <= -int(m.group(2)) <= 12:
        return float(m.group(1) or 1) * 10 ** int(m.group(2))
    m = re.match(r"^0\s*-\s*(\d+(?:\.\d+)?)", s)                 # "0-2 mM": the dose tested is the top of the range
    if m:
        return float(m.group(1))
    m = re.match(r"^(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def _numbers(v) -> list[float]:
    """Every dose a cell names: "1 to 50", "5 and 10 uM", "10^-5 to 10^-3 mol/L", "0-2 mmol/l". A range keeps both
    ends, so a study that went from 1 to 50 µM is not read as a study that used 1 µM."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return [float(v)] if v > 0 else []
    s = re.sub(r"(?<=\d)[   ](?=\d\d\d\b)", "", str(v or "").replace("−", "-").replace("–", "-").strip())

    def ten(m):                                    # a power of ten as papers print it, where the exponent is one
        e = int(m.group(2))
        return f" {float(m.group(1) or 1) * 10 ** e:.13f} " if 1 <= -e <= 12 else m.group(0)
    s = re.sub(r"(?:(\d+(?:\.\d+)?)\s*[x×*]\s*)?10\s*\^?\s*(-\d{1,2})(?![\d.])", ten, s)
    return [x for x in (float(t) for t in re.findall(r"\d+(?:\.\d+)?", s)) if x > 0]


def _unit(v) -> str:
    return re.sub(r"\s+", "", str(v or "")).replace("μ", "µ").replace("u", "µ", 1).lower()


def scale_of(dose: dict | None, key: str) -> dict:
    """One of the plan's dose scales: the concentration the cells were given (dose itself), or the daily dose the
    women took (dose.clinical). They are not comparable, so each has its own unit, factors, reference band and
    (optional) `max`: a dose above it is not a dose anyone gave, so the unit as printed cannot be what was done."""
    if not dose:
        return {}
    sc = dict(dose) if key == "concentration" else {**{k: dose[k] for k in ("value", "unit") if k in dose}, **(dose.get("clinical") or {})}
    return sc if sc.get("factors") else {}


def dose_span(row: dict, dose: dict | None, key: str):
    """The row's dose on that scale as (lowest, highest): one number where the study used one."""
    sc = scale_of(dose, key)
    if not sc:
        return None
    vals = _numbers(row.get(sc["value"]))
    unit = _unit(row.get(sc.get("unit")))
    top = float(sc["max"]) if sc.get("max") else None
    for k, f in (sc.get("factors") or {}).items():
        if _unit(k) == unit:
            on = [v * float(f) for v in vals if v > 0]
            on = [v for v in on if top is None or v <= top]     # a unit the paper cannot mean (see scale_of)
            return (min(on), max(on)) if on else None
    return None


def dose_on(row: dict, dose: dict | None, key: str):
    """The row's dose on that scale (factors: unit as written -> factor), None when its unit is not one of them."""
    sc = scale_of(dose, key)
    if not sc:
        return None
    span = dose_span(row, dose, key)
    return span[0] if span else None


def dose_scale(row: dict, dose: dict | None) -> str | None:
    """Which scale the row's dose is on, if any."""
    return next((k for k in SCALES if dose_on(row, dose, k) is not None), None)


def concentration(row: dict, dose: dict | None):
    return dose_on(row, dose, "concentration")


def sig(v: float) -> str:
    """A dose as it reads: 0.5, 10, 3,000."""
    return f"{v:,.0f}" if v >= 100 else f"{v:.3g}" if v else "0"


def band_note(lo: float, hi: float, band) -> tuple[str, str]:
    """(is it the dose women get?, how it compares) for a dose or range against a reference band."""
    if not band:
        return "", ""
    b0, b1 = float(band[0]), float(band[1])
    if hi < b0:
        return "no, below it", f"{sig(b0 / hi)}× below the band"
    if lo > b1:
        times = sig(lo / b1) if sig(lo / b1) == sig(hi / b1) else f"{sig(lo / b1)}–{sig(hi / b1)}"
        return "no, above it", f"{times}× above the band"
    if lo >= b0 and hi <= b1:
        return "yes", "within the band"
    if lo < b0:
        return "partly", "from below the band into it" if hi <= b1 else f"from below the band to {sig(hi / b1)}× above it"
    return "partly", f"from inside the band to {sig(hi / b1)}× above it"


class Data:
    """The review's results as a plan sees them: the rows that count (the meta-analysis plan's include and exclude
    rules), each with its concepts, model tier, direction and concentration; the pooled groups; the risk of bias."""

    def __init__(self, ctx, plan: dict):
        st = ctx.store
        self.ctx, self.plan = ctx, plan
        self.mplan = meta.plan_of(ctx.topic)
        con = plan.get("concepts") or {}
        self.domains = {d["key"]: d for d in con.get("domains") or []}
        self.items = [{**i, "rx": re.compile(i["pattern"], re.I)} for i in con.get("items") or []]
        cls = next(iter((self.mplan or {}).get("classify") or []), {})       # what was measured: the pooling's own classes
        self.readout_set = str(cls["set"]) if cls else None
        self.readout_order = [str(r["label"]) for r in cls.get("rules") or []]
        self.settings = [{**g, "tiers": list(g.get("tiers") or [])} for g in plan.get("settings") or []]
        col = con.get("column")
        derived = {str(c.get("set")) for c in (self.mplan or {}).get("classify") or []}     # classes, not row columns
        tier = next((g for g in reversed((self.mplan or {}).get("group_by") or []) if g not in derived), None)
        self.tier_col = con.get("tier_column") or tier
        dcol = ((self.mplan or {}).get("columns") or {}).get("direction") or con.get("direction_column") or "direction"
        self.included = st.included_ids()
        self.results = []
        field = (self.mplan or {}).get("field")
        for rid in self.included:
            ext = st.get(f"ext:{rid}")
            if not field or not ext or ext["status"] != "active":
                continue
            for i, r in enumerate(((ext["data"]["fields"].get(field) or {}).get("rows")) or [], 1):
                if meta.left_out(r, self.mplan):
                    continue
                text = str(r.get(col) or "")
                res = {"rid": rid, "row": i, "concepts": [it["name"] for it in self.items if it["rx"].search(text)],
                       "tier": r.get(self.tier_col), "dir": r.get(dcol), "conc": concentration(r, plan.get("dose")),
                       "readout": self.readout_of(r), "scale": dose_scale(r, plan.get("dose")), "raw": r}
                res["given"] = self.given_of(res)
                self.results.append(res)
        self.pooling = meta.analyse(ctx) if self.mplan else {"groups": [], "rows": 0, "rows_used": 0, "not_used": {}, "min_studies": 0}
        self.pooled = {(" · ".join(g["group"].values()), g["measure"]): g for g in self.pooling["groups"] if g["pooled"]}
        self.tiers = plan.get("tiers") or [t for t, _ in Counter(r["tier"] for r in self.results if r["tier"]).most_common()]
        for r in self.results:
            r["setting"] = self.setting_of(r)

    def readout_of(self, row: dict) -> str | None:
        """What kind of change a result measured. These are the classes the quantitative synthesis groups by
        (meta_analysis.classify), so a table and a pooled estimate always mean the same thing by "protein level"."""
        return meta.classes(row, self.mplan).get(self.readout_set) if self.readout_set else None

    def label_of(self, item: dict) -> str:
        """A concept as a figure or table names it. A molecule says where the review's cascades place it: the
        function it is upstream of, so that every row reads at one level and a pathway is not left hanging."""
        ends = [self.short_of(n) for n in item.get("leads_to") or []]
        return f"{item['name']} \u2192 {', '.join(ends)}" if ends else item["name"]

    def short_of(self, name: str) -> str:
        it = next((i for i in self.items if i["name"] == name), None)
        return (it or {}).get("short") or name

    def given_of(self, res: dict) -> str:
        """Where the aspirin was given. A dose written as a daily dose was taken by a pregnant woman; a dose written
        as a concentration was added to the culture or the sample. Tissue from a woman who took aspirin is not an
        in-vitro exposure, however the tissue is studied afterwards, and serum that had aspirin added to it in a
        tube is not an in-vivo one. Where a row states no dose, the model decides."""
        by_scale = {"clinical": "in vivo", "concentration": "in vitro"}
        if res.get("scale") in by_scale:
            return by_scale[res["scale"]]
        women = [g for g in self.settings if g.get("given") == "in vivo" and res.get("tier") in (g.get("tiers") or [])]
        return "in vivo" if women and res.get("tier") in (self.plan.get("in_vivo_tiers") or ["pregnant women"]) else "in vitro"

    def setting_of(self, res: dict) -> str | None:
        """Which setting a result belongs to: the first whose models include its model and whose exposure, where it
        states one, is the one that result had. Nothing is counted across settings."""
        return next((g["key"] for g in self.settings
                     if res.get("tier") in g["tiers"] and g.get("given", res.get("given")) == res.get("given")), None)

    def in_setting(self, res: dict, key, tiers=None) -> bool:
        return (key is None or res.get("setting") == key) and (not tiers or res.get("tier") in tiers)

    def results_in(self, key, tiers=None) -> list:
        return [r for r in self.results if self.in_setting(r, key, tiers)]

    def setting(self, key) -> dict:
        return next((g for g in self.settings if g["key"] == key),
                    {"key": key, "label": str(key or "all studies"), "short": "", "tiers": []})

    def setting_keys(self) -> list:
        return [g["key"] for g in self.settings] or [None]

    def tiers_for(self, key) -> list | None:
        """The models of one setting (None: every model the review has)."""
        if not key:
            return None
        return [t for t in self.tiers if t in self.setting(key)["tiers"]] or self.setting(key)["tiers"]

    def readouts(self, concept: str, tiers=None, setting=None) -> dict:
        """{readout: {direction: [studies]}} for a concept: what was measured and which way it moved."""
        out: dict = defaultdict(lambda: defaultdict(set))
        for r in self.results:
            if concept in r["concepts"] and r["readout"] and self.in_setting(r, setting, tiers):
                out[r["readout"]][r["dir"] or "not stated"].add(r["rid"])
        return {k: {d: sorted(v) for d, v in sorted(by.items(), key=lambda x: -len(x[1]))} for k, by in out.items()}

    def by_concept(self, setting=None, tiers=None) -> dict:
        """{concept: {tier: {study: direction}}} within one setting (all of them when none is named)."""
        acc: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
        for r in self.results:
            if not self.in_setting(r, setting, tiers):
                continue
            for c in r["concepts"]:
                acc[c][r["tier"]][r["rid"]].add(r["dir"])
        return {c: {t: {rid: figlib.study_direction(ds) for rid, ds in by.items()} for t, by in tiers.items()} for c, tiers in acc.items()}

    def study_directions(self, concept: str, tiers=None, setting=None) -> dict:
        """{study: one direction} for a concept in one setting: a study measured in two models, or with results in
        both directions, counts once (as "mixed" when its results disagree)."""
        acc: dict = defaultdict(set)
        for r in self.results:
            if concept in r["concepts"] and self.in_setting(r, setting, tiers):
                acc[r["rid"]].add(r["dir"])
        return {rid: figlib.study_direction(ds) for rid, ds in acc.items()}

    def dose_verdicts(self, setting, scale: str = "concentration") -> dict:
        """{study: is the dose it used the dose women receive?} for one setting, on one dose scale. A study that used
        several doses is judged on the whole range it covered (band_note)."""
        per: dict = defaultdict(list)
        for r in self.results:
            if self.in_setting(r, setting) and dose_span(r["raw"], self.plan.get("dose"), scale):
                per[r["rid"]].append(r)
        band = scale_of(self.plan.get("dose"), scale).get("band")
        out = {}
        for rid, res in per.items():
            lo, hi = dose_span_of(self, res, scale)
            out[rid] = band_note(lo, hi, band)[0]
        return out

    def counts(self) -> dict:
        """The numbers a report may state about this review. Everything a section claims about the data comes from
        here or from a pooled group, so no count is ever typed by hand."""
        from .stages import report as _report
        ext = [r for r in self.included if (self.ctx.store.get(f"ext:{r}") or {}).get("status") == "active"]
        f = _report.prisma(self.ctx)
        st = self.ctx.store
        import datetime as _dt
        when = (st.get("search") or {}).get("updated")
        flow = {"identified": f["identified"], "screened": f["screened"], "sought": f["reports_sought"],
                "assessed": f["fulltext_assessed"], "excluded_fulltext": f["excluded_fulltext"],
                "search_date": _dt.date.fromtimestamp(when).isoformat() if when else "not recorded",
                "human_decisions": sum(1 for n in st.by_type("screen") + st.by_type("eligibility")
                                       if n["status"] == "active" and n["data"].get("human_decision")),
                "high_risk": sum(1 for r in self.included if ((st.get(f"rob:{r}") or {}).get("data") or {}).get("overall") == "high")}
        left_out = {f"rows_left_out:{k.split(': ', 1)[1] if k.startswith('left out by rule: ') else k}": v
                    for k, v in (self.pooling.get("not_used") or {}).items()}
        return {**flow, **left_out,
                "included": len(self.included), "extracted": len(ext), "rows": self.pooling["rows"], "rows_used": self.pooling["rows_used"],
                "results": len(self.results), "pooled_groups": len(self.pooled),
                "studies_with_results": len({r["rid"] for r in self.results}),
                **{f"studies:{t}": len({r["rid"] for r in self.results if r["tier"] == t}) for t in self.tiers},
                **{f"studies:{g['key']}": len({r["rid"] for r in self.results if r["setting"] == g["key"]}) for g in self.settings}}


# ---------------------------------------------------------------- figures and computed tables
def _tiers(D: Data, spec: dict) -> list:
    """The models a figure or table covers: its own list, the models of the setting it names, or all of them."""
    return spec.get("tiers") or (D.tiers_for(spec["setting"]) if spec.get("setting") else D.tiers)


def _concept_rows(D: Data, spec: dict) -> list[dict]:
    want = spec.get("domains")
    by = D.by_concept(spec.get("setting"), spec.get("tiers"))
    least = int(spec.get("min_studies", 1))
    out = []
    for it in D.items:
        if want and it["domain"] not in want:
            continue
        tiers = by.get(it["name"]) or {}
        if len({rid for t in tiers.values() for rid in t}) >= least:
            out.append({"name": it["name"], "label": D.label_of(it), "domain": it["domain"], "tiers": tiers})
    out.sort(key=lambda r: list(D.domains).index(r["domain"]))      # a domain is one block, never two
    return out


def fig_evidence_map(D: Data, spec: dict) -> str:
    rws = _concept_rows(D, spec)
    return figlib.evidence_map([r["label"] for r in rws], D.tiers, {(r["label"], t): len(r["tiers"].get(t) or {}) for r in rws for t in D.tiers},
                               {r["label"]: D.domains[r["domain"]]["label"] for r in rws})


def fig_direction_plot(D: Data, spec: dict) -> str:
    rws = _concept_rows(D, spec)
    return figlib.direction_plot([{"label": r["label"], "by": {t: list((r["tiers"].get(t) or {}).values()) for t in D.tiers}} for r in rws],
                                 D.tiers, {r["label"]: D.domains[r["domain"]]["label"] for r in rws})


def fig_mechanism_map(D: Data, spec: dict) -> str:
    """Panels are the plan's domains; an item's direction and its number of studies come from the data (the direction
    most studies report; "mixed" when no direction has a majority), unless the plan states them for an item."""
    by = D.by_concept()
    tiers = _tiers(D, spec)
    panels = []
    for p in spec["panels"]:
        dom = D.domains[p["domain"]]
        items = []
        for name in p["items"]:
            fixed = name if isinstance(name, dict) else {}
            label = fixed.get("name") or name
            dirs = list(D.study_directions(label, tiers, spec.get("setting")).values())
            c = Counter(d for d in dirs if d in ("increase", "decrease", "no change", "mixed"))
            top = c.most_common(1)[0] if c else ("not stated", 0)
            d = fixed.get("dir") or (top[0] if top[1] > len(dirs) / 2 else "mixed" if dirs else "not stated")
            note = fixed.get("note") or (f"{top[1]} of {len(dirs)} " + ("study" if len(dirs) == 1 else "studies") if dirs else "")
            items.append({"label": fixed.get("label") or label, "dir": d, "note": note})
        panels.append({"title": dom["label"], "color": dom.get("color", "#eef3f8"), "items": items})
    return figlib.mechanism_map(panels, spec.get("inputs") or [], spec.get("intervention", ""), spec.get("outputs") or [], spec.get("loop", ""))


def fig_dose_plot(D: Data, spec: dict) -> str:
    dose = D.plan.get("dose") or {}
    tiers = _tiers(D, spec)
    dom_of = {i["name"]: i["domain"] for i in D.items}
    lanes = [D.domains[k]["label"] for k in (spec.get("domains") or list(D.domains))]
    pts = []
    for r in D.results:
        if r["conc"] is None or not D.in_setting(r, spec.get("setting"), tiers) or r["dir"] not in ("increase", "decrease", "no change"):
            continue
        for dk in dict.fromkeys(dom_of[c] for c in r["concepts"]):      # in the order the concepts matched, not a set's
            pts.append({"x": r["conc"], "lane": D.domains[dk]["label"], "dir": r["dir"]})
    band = dose.get("band")
    return figlib.dose_plot(pts, lanes, tuple(band) if band else None, dose.get("band_label", ""), dose.get("unit_label", "µM"),
                            float(spec.get("from", 0.01)), float(spec.get("to", 100000)))


def fig_rob_summary(D: Data, spec: dict) -> str:
    st = D.ctx.store
    acc: dict = defaultdict(Counter)
    n = 0
    only = set(spec.get("tiers") or (D.tiers_for(spec["setting"]) if spec.get("setting") else []))
    tier_of = defaultdict(set)
    for r in D.results:
        if D.in_setting(r, spec.get("setting"), spec.get("tiers")):
            tier_of[r["rid"]].add(r["tier"])
    doms = [d["name"] for d in D.ctx.topic.get("rob_domains") or []]
    for rid in D.included:
        rob = st.get(f"rob:{rid}")
        if not rob or rob["status"] != "active" or (only and not (tier_of[rid] & only)):
            continue
        n += 1
        for d in rob["data"].get("domains") or []:
            if d.get("domain") in doms:
                acc[d["domain"]][d.get("judgment") or "unclear"] += 1
    return figlib.rob_summary([(k.replace("_", " ").capitalize(), dict(acc[k])) for k in doms], f"{n} studies")


def fig_forest(D: Data, spec: dict) -> str:
    hits = pool_of(D, spec["group"])
    if len(hits) != 1:
        raise ValueError(f"figure {spec.get('id')}: {'no' if not hits else len(hits)} pooled group(s) {spec['group']!r}")
    return meta.forest_svg(D.pooled[hits[0]])


def fig_prisma(D: Data, spec: dict) -> str:
    from .stages import report
    p = (D.ctx.store.get("protocol") or {}).get("data") or {}
    html = report._prisma_figure(D.ctx, report._flow(D.ctx), {c["id"]: c for c in p.get("criteria", [])}, 0)
    html = re.sub(r"<figcaption>.*?</figcaption>", "", html, flags=re.S)      # the plan writes the caption
    return re.sub(r"^<figure[^>]*>|</figure>$", "", html.strip())             # its own grid, not wrapped again


def fig_pathway(D: Data, spec: dict) -> str:
    """A signalling cascade the plan declares. A node with a `concept` is annotated from the data: the direction most
    studies report, how many, and (unless the node names its own `sub`) the kind of change they measured. With
    `settings`, a node carries one line per setting, so the cells and the women are read side by side and never
    added together; the first setting given carries the mark and the readout line."""
    keys = spec.get("settings") or [None]
    nodes = []
    for nd in spec["nodes"]:
        n = dict(nd)
        concept = n.pop("concept", None)
        if concept:
            lines, lead = [], None
            for key in keys:
                tiers = spec.get("tiers")
                d = D.study_directions(concept, tiers, key)
                if not d:
                    continue
                c = Counter(d.values())
                top = c.most_common(1)[0]
                way = top[0] if top[1] > len(d) / 2 else "mixed"
                short = D.setting(key).get("short") or D.setting(key)["label"]
                lines.append((f"{short}: " if key else "") + f"{top[1]} of {len(d)} {way}" if key else
                             f"{top[1]} of {len(d)} " + ("study" if len(d) == 1 else "studies"))
                if lead is None:
                    lead, reads = (way, tiers), D.readouts(concept, tiers, key)
                    n.setdefault("dir", way)
                    if reads and not n.get("sub"):
                        n["sub"] = max(reads, key=lambda k: sum(len(v) for v in reads[k].values()))
            n.setdefault("note", lines or "not measured")
        nodes.append(n)
    return figlib.pathway_map(nodes, spec.get("edges") or [], spec.get("heading", ""), spec.get("note", ""), spec.get("flow", "right"))


FIGURES = {"prisma": fig_prisma, "pathway": fig_pathway, "evidence_map": fig_evidence_map, "direction_plot": fig_direction_plot, "mechanism_map": fig_mechanism_map,
           "dose_plot": fig_dose_plot, "rob_summary": fig_rob_summary, "forest": fig_forest}


def pool_of(D: Data, name: str) -> list:
    """The pooled groups a plan's name matches: "<value> · <value> ... | <measure>". A dimension may be left out
    where what is left names one group; where it names two, the name is ambiguous and `check` says so, which is the
    point of naming the setting and the readout in the first place."""
    want, _, measure = str(name).partition("|")
    parts = [x.strip() for x in want.split("·") if x.strip()]
    measure = measure.strip()
    return [k for k in D.pooled if (not measure or k[1] == measure) and set(parts) <= {x.strip() for x in k[0].split("·")}]


def pool_text(g: dict) -> str:
    P = g["pooled"]
    return f"{P['ratio']:.2f} (95% CI {P['ratio_ci'][0]:.2f} to {P['ratio_ci'][1]:.2f}; {P['k']} studies; I² = {P['i2']:.0f}%)"


def group_labels(D: Data, spec: dict) -> list[str]:
    nice = {**{"model_tier": "Model", "outcome_family": "Outcome", "readout": "What was measured"},
            **(D.plan.get("group_labels") or {}), **(spec.get("group_labels") or {})}
    return [nice.get(g, str(g).replace("_", " ").capitalize()) for g in D.pooling.get("group_by") or ["Group"]]


def table_pooled(D: Data, spec: dict) -> tuple[list[str], list[list[str]]]:
    """One line per pooled group. Each grouping dimension is its own column, so what was pooled with what is
    readable: a serum concentration and the expression of the same protein are different lines."""
    def br(x):
        if not x:                       # the analysis does not apply: every study is already in the main estimate
            return "same as the main estimate"
        if "ratio" not in x:
            return f"not pooled: {x['k']} stud{'y' if x['k'] == 1 else 'ies'} left"
        return f"{x['ratio']:.2f} ({x['ratio_ci'][0]:.2f}–{x['ratio_ci'][1]:.2f}), k = {x['k']}"
    want, gb = spec.get("groups"), D.pooling.get("group_by") or []
    out = []
    for (name, measure), g in D.pooled.items():
        if want and not any(re.search(w, name, re.I) for w in want):
            continue
        P = g["pooled"]
        pr = P.get("ratio_prediction")
        out.append([g["group"].get(k, "") for k in gb] +
                   [measure, str(P["k"]), f"**{P['ratio']:.2f}** ({P['ratio_ci'][0]:.2f}–{P['ratio_ci'][1]:.2f})", f"{P['i2']:.0f}%",
                    f"{pr[0]:.2f}–{pr[1]:.2f}" if pr else P.get("prediction_note", ""), br(g["sensitivity"].get("reported numbers only")),
                    br(g["sensitivity"].get("without high risk of bias")), f"{sum(1 for s in g['studies'] if s['estimated'])} of {P['k']}"])
    return (group_labels(D, spec) + ["Measure", "Studies", "Ratio (95% CI)", "I²", "Prediction interval", "Reported numbers only",
                                     "Without high risk of bias", "Studies with figure estimates"], out)


def consensus(d: dict, consistent: float = 0.75, least: int = 3) -> str:
    """One direction for an item, from the studies that measured it: the direction most of them report and how far
    they agree. Where no direction has a majority the honest answer is that the evidence does not settle it."""
    c = Counter(v for v in d.values() if v != "not stated")
    if not c:
        return "not stated"
    top, n = c.most_common(1)[0]
    k = sum(c.values())                       # studies that gave a direction; the rest are counted as not stated
    share = n / k
    silent = f"; {len(d) - k} not stated" if len(d) > k else ""
    if len(c) > 1 and share <= 0.5:
        return "inconsistent" + silent
    if k < least:
        return f"**{top}**, {k} stud{'y' if k == 1 else 'ies'} only" + silent
    return f"**{top}** ({'consistent' if share >= consistent else 'majority'})" + silent


def dose_span_of(D: Data, results: list, scale: str):
    """The whole range of doses behind a set of results, both ends."""
    spans = [v for r in results if (v := dose_span(r["raw"], D.plan.get("dose"), scale)) is not None]
    return (min(lo for lo, _ in spans), max(hi for _, hi in spans)) if spans else None


def dose_range(D: Data, results: list, scale: str) -> str:
    """The doses behind a set of results, as a range on one scale."""
    span = dose_span_of(D, results, scale)
    if not span:
        return ""
    lab = scale_of(D.plan.get("dose"), scale).get("unit_label", "")
    lo, hi = span
    return (f"{sig(lo)} {lab}" if lo == hi else f"{sig(lo)}–{sig(hi)} {lab}").strip()


def table_concepts(D: Data, spec: dict) -> tuple[list[str], list[list[str]]]:
    """One line per molecule and setting, with the doses used, one summarising direction and the studies behind it.
    Cells and women are separate lines: a protein measured in culture and the same protein in blood are not one
    result, and nothing here counts a study on both sides."""
    thr, least = float(spec.get("consistent", 0.75)), int(spec.get("consensus_min_studies", 3))
    out = []
    for r in _concept_rows(D, spec):
        first = True
        for key in D.setting_keys():
            tiers = D.tiers_for(key)
            d = D.study_directions(r["name"], setting=key)
            if not d:
                continue
            c = Counter(d.values())
            res = [x for x in D.results if r["name"] in x["concepts"] and D.in_setting(x, key)]
            doses = "; ".join(x for x in (dose_range(D, res, sc) for sc in SCALES) if x)
            with_dose = len({x["rid"] for x in res if any(dose_span(x["raw"], D.plan.get("dose"), sc) for sc in SCALES)})
            if doses and with_dose < len(d):   # a range drawn from some of the studies is not the range of all of them
                doses += f" ({with_dose} of {len(d)} studies)"
            out.append([D.domains[r["domain"]]["label"] if first else "", r["label"] if first else "",
                        D.setting(key)["label"], str(len(d)), doses or "not stated",
                        consensus(d, thr, least),
                        " · ".join(f"{k} {c[k]}" for k in ("increase", "decrease", "mixed", "no change", "not stated") if c[k]),
                        ", ".join(f"{t} {len({x['rid'] for x in res if x['tier'] == t})}" for t in (tiers or D.tiers)
                                  if any(x["tier"] == t for x in res)),      # the models of this setting, not of all of them
                        " ".join(f"[@{x}]" for x in sorted(d)[:12])])
            first = False
    return (["Function", "Molecule or readout", "Setting", "Studies", "Aspirin dose", "Direction overall",
             "Direction with aspirin (studies)", "Models (studies)", "References"], out)


def table_dose_check(D: Data, spec: dict) -> tuple[list[str], list[list[str]]]:
    """Whether each study's aspirin dose is the dose a pregnant woman receives. The cell and tissue studies are
    compared with the plasma salicylate concentration low-dose aspirin reaches; the clinical studies with the daily
    dose itself. A study that used several doses appears as the range it covered."""
    dose = D.plan.get("dose") or {}
    out = []
    for key in D.setting_keys():
        here = D.results_in(key)
        for scale in SCALES:
            sc = scale_of(dose, scale)
            if not sc:
                continue
            per: dict = defaultdict(list)
            for r in here:
                if dose_on(r["raw"], dose, scale) is not None:
                    per[r["rid"]].append(r)
            for rid, res in sorted(per.items(), key=lambda t: dose_span_of(D, t[1], scale)[0]):
                lo, hi = dose_span_of(D, res, scale)
                written = sorted(dict.fromkeys(f"{r['raw'].get(sc['value'])} {r['raw'].get(sc.get('unit')) or ''}".strip() for r in res),
                             key=lambda t: (_number(t) or 0, t))        # doses that read the same number keep one order
                equiv, how = band_note(lo, hi, sc.get("band"))
                out.append([f"[@@{rid}]", D.setting(key)["label"], "; ".join(written[:6]) + (" …" if len(written) > 6 else ""),
                            dose_range(D, res, scale), equiv, how, str(len(res))])
    least = int(spec.get("min_results", 1))
    return (["Study", "Setting", "Aspirin as given", "On a common scale", "The dose women get?",
             "How it compares with low-dose aspirin", "Results"], [r for r in out if int(r[-1]) >= least])


def table_modulation(D: Data, spec: dict) -> tuple[list[str], list[list[str]]]:
    """Per molecule and setting, what kind of change was measured and in which direction: expression, activation,
    activity, release or function, each with the studies that report it. A level in blood and a level in culture are
    different measurements in different settings, so they are never one line."""
    want = spec.get("items") or [i["name"] for i in sorted(D.items, key=lambda i: list(D.domains).index(i["domain"]))]
    order = {name: i for i, name in enumerate(D.readout_order)}
    out = []
    for name in want:
        it = next((i for i in D.items if i["name"] == name), {})
        dom = D.domains.get(it.get("domain", ""), {})
        label, first = D.label_of(it) if it else name, True
        for key in D.setting_keys():
            reads = D.readouts(name, setting=key)
            for rd in sorted(reads, key=lambda k: order.get(k, 99)):
                by = reads[rd]
                out.append([dom.get("label", "") if first else "", label if first else "", D.setting(key)["label"], rd,
                            " · ".join(f"{d} {len(v)}" for d, v in by.items()) or "not stated",
                            " ".join(f"[@{x}]" for x in sorted({r for v in by.values() for r in v})[:10])])
                first = False
    return ["Function", "Molecule or readout", "Setting", "What was measured", "Direction (studies)", "References"], out


TABLES = {"pooled": table_pooled, "modulation": table_modulation, "concepts": table_concepts, "dose_check": table_dose_check}


# ---------------------------------------------------------------- text
class Refs:
    def __init__(self, st):
        self.st, self.order = st, []

    def n(self, rid: str) -> int:
        if rid not in self.order:
            self.order.append(rid)
        return self.order.index(rid) + 1

    def label(self, rid: str) -> str:
        from .store import record_of
        rec = record_of(self.st, rid)
        a = re.split(r"[,;]", str(rec.get("authors") or ""))[0].strip()
        many = len(re.split(r"[,;]", str(rec.get("authors") or ""))) > 1
        return f"{a.split()[0] if a else rid}{' et al.' if many else ''} {rec.get('year') or ''}".strip()


def cited(plan: dict) -> list[str]:
    return re.findall(r"@@?([A-Za-z0-9]+)", " ".join(re.findall(r"\[@[^\]]+\]", yaml.safe_dump(plan, allow_unicode=True, width=10 ** 9))))


def render_text(s: str, D: Data, refs: Refs, numbers: dict, counts: dict) -> str:
    s = figlib.x(s)

    def cite(m):
        parts = [p.strip() for p in m.group(1).split(";")]
        named = [p for p in parts if p.startswith("@@")]
        nums = ", ".join(f"<a href='#ref-{refs.n(p.lstrip('@'))}'>{refs.n(p.lstrip('@'))}</a>" for p in parts)
        lead = (", ".join(refs.label(p.lstrip("@")) for p in named) + " ") if named else ""
        return f"{lead}<span class='cite'>[{nums}]</span>"
    s = re.sub(r"\[((?:@@?[A-Za-z0-9]+)(?:\s*;\s*@@?[A-Za-z0-9]+)*)\]", cite, s)
    def pooled(m):
        hits = pool_of(D, m.group(1))
        return pool_text(D.pooled[hits[0]]) if len(hits) == 1 else "<span class='missing'>[not pooled]</span>"
    s = re.sub(r"\{\{pool:([^}]+)\}\}", pooled, s)
    s = re.sub(r"\{\{n:([^}]+)\}\}", lambda m: str(counts.get(m.group(1).strip(), "<span class='missing'>[?]</span>")), s)

    def dirs(m):
        concept, _, rest = m.group(1).partition("|")
        want, _, setting = rest.partition("|")
        d = D.study_directions(concept.strip(), setting=setting.strip() or None)
        if not d:
            return "<span class='missing'>[no such item]</span>"
        c = Counter(d.values())
        if not want.strip():
            top = c.most_common(1)[0]
            return f"{top[1]} of {len(d)} studies report {top[0]}"
        return f"{c[want.strip()]} of {len(d)}"
    s = re.sub(r"\{\{dir:([^}]+)\}\}", dirs, s)

    def doses(m):
        """{{dose:<setting>}} how many studies have a dose on the concentration scale; {{dose:<setting>|<scale>}} on
        that scale; a third part, a verdict of band_note, how many of them got that verdict."""
        parts = [x.strip() for x in m.group(1).split("|")]
        scale = parts[1] if len(parts) > 1 and parts[1] in SCALES else "concentration"
        want = parts[2] if len(parts) > 2 else parts[1] if len(parts) > 1 and parts[1] not in SCALES else ""
        v = D.dose_verdicts(parts[0], scale)
        if not v:
            return "<span class='missing'>[no doses on that scale]</span>"
        return str(sum(1 for x in v.values() if x == want)) if want else str(len(v))
    s = re.sub(r"\{\{dose:([^}]+)\}\}", doses, s)
    s = re.sub(r"\{(fig|tab):([A-Za-z0-9_-]+)\}", lambda m: f"<a href='#{m.group(1)}-{m.group(2)}'>{numbers.get((m.group(1), m.group(2)), '?')}</a>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)


# ---------------------------------------------------------------- check and render
def blocks(plan: dict):
    for s in plan.get("sections") or []:
        for b in s.get("content") or []:
            yield s, b


def _ambiguous(D: Data, name: str, what: str) -> str:
    hits = pool_of(D, name)
    if not hits:
        return f"{what}: no pooled group {name!r} (groups: {'; '.join(a + '|' + b for a, b in list(D.pooled)[:6])} ...)"
    return f"{what}: {name!r} names {len(hits)} pooled groups, say which: {'; '.join(a + '|' + b for a, b in hits)}"


def check(ctx, plan: dict) -> list[str]:
    """What is wrong with a plan (empty: it can be rendered): unknown figure or table types, citations of studies that
    are not included, pooled results that do not exist, references to figures that are not in the plan, bad patterns."""
    errs = []
    inc = set(ctx.store.included_ids())
    for rid in sorted(set(cited(plan)) - inc):
        errs.append(f"citation [@{rid}]: not an included study")
    con = plan.get("concepts") or {}
    doms = {d.get("key") for d in con.get("domains") or []}
    names = {it.get("name") for it in con.get("items") or []}
    for it in con.get("items") or []:
        if it.get("domain") not in doms:
            errs.append(f"concept {it.get('name')}: domain {it.get('domain')!r} is not listed under concepts.domains")
        for end in it.get("leads_to") or []:
            if end not in names:
                errs.append(f"concept {it.get('name')}: leads_to {end!r} is not another item under concepts.items")
        try:
            re.compile(str(it.get("pattern")))
        except re.error as e:
            errs.append(f"concept {it.get('name')}: pattern: {e}")
    if errs:
        return errs
    D = Data(ctx, plan)
    ids = set()
    for _, b in blocks(plan):
        kind = next(iter(b))
        v = b[kind]
        if kind == "figure":
            ids.add(("fig", v.get("id")))
            if v.get("type") not in FIGURES:
                errs.append(f"figure {v.get('id')}: type {v.get('type')!r}; one of {', '.join(FIGURES)}")
            elif v["type"] == "forest" and len(pool_of(D, v.get("group", ""))) != 1:
                errs.append(_ambiguous(D, v.get("group", ""), f"figure {v.get('id')}"))
            elif v["type"] == "pathway":
                known = {n.get("id") for n in v.get("nodes") or []}
                names = {i["name"] for i in D.items}
                for e in v.get("edges") or []:
                    for side in ("from", "to"):
                        if e.get(side) not in known:
                            errs.append(f"figure {v.get('id')}: edge {side} {e.get(side)!r} is not a node of this pathway")
                for nd in v.get("nodes") or []:
                    if nd.get("concept") and nd["concept"] not in names:
                        errs.append(f"figure {v.get('id')}: node {nd.get('id')}: concept {nd['concept']!r} is not under concepts.items")
        elif kind == "table":
            ids.add(("tab", v.get("id")))
            if v.get("type") and v["type"] not in TABLES:
                errs.append(f"table {v.get('id')}: type {v.get('type')!r}; one of {', '.join(TABLES)} (or columns and rows)")
            elif not v.get("type") and any(len(r) != len(v.get("columns") or []) for r in v.get("rows") or []):
                errs.append(f"table {v.get('id')}: a row does not have one cell per column")
        elif kind not in ("p", "list", "h"):
            errs.append(f"unknown block {kind!r} (p, h, list, figure, table)")
    text = yaml.safe_dump(plan, allow_unicode=True, width=10 ** 9)
    names = {i["name"] for i in D.items}
    for k in dict.fromkeys(re.findall(r"\{\{dir:([^}|]+)", text)):
        if k.strip() not in names:
            errs.append(f"{{{{dir:{k.strip()}}}}}: not an item under concepts.items")
    for k in dict.fromkeys(re.findall(r"\{\{pool:([^}]+)\}\}", text)):
        if len(pool_of(D, k)) != 1:
            errs.append(_ambiguous(D, k, f"{{{{pool:{k}}}}}"))
    keys = {g["key"] for g in D.settings}
    for k in dict.fromkeys(x.split("|")[2].strip() for x in re.findall(r"\{\{dir:([^}]+)\}\}", text) if x.count("|") > 1):
        if k not in keys:
            errs.append(f"{{{{dir:...|{k}}}}}: not a setting of this plan (settings: {', '.join(sorted(keys)) or 'none'})")
    for kind, i in dict.fromkeys(re.findall(r"\{(fig|tab):([A-Za-z0-9_-]+)\}", text)):
        if (kind, i) not in ids:
            errs.append(f"{{{kind}:{i}}}: no such {'figure' if kind == 'fig' else 'table'} in the plan")
    return errs


CSS = """
body{font:16px/1.6 Georgia,'Times New Roman',serif;color:#1c1c1c;max-width:1000px;margin:36px auto;padding:0 28px;background:#fff}
h1{font:600 30px/1.25 Helvetica,Arial,sans-serif;margin:6px 0 10px} h2{font:600 21px Helvetica,Arial,sans-serif;margin:38px 0 10px;border-bottom:1px solid #ddd;padding-bottom:5px}
h3{font:600 16px Helvetica,Arial,sans-serif;margin:24px 0 6px}.kicker{font:12px Helvetica,Arial,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:#777}
.summary{background:#f4f7fa;border-left:4px solid #1f4e79;padding:12px 18px;margin:18px 0}.summary li{margin:5px 0}
figure{margin:26px 0;padding:0}figure .plot{overflow-x:auto;border:1px solid #e3e3e3;border-radius:6px;padding:10px;background:#fff}
figcaption,.cap{font:13.5px/1.45 Helvetica,Arial,sans-serif;color:#333;margin-top:8px}
table{border-collapse:collapse;font:13px/1.4 Helvetica,Arial,sans-serif;width:100%}th{background:#f1f3f5;text-align:left}
th,td{border:1px solid #d9d9d9;padding:6px 8px;vertical-align:top}.tblwrap{overflow-x:auto;margin:22px 0}
.cite a{color:#1f4e79;text-decoration:none}.refs li{font:13.5px/1.45 Helvetica,Arial,sans-serif;margin:5px 0}
.missing{background:#ffe1e1;color:#a00}.note{font:13px Helvetica,Arial,sans-serif;color:#666}
nav.toc{font:14px Helvetica,Arial,sans-serif;columns:2;margin:14px 0 6px}nav.toc a{color:#1f4e79;text-decoration:none}
@media print{body{margin:0;max-width:none}figure,.tblwrap{break-inside:avoid}}
"""


def render(ctx, plan: dict | None = None) -> dict:
    """Write <run folder>/manuscript.html (figures inline) and each figure as manuscript_figures/<id>.svg. Returns
    {path, figures, tables, references, problems}; a plan with problems is not rendered."""
    from .stages import report
    from .store import record_of
    plan = plan or load(ctx)
    errs = check(ctx, plan)
    if errs:
        return {"ok": False, "problems": errs, "message": f"the report plan has {len(errs)} problem(s): " + "; ".join(errs[:4])}
    D = Data(ctx, plan)
    refs, counts = Refs(ctx.store), D.counts()
    numbers, nf, nt = {}, 0, 0
    for _, b in blocks(plan):
        if "figure" in b:
            nf += 1
            numbers[("fig", b["figure"]["id"])] = f"Figure {nf}"
        elif "table" in b:
            nt += 1
            numbers[("tab", b["table"]["id"])] = f"Table {nt}"
    T = lambda s: render_text(s, D, refs, numbers, counts)
    fdir = Path(ctx.run_dir) / FIG_DIR
    fdir.mkdir(exist_ok=True)
    for old in fdir.glob("*.svg"):
        old.unlink()
    body, toc = [], []
    for k, s in enumerate(plan.get("sections") or [], 1):
        sid = s.get("id") or f"s{k}"
        toc.append(f"<div><a href='#{sid}'>{figlib.x(s['title'])}</a></div>")
        body.append(f"<h2 id='{sid}'>{figlib.x(s['title'])}</h2>")
        for b in s.get("content") or []:
            if "p" in b:
                body.append(f"<p>{T(b['p'])}</p>")
            elif "h" in b:
                body.append(f"<h3>{T(b['h'])}</h3>")
            elif "list" in b:
                body.append("<ul>" + "".join(f"<li>{T(i)}</li>" for i in b["list"]) + "</ul>")
            elif "figure" in b:
                f = b["figure"]
                svg = FIGURES[f["type"]](D, f)
                if svg.startswith("<svg"):
                    (fdir / f"{f['id']}.svg").write_text(svg)
                    svg = f"<div class='plot'>{svg}</div>"
                body.append(f"<figure id='fig-{f['id']}'>{svg}<figcaption><b>{numbers[('fig', f['id'])]}.</b> "
                            f"{T(f.get('caption', ''))}</figcaption></figure>")
            elif "table" in b:
                t = b["table"]
                cols, rws = TABLES[t["type"]](D, t) if t.get("type") else (t["columns"], t["rows"])
                body.append(f"<div class='tblwrap' id='tab-{t['id']}'><p class='cap'><b>{numbers[('tab', t['id'])]}.</b> {T(t.get('caption', ''))}</p>"
                            "<table><thead><tr>" + "".join(f"<th>{figlib.x(c)}</th>" for c in cols) + "</tr></thead><tbody>"
                            + "".join("<tr>" + "".join(f"<td>{T(str(c))}</td>" for c in r) + "</tr>" for r in rws) + "</tbody></table>"
                            + (f"<p class='note'>{T(t['foot'])}</p>" if t.get("foot") else "") + "</div>")
    summary = "".join(f"<li>{T(i)}</li>" for i in plan.get("summary") or [])
    ref_html = "".join(f"<li id='ref-{i}'><span>{i}.</span><span>{report._vancouver(record_of(ctx.store, rid))}</span></li>"
                       for i, rid in enumerate(refs.order, 1))
    html = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{figlib.x(plan.get('title', 'Report'))}</title>"
            f"<style>{report._CSS}{CSS}</style></head><body><div class='kicker'>{figlib.x(plan.get('kicker', 'Systematic review'))}</div>"
            f"<h1>{figlib.x(plan.get('title', ''))}</h1>"
            + (f"<p class='note'>{T(plan['note'])}</p>" if plan.get("note") else "")
            + (f"<div class='summary'><b>Key messages</b><ul>{summary}</ul></div>" if summary else "")
            + f"<nav class='toc'>{''.join(toc)}</nav>" + "".join(body)
            + f"<h2 id='references'>References</h2><ol class='refs'>{ref_html}</ol></body></html>")
    out = Path(ctx.run_dir) / OUT_FILE
    out.write_text(html)
    return {"ok": True, "path": str(out), "figures": nf, "tables": nt, "references": len(refs.order), "problems": [],
            "message": f"{out.name}: {nf} figures, {nt} tables, {len(refs.order)} references"}


def inventory(ctx, plan: dict | None = None) -> dict:
    """What the data can show, for whoever plans the report (the agent, a person): studies per model, per concept and
    direction, the pooled groups, and the figure and table types on offer. No model call."""
    plan = plan or {}
    D = Data(ctx, plan)
    by = D.by_concept()
    return {"counts": D.counts(), "tiers": D.tiers,
            "concepts": {c: {t: dict(Counter(v.values())) for t, v in tiers.items()} for c, tiers in by.items()},
            "unmatched_results": sum(1 for r in D.results if not r["concepts"]),
            "pooled_groups": [{"group": a, "measure": b, "text": pool_text(g)} for (a, b), g in D.pooled.items()],
            "figure_types": list(FIGURES), "table_types": list(TABLES)}
