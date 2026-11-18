# 07 · A report planned with the review team

The standard report lists everything so that it can be verified. Readers need something else: a message, sections in
an order that carries it, and the figures and tables that show it. Which figures those are depends on the review (a
mechanism review needs a pathway map and a table of molecular targets; an intervention review needs forest plots and
a summary of findings), so the report has to be **planned per review, with the team, by the agent**, not hard-coded.

## What exists (2026-11-16, no model calls)

| Piece | File | What it does |
|---|---|---|
| Report plan | `configs/<kind>/<topic>/report_plan.yaml` | Title, key messages, the concept map (how extracted results are grouped for figures), the settings, the dose scales, and sections made of paragraphs, lists, figures and tables |
| Renderer | `sragent/reportplan.py` | `check` (citations are included studies, pooled groups exist, figure references resolve), `render` (one HTML page, each figure also as SVG), `inventory` (what the data can show) |
| Figure library | `sragent/figlib.py` | Evidence map, effect direction plot, mechanism map, dose plot, risk-of-bias summary; forest plots come from `sragent/meta.py`; the PRISMA diagram from the standard report |
| Operations | `report.manuscript`, `report.inventory` | Render the plan; list what a plan can draw on |
| Interface | Output · For readers | Serves the planned report, rebuilt from the current data and the current plan on each open; the link is there only for a review that has a `report_plan.yaml` |
| Interface | Restart notice | A running interface keeps the modules it imported, so editing a source file changes the pages but not what computes them. `ui/server.py:code_changed()` lists the package's `.py` files edited after the process started; the home view and the report for readers both say so and ask for a restart |

**What kind of change.** A result is classified by the `classify` rules of the review's pooling plan
(`topic.meta_analysis`, patterns over the wording of the analyte, the sample and the study's own measure type) into
protein level, mRNA, miRNA, phosphorylation (activation), enzyme activity, promoter or DNA binding, release or
circulating level, metabolite, or function. That is what lets a pathway node say *eNOS, mRNA level* against
*NF-κB, phosphorylation (activation)*: aspirin changes how much eNOS there is, but only how much NF-κB is active. A
node a cascade names that no included study measured says "not measured", which is where a chain stops being evidence
and becomes inference.

**Nothing unlike is added together.** The class above is a `group_by` dimension of the analysis, not only a label, so
a concentration in blood, the same protein inside a cell and its transcript are three pooled estimates and three
table lines. The plan's `settings` do the same for the in-vivo and in-vitro divide: a setting is a group of model
tiers, every count and every figure annotation is made within one setting, and a figure or table covers one with
`setting: <key>`. A `{{pool:...}}` name that matches two groups is an error naming both, so the author has to say
which. This was a real defect: before 2026-11-16 the serum concentration of PlGF and its expression in placental
tissue were one pooled estimate, and "sFlt-1 falls in 12 of 17 studies" counted cells and women together.

**Where the aspirin was given decides the setting.** A setting is the exposure, not only the model: a result whose
dose is a daily dose was produced in a woman, one whose dose is a concentration was produced in a dish. Placental
tissue taken from a woman who took 75 mg a day is in-vivo evidence although the model is tissue, and maternal serum
with aspirin added to it in a tube is in-vitro evidence although the material came from a woman. A setting may
declare `given: in vivo | in vitro` beside its `tiers`, every result carries its own setting, and counts, figures
and tables are made within one. This was a real defect: before 2026-11-17 five studies of tissue from treated women
were counted among the in-vitro studies and one in-vitro serum study among the women.

**One level per row.** A concept map mixes levels unless it is made to not: molecules and the cellular functions
they end in were in one list, so a figure row could be *trophoblast invasion* beside *MMP-2/9* with nothing saying
how they relate, and the same domain could appear twice when its items were declared out of order. Concepts now
carry a domain that is a level (`function` for the phenotypes), rows are grouped in the order the domains are
declared, and a molecule declares `leads_to`, so it is labelled *AKT / PI3K / mTOR → invasion, migration*. That
arrow is the plan's reading of its own cascade figures, not a measured association, and the captions say so.

**Is this the dose a patient receives?** The plan holds two dose scales — the concentration the cells were given
(`dose`) and the daily dose the patients took (`dose.clinical`) — each with its own units, factors and reference
band. The `dose_check` table places every study on its scale and says whether its dose falls in the band, by what
factor it does not, and how many results it contributed; `{{dose:<setting>|<scale>|<verdict>}}` counts them in the
text. Doses written as ranges (`1 to 50 µM`, `10^-5 to 10^-3 mol/L`, `0-2 mM`) keep both ends.

The render is reproducible: nothing in the page is ordered by a set, so the same data and plan give the same bytes, and a diff between two versions of a report shows only what changed.

**What a report must say even when the answer is "not done".** The flow diagram states the arithmetic of each step
and counts what the query returned, including the records a database gives back in a form that is not an article
(PubMed book chapters), so identification reconciles with the hit count. A synthesis whose automatic check has not
run says so instead of reporting zero supported statements. A sensitivity analysis that does not apply says so
instead of leaving a cell empty, and a prediction interval is computed only from `meta.PREDICTION_MIN` (5) studies
on, because below that it spans orders of magnitude and says nothing. A term a reference vocabulary does not list is
reported apart from the open flags: RxNorm and MONDO do not hold ordinary obstetric wording, so those misses are
about the vocabulary, not the review. The methods section describes the quantitative synthesis that actually ran,
and names what was not done (reporting bias, GRADE, a second screener). `search.bibliography` completes journal,
volume, issue and pages from PubMed for the reference list; records are read for their content only
(`graph.READS`), so completing them makes nothing out of date.

Numbers the system can compute are tokens in the text (`{{pool:group|measure}}`, `{{n:included}}`, `{{n:search_date}}`,
`{{n:studies:<setting>}}`, `{{n:rows_left_out:<reason>}}`, `{{dir:item|direction|setting}}`,
`{{dose:setting|scale|verdict}}`), so a corrected value changes the report without
anyone retyping it. Citations are `[@id]` and must be included studies.

## Figure and table types, and what they are for

| Type | Shows | Standard it follows |
|---|---|---|
| `prisma` | Study selection | PRISMA 2020 flow diagram |
| `forest` | Per-study and pooled effects of one group | Cochrane Handbook ch. 10 |
| `direction_plot` | One mark per study by direction of effect, per item and model | Effect direction plot (SWiM guidance, for results that cannot be pooled) |
| `evidence_map` | Number of studies per item and model | Evidence gap map |
| `dose_plot` | Each result at its concentration, by direction, with a reference band | Dose-response overview (bubble plot without weights) |
| `rob_summary` | Judgments per domain | Risk-of-bias summary plot (robvis style) |
| `mechanism_map` | Stress, intervention, panels of targets with direction and number of studies, outcomes | The review's own schematic, drawn from the data |
| `pathway` | A signalling cascade: nodes in columns by depth, edges labelled with the step (`activates`, `inhibits`), each node carrying the direction, the number of studies and the kind of change measured | A mechanism figure; `flow: down` for a deep chain, `settings: [a, b]` for one line of evidence per setting |
| table `modulation` | Per molecule and setting, what kind of change was measured (expression, activation, activity, release, function) and in which direction | Answers "expression or activation?" |
| table `pooled` | Every pooled group with sensitivity analyses, one column per grouping dimension | Summary of pooled estimates |
| table `concepts` | Per concept and setting: studies, the doses used, one summarising direction, the per-study directions, models, references | Summary of targets |
| table `dose_check` | Per study: the dose as given, on a common scale, and whether it is the dose a patient receives | Answers "is this a clinically reachable exposure?" |
| table with `columns` and `rows` | Written by the planner (for example pathway by target) | Any |

Still to add when a review needs them: funnel plot (10 or more studies in a group), per-study traffic-light risk of
bias, summary-of-findings table with certainty (GRADE), subgroup forest plot, harvest plot, leave-one-out plot,
dose-response meta-regression, images supplied by the team (an edited schematic).

## The planning stage (to build)

A new stage between Synthesis and Report, with the agent in the lead and the team deciding:

1. **Inventory** (free): `reportplan.inventory` plus the synthesis statements and the pooled table.
2. **Proposal** (one model call, `report.plan`): the agent reads the question, the inventory and any material the team
   gives (an outline, a draft, a target journal) and proposes: the message, the section outline, for each section
   the figures and tables with the reason for each, and the concept map. It also says what the data cannot support
   (a planned section with one study behind it) and what the team's material mentions that the review did not include.
3. **Conversation** (Needs you card, like the protocol card): the team accepts, reorders, drops or adds sections and
   figures in plain words ("split inflammation from prostanoids", "add a forest plot for PlGF", "the dose plot
   should use salicylate equivalents"). Each change is one call returning the revised plan and what changed.
   `check` runs on every version; the plan is saved as a node, so its history is kept.
4. **Writing** (one call per section, `report.write_section`): the writer gets only that section's brief, its figures'
   data and the extracted fields of the studies its concepts match; it returns paragraphs with `[@id]` citations and
   tokens for computed numbers. Each paragraph is checked like a synthesis statement, against exactly what the
   writer was given (`basis`).
5. **Render** (free), and **update**: sections depend on the outputs they cite, so a changed extraction marks the
   section out of date; figures and tokens update on render without a call.

Cost before each step, as everywhere: the plan is one or two calls; writing is one call per section plus its checks,
estimated from the section's inputs.

## Two things the first planned report found

**A screening bug (fixed).** An `any_of` group of alternatives could be declared not met at title/abstract while one
of its alternatives was a full-text criterion that had not been read. In the aspirin review, criterion I5 (human
endothelial cells used as a model of the placental vasculature) is a full-text criterion in the same group as I2-I4,
so every endothelial-cell study was excluded at title/abstract and I5 never applied to anything.
`screening.rule_decision` now treats such a group as unclear at title/abstract, so the record goes forward and the
deferred alternative is read. Re-applying the stored assessments (no model calls) moves **181 records** from excluded
to forwarded; 84 of them are endothelial studies. Screening them on their full texts is estimated at about $2.15.

**Counts that drift.** The first draft of the plan had hand-typed counts ("16 of 17 studies"), and the figure built
from the same data said "16 of 21" because a study measured in two models was counted twice. Both are now one
computed token, `{{dir:<item>|<direction>}}`, and `study_directions` counts a study once (as mixed when its own
results disagree). Any number a report states about the data should be a token, not typed.

## A recall check that comes for free

When the team supplies a draft or reference list, the agent compares it with the review: which of its references are
included, which were excluded and why. For the aspirin review, 12 of the draft's 16 references are included. Of the other four, three are HUVEC studies
excluded by the bug above (they will be re-screened), and one is a review article, correctly excluded by rule.
