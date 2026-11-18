# 06 · Update plan after the refactor (2026-11-10)

Written against commit `a349ea3`. This puts together, in one plan:

- the agreed budget proposal and the open work after phase 5 (items 1-31);
- what we found while reviewing the `aspirin_placental_mechanisms` run (items N1-N8);
- two reports from another session (criterion groups, the "Process the added files" button) and two
  requests (progress, saving during long jobs).

Items keep their numbers; new items are `N1`-`N8`. The chunks below are in the order to build them.

## Coverage: where each issue and request went

| Source | Issue or request | Where |
|---|---|---|
| Budget proposal | `budget:` section with `total_usd` and per-stage limits, `unlimited` | Chunk B (1-5) |
| Plan | All settings visible (6-8) | Chunk D |
| Plan | Small page fixes (9-12) | Chunk C |
| Plan | Remaining phase 5 (13-17) | Chunk E |
| Plan | OpenAI and Google parity (18-20) | Chunk I |
| Plan | Phase 6 (21-26) | Chunks F and H |
| Plan | Checks (27-31) | Chunk G |
| This review | "1 output is out of date (1 search)" after a criterion stage edit: a false alarm | N3 |
| This review | The same false alarm for the search strategy, and for synthesis statements when a risk-of-bias draft is confirmed (this one would cost money) | N3 |
| This review | Full texts row says "2293 forwarded studies"; 825 are forwarded | N4 |
| Report 1 | 14989141 forwarded although I2 was not met | No decision bug: group A (I2-I5, any one is enough) is met through I4. Display: N5. Related bug: N6 |
| Report 1 | "Protocol stage, Items table shows no group" | Already shown there ("[any one of group A suffices]", `ui/static/js/app.js:327`); the missing group name is N5 |
| Report 2 | "Process the added files": unclear label, reaches far beyond its stage | N7 (decided: remove the button) |
| Request | Progress and counts while a job runs | N2 |
| Request | A 2-3 hour re-screen that saves nothing until the end | N1 |

## Open decisions

1. The word `unlimited` for a stage with no limit (proposed; not yet confirmed).
2. When to start chunk B. It changes the run-file format, so start when no job is running. Chunk A can
   start now.
3. Item 17 (blind verifier as the default): decided after check 29.
4. Items 24 and 25: only if a benchmark shows they help.

## The current run (`aspirin_placental_mechanisms`)

Checked against the run's store on 2026-11-17.

- 6,860 records screened at title/abstract: 825 forwarded. Of those, 635 screened on the full text (172 included,
  463 excluded) and 190 still waiting for full-text screening, most of them brought back by the any-of group fix
  (07, "A screening bug"; estimated at about $2.15, not run yet).
- Every full-text decision has had a second reading (635 verifier calls).
- 172 included studies extracted and assessed for risk of bias; 56 synthesis statements.
- The earlier notes here (the false out-of-date search, the forwarded count, not confirming risk-of-bias drafts
  after a synthesis exists) are settled: N3 is done, and N4 is a display fix only.

---

## Chunk A · Engine fixes (new, free, start now)

No run-file change. This goes first because chunk B's Resume needs jobs that can resume, and the
current run is about to enter extraction, risk of bias and synthesis.

| # | Item | Details |
|---|---|---|
| N1 | Long update jobs run in parallel and save as they go | The update engine handles each study one at a time and saves only at the end (`engine.py:274-290`: re-screening, extraction and risk of bias). Your `concurrency: 12` isn't used, and a stop loses everything. Use the same chunked map as stage runs (`context.py:90`: parallel, saved after each chunk), so a stopped job continues where it stopped. Expected effect: the 2-3 hour re-screen becomes much shorter (roughly 10×) and can be left alone. First check that per-study screening is safe to run in parallel (`_because`, store writes). |
| N2 | Progress for every job | "Screening 340 / 825 · about 12 min left" in the job box, on the stage page and on Overview. Stage runs and N1 already work in chunks, so this is one counter per chunk, not extra load. |
| N3 | Out of date only when the part that is read changes | Today an output records its dependency's version number, so any change counts. Instead, record a fingerprint of the part it reads: protocol → question, PICO, key concepts (search, search strategy); risk of bias → domain judgments and overall (synthesis statements). This removes the three false alarms. It also replaces the engine's own comparison of protocol versions (`engine.py:32-52`) with one general rule. The current run's search warning clears; no conversion code for older stores is needed. |
| N8 | Offline tests for N1-N3 | A criterion stage edit leaves the search current; confirming a risk-of-bias draft leaves the synthesis current; a re-screen stopped halfway resumes without redoing finished studies and gives the same result as one that wasn't stopped. |

**Status (2026-11-10): done.**

- N1: the update engine's re-screening, extraction and risk-of-bias steps run through `Ctx.map` (`engine._each`).
  - Saves are time-based (`Ctx.SAVE_SECONDS = 90`), because a save of a 250 MB review takes ~10 s; a stop loses at most ~1.5 min of work.
  - A model reply that can't be used now skips that study (run again to retry) instead of stopping the update.
  - Made safe for parallel workers: per-worker causes (`because(local=True)`), one change-log seq per entry, one cohort refresh at a time.
  - Found while building it: a sandbox copy that shares the review's folder (proposals, re-runs) could save over the live `store.json` through `Ctx.map`. `Ctx.save` now refuses that (`ReviewStore.copied_from`); copies with a folder of their own (evaluations, previews) still save there.
- N2: `progress.json` (step, done, total), shown in the sidebar job box with a bar and time left, and as "done/total" on the top Running button.
  - Every page shows the sidebar, so there's no separate display on stage pages or Overview.
- N3: `graph.READS` names the part each output reads, and `stale_reasons` compares that part between the version the output saw and now (from the input's history).
  - The pairs: search and strategy ← protocol (question, PICO, key concepts); risk of bias ← extraction (values); synthesis ← extraction (values and quotes); synthesis ← risk of bias (domain judgments, overall).
  - `attention.edit_state` uses the same rule.
  - Nothing is stored differently, so there's no conversion. On the aspirin review the search warning is gone and nothing else changed status.
- N8: four tests in `tests/test_offline.py`.
  - Eight operation snapshots changed, all for the intended reason: "search kept" is gone after protocol edits, and synthesis statements aren't marked for revision after `rob.confirm` / `rob.verify` when no rating changed.

## Chunk B · Budgets (free to build; after the current job)

```yaml
budget:
  total_usd: 30          # always a number, so there is one hard ceiling
  stages:
    protocol: unlimited
    search: unlimited
    screen: 5
    eligibility: 3
    snowball: unlimited
    extract: 4
    rob: unlimited
    synthesis: unlimited
    feedback: unlimited
```

Every stage that makes model calls must be listed; a missing one is an error. `retrieve` and `report`
make no model calls and aren't listed. `eval.budget_usd` stays separate (evaluations only). The per-job
cap goes; no limits per role.

| # | Item | Note |
|---|---|---|
| 1 | Add `budget:`; remove `budget_usd` and the per-job cap; rewrite the run files | |
| 2 | One rule everywhere: a call runs only if the total and its stage both have room; batches too | |
| 3 | Reaching a limit is a clean stop, with a box to raise the limit and a **Resume** button | Needs N1 |
| 4 | Budget card in Settings; "spent X of Y · Edit" on each stage page; "Spent X of Y" on Overview | |
| 5 | Estimated cost before Run, with a warning if it won't fit | Also used by N7 for newly included studies. Replaces today's fixed "runs as a job with model calls" question (`ui/static/js/ops.js:80`), which comes from the operation's kind, not from the work: Full texts → Run is marked `llm` (`ops/selection.py:90`) only because "run up to here" may include earlier stages, so it asks even when nothing will call a model. The question should say what will run and its cost, or "no model calls" |

**Status (2026-11-10): done.**

- 1: `budget:` is required in every run file; a missing or unknown stage, or a value that isn't a number or `unlimited`, is an error.
  - `config.budget_limits` checks it whenever it's written. `LLMHub`, the cost tracker behind every model call, requires it, and so do the Configs page's Check and Save for run files.
  - An old `budget_usd` line gets a message naming its replacement.
  - The 13 run files under `configs/` were rewritten: each keeps its total, and every stage is `unlimited`. The new-review form writes the same section.
  - Removed: the page's per-job cap. Every job started from the page ran with a limit of "spent so far + $2" (`extra_budget`, default 2), whatever the run file said. Also removed: `ui_settings.extra_budget`, `/api/settings`, and the "Spending cap" card.
  - Evaluations get `eval.budget_usd` as a total with no stage limits.
- 2: `CostTracker` keeps spending per stage (from usage.jsonl), and `check(stage)` runs before every paid call, retries included.
  - Batches send only what `room(stage)` allows (the smaller of the total's room and the stage's).
  - Cached answers are free and never checked.
  - PDF reading by a model (`vision_pages`) now lets a budget stop through instead of marking the paper unreadable.
- 3: a limit raises `BudgetExceeded`, which knows its stage and limit and writes `budget_stop.json`. The CLI exits with code 3 (`BUDGET_STOP`) and a plain message, and the page marks the job `budget`.
  - The sidebar job box then says which limit was reached and offers a new limit with **Raise and resume** (`/api/budget`, then `/api/job_resume`, which queues the same job again).
  - Finished work is kept (chunk A), so the job continues where it stopped.
- 4: Settings → Budget: the total and each stage's spent and limit (empty means unlimited), with bars; it writes `budget:` one stage per line.
  - Stage pages show "spent $X of $Y · Edit" (or "no stage limit"); Overview and the sidebar show "Spent $X of $Y".
- 5: before a model job, the question comes from `/api/run_estimate`.
  - A stage's Run (`<stage>.run`, `protocol.draft`, `synthesis.write`) is estimated from the work left up to that stage, by budget stage, with the room left and a warning when it may not fit. "No model calls are expected" when nothing is left (Full texts → Run on the aspirin review).
  - Other model jobs show the room left.
- Also fixed: Settings → "Test the agent model" built a tracker with a $0.05 limit that also counted the folder's past spending, so the test was refused in any review that had spent more than 5 cents. It now runs within the run's own total.
- Tests: four in `tests/test_offline.py` (the section's checks, a stage limit versus the total, a review stopped by a stage limit that continues once raised, and the page's view, save, estimate and resume). Checked by hand: a zero-budget command stops with exit code 3 and `budget_stop.json` before any call is sent.

## Chunk C · Page fixes and the full-text flow (free)

| # | Item | Details |
|---|---|---|
| 9 | Hide Approve in agent-only mode | |
| 10 | Plain words on the Inputs card | |
| 11 | Fix the misleading Report row label | |
| 12 | Replace old wording such as "To-do list" | |
| N4 | The Full texts row counts forwarded studies only | `ui/steps.py:53` counts every stored full text, including 1,468 of studies excluded at title/abstract after the criteria change. Count only studies whose current decision isn't a title/abstract exclusion: "825 forwarded · 266 with full text (68 from your PDFs) · 557 not retrieved · 2 unreadable". The stored texts stay (needed if a decision is reversed). |
| N5 | Criterion groups shown on the study page, with a name | Show the members together: "Group A (any one is enough): met via I4", members I2-I5 under it. Add an optional name per group to the protocol (for example A = "human model studied: cells, tissue, pregnant women or endothelial models"), shown on the study page and on the protocol page, and drafted by the planner. Naming a group changes no criterion, so it re-screens nothing (and with N3 it doesn't touch the search). |
| N6 | The Needs-you suggestion follows the group rule | `todos.py:116-136` ignores `any_of`, so a study kept because group A is met can be pre-filled with "exclude: I2 fails". Use the same rule as the decision (`screening.rule_decision`). Open in 03 as well. |
| N7 | Remove "Process the added files" | See below. |
| N10 | The Overview's Next step names full-text screening when forwarded studies wait for it | Today it says "extraction, risk of bias and synthesis" while 261 studies wait for full-text screening |
| N11 | Read scanned PDFs with local text recognition | All 19 "unreadable file" studies in the current run are scans with no text layer (pdftotext gives 1-588 characters). `vision_pages` only adds to a PDF that already has text, and Azure isn't configured. Add a free fallback in `docparse.parse_file`: when pdftotext gives under 1,500 characters, render the pages (`pdftoppm -r 300`) and read them with `tesseract` (installed here). Tested on 1407882.pdf: 7 pages, 34,600 characters of clean text in 46 s. Parser shown as "pdftotext + OCR". Parse in parallel; 19 files take about 10 minutes. |
| N12 | Stage pages: bulk selection and clearer labels (done 2026-11-10) | Checkboxes with select-all on rows whose operation takes a list of studies (Not obtainable, Restore, Confirm). Removed the "Decided by you" tab; a person's decision shows a "by you" badge. Full texts page: "no full text" / "unreadable file", and the By column says where the text came from ("your file", "agent: …", "abstract only"). Full-text screening page: those studies show "not screened: no full text". "Decide without" now shows only for a study a reviewer asked a PDF for; on a study with no full text it recorded nothing. Full-text screening's Verify button follows `pipeline.verify_screening`: with `full_text` it reads every full-text decision not yet read twice ("Verify unread (436)"), with a cost estimate from this review's own verifier calls. |

**Status (checked in the code 2026-11-17):** N12 done. Open: N4 (`ui/steps.py:20` still counts every stored
full text), N5 (no group names yet), N6 (`todos.py:136` still ignores `any_of`), N7 (`retrieve.process_added_files`
still exists), N10 (Next step still says "Continue the review: extraction, risk of bias and synthesis" while 190
studies wait for full-text screening), N11 (no text recognition step), 12 ("To-do list" still in 8 places). Not
checked: 9-11.

**N7 in detail.** Today the button reads the new PDFs, screens those studies, and then runs the whole
update (`sync.py:66`). That includes extraction and risk of bias for every included study, not only the
new ones. Texts the agent fetched itself aren't screened by it. Replacement:

1. **Notice only.** New files are found free of charge (by content, `fulltext.py:292`). Overview and the
   Full texts page show "3 new PDFs for forwarded studies · read and screened at Full-text screening".
2. **Full-text screening reads them.** Its Run already reads a supplied file for a study with no full
   text (`screening.py:521`). Add: a **replaced** file for a study that already has a full text is read
   again, which makes that study's full-text decision out of date in the usual way.
3. **Later stages follow from what's missing or out of date.** A newly included study shows as
   "extraction and risk of bias to do" in those stages, with the cost first (item 5).
4. **Unrelated files are ignored and listed.** A file counts only if its name matches a currently
   forwarded study. Other files ("no forwarded study with that name", or a study excluded at
   title/abstract) are listed as ignored, so a wrong name is easy to spot.
5. **Wrong paper under a right name.** A free check compares the record's title with the first page of
   the PDF. On a mismatch the file isn't used: "123.pdf doesn't look like this study".
6. Remove the operation (`retrieve.process_added_files`), its Needs-you answer (`todos.py:448`, `500`)
   and `sync.process_pdfs`. A person's existing decision is still kept when its file arrives.

## Chunk D · All settings visible (free)

| # | Item |
|---|---|
| 6 | One list of settings (name, default, allowed values, meaning, fixed or not) that generates everything else |
| 7 | A Settings form built from it, including concurrency and batch stages, with an Advanced section and locks on fixed settings; Edit as file stays |
| 8 | The new-review form and the README use the same list |

## Chunk E · Remaining phase 5 (free to build; these prompts won't reuse cached answers)

| # | Item |
|---|---|
| 13 | The eligibility check after extraction reads as much of the paper as the extractor did |
| 14 | A re-run note goes only to the call it targets |
| 15 | The synthesis writer and checker see whether each study was read from an abstract or a full text |
| 16 | The search refinement prompt gets the validated synonyms |
| 17 | Decide whether the blind verifier becomes the default (after check 29) |
| N9 | **Done 2026-11-10 as a level of `verify_screening`:** `conflict` reads the undecided full-text decisions and those the screener's overall answer disagrees with (`screening.self_conflict`); the levels are off < pending < conflict < full_text < all, the Verify button follows the level, and the estimate has a verifier entry. Original note: a screening decision that the screener's own overall answer disagrees with is a flag with a reason, so the verifier reads it, even when `verify_screening` is `off`. Found on the aspirin review: the stored decision comes from the per-criterion answers (`rule_decision`), while the model's overall answer is kept only as `llm_decision`. 91 of 527 full-text decisions disagree (37 exclude → include, 35 uncertain → include, 19 uncertain → exclude), and nothing shows it. Some disagreements are the screener misreading an any-of group, where the rule is right, so these need a second reading, not a reversal. Also: `estimate` has no entry for verifier calls (`screen_verify_full_text`). |
## Chunk F · Phase 6a-b

| # | Item | Recommendation |
|---|---|---|
| 21 | Stage audits after screening and extraction | Do |
| 22 | Plan narrator in the preview | Do |

## Chunk G · Checks

| # | Check | Cost |
|---|---|---|
| 27 | First real run of structured outputs (your next Run) | Part of that run |
| 28 | First real run of message batches | Cents, if you turn it on |
| 29 | Blind vs anchored verifier on all benchmark records | About $1 with a low-cost model |
| 30 | Add `contact_email` to the benchmark, to recover the 8 missing full texts | Free |
| 31 | Final end-to-end run after the changes; include a criteria edit partway through, a stop and a Resume, and an added PDF | About $1 with a low-cost model on the benchmark sample |

## Chunk H · Rest of phase 6

| # | Item | Recommendation |
|---|---|---|
| 23 | Named, versioned prompts | Do, but it stops all cached answers being reused |
| 24 | Evidence locator for extraction | Only if a benchmark shows it helps |
| 25 | Feedback agent that can read sources | Only if a benchmark shows it helps |
| 26 | Planning sessions for protocol and search | Lowest priority |

## Chunk I · OpenAI and Google parity (build is free; checks need those API keys)

| # | Item |
|---|---|
| 18 | Batch processing for OpenAI and Google, checking their current terms first |
| 19 | Structured outputs for OpenAI and Google; Google's may need its schemas adjusted |
| 20 | A log line when a batch stage uses a model that can't batch (today it switches to live calls silently) |

Last, because the API budget covers one provider only: it can be built and tested offline, but stays unproven on the
real services until someone spends a few cents on each.

## Chunk J · Quantitative synthesis (meta-analysis)

Asked on 2026-11-10 for the aspirin review: extraction good enough to pool, not only a narrative synthesis. Until
now each extraction item was one sentence of text per study, with numbers inside the prose, and nothing computed an
effect size. Agreed: the order below, the CSV for R (metafor) first, built-in pooling once the rows prove reliable,
and sFlt-1/PlGF (cells and clinical trials) as the first outcome groups to pool.

| Step | What | Status |
|---|---|---|
| J1 | **Rows items.** `kind: rows` with `columns` (a list of values, `number` or `text`) in `topic.extraction_fields` (`sragent/rows.py`). One row per result; each row cites its own sentences; every number is checked against them (found nowhere: `uncovered`); a value off a choice list is kept and marked. The model copies numbers as written and computes nothing; a figure-only result is a row with source `figure` and no numbers. The item's `value` is a summary written by the code, so every reader of `value` works unchanged; the synthesis input, the out-of-date check (`graph.READS`), hand edits (JSON lines on the study page), feedback revisions and the data-items card (columns kept) read the rows. Every column is a string in the reply schema: the API's structured outputs allow at most 16 union-typed (nullable) parameters per request. A reply shape the API cannot compile is asked again in plain JSON mode. `meta_rows.csv` (Data extraction page, `sragent op extract.export_rows`, and with every report). Reviews without a rows item send byte-identical prompts. Evidence is support, not a gate: a row's citations are optional (at most 2), the prompt says never to drop or shorten a row for lack of a sentence, and the code finds the sentences that hold the row's numbers itself (`_best_sentences`). | done |
| J2 | **Columns for the aspirin review** (`topic.yaml`, `outcome_data`, 31 columns), strengthened for pooling: `experiment` (rows sharing a control group), `comparison_design` (independent or paired), `unit_of_n`, `value_type` (mean, median, proportion, number of events: binary clinical outcomes use the count with n as group size), `measure_basis` (raw, normalised to control, change from baseline), dispersion with an upper bound for intervals, `effect_reported`, `p_value`, `direction`, `aspirin_form`, `source`. `models.extract.writer` runs at medium effort. | done: used for the extraction of every included study (J4) |
| J3 | **Pilot on 8-10 included studies** (the extraction writer at medium effort, about $1): how many results exist only in figures, whether the columns fit, whether `n` is independent experiments or replicates. Then adjust the columns. | done (superseded by J4) |
| J4 | **Extract every included study** (about $0.10 per study with the extraction writer at medium effort and ~10 rows: $15-20 for ~180; the total budget has to be raised). | done (checked 2026-11-17): 172 included studies extracted, 147 with rows, 879 rows |
| J5 | **Pooling by code** (`sragent/meta.py`, `synthesis.meta_analysis`; no model calls, no numpy or scipy). General code; what is specific to a review is its `topic.meta_analysis` plan (the rows item, which rows count, grouping, minimum studies, which columns hold n, value, spread). Log ratio of means (SD from SEM or 95% CI; a control normalised to 1 gets the treated CV, marked), log risk ratio from events or percentages, one effect per study and group (rows averaged, variance too), DerSimonian-Laird with the Hartung-Knapp interval, I², prediction interval, sensitivity without figure estimates and without high risk of bias, rows left out counted by reason. Writes `<run>/meta/`: `results.json`, `effects.csv`, `summary.md`, one forest plot (SVG) per pooled group. First run on the aspirin review (before figure reading): 261 of 879 rows gave an effect, 15 of 40 groups pooled. Added the same day: `refine` (finer groups from a text column by rules: clinical outcomes into preeclampsia, growth restriction, ... without re-extracting), `same_study` (papers of one trial count once in a group; identical effects are listed as possible duplicates), `binary_only_if` (a percentage counts events only for participants). **Synthesis:** a review with a plan is written per pooled group (`SYNTH_POOLED`: the pooled result and that group's rows) plus one call for what was not pooled (`SYNTH_UNPOOLED`: studies by direction); a statement keeps its pooled result (`pooled`), which its check and repair see; the whole extraction table is no longer sent in one prompt (it was 907,000 characters here). The single-call prompt no longer names PK findings. **Report:** the pooled table in `report.md`, the forest plots in `report_evidence.html`. **2026-11-11, without model calls:** medians with quartiles or range are converted to mean and SD (Wan 2014; 25 more rows used); `exclude` rules (46 rows of animal experiments reported inside included papers had been placed in the human tiers); `risk_of_bias_high` can name domains (the overall rating was "high" for 142 of 172 studies because dose and model relevance count like bias; on the four internal-validity domains 18 of the 64 studies in the analysis are high, so the sensitivity analysis means something); a statement's check and repair read exactly what it was written from (`basis`) - the first run had shown the check less than the writer, every statement was faulted and repaired by the synthesis writer ($14 lost); `pooled_sentence` writes a group's pooled finding by rule; the estimate measures input sizes on the review's own papers and counts a grouped synthesis with its checks and repairs; Data extraction has Read figures and selectable Extract, Synthesis has the pooled table with forest plots. **Figure estimates checked by eye against the page images: 24 rows in 8 studies, all 24 values matched the plot**; one comparator was wrong (Richards 2025, Fig 4e: the TNFα row was compared with the untreated control; corrected). The 56 statements of the aspirin review were brought to the recomputed results by rule (pooled findings) and by hand (the rest). **Still to do:** a revision of a pooled statement by the update engine does not recompute its pooled text or basis (use `pooled_sentence`); change scores with negative values make meaningless ratios (Mulcahy 2019); rows that merge several analytes or doses. The report of this review was 59 MB in one page (Appendix C, every eligibility decision of 6,860 records: 43 MB; Appendix A with each study's source text: 14.5 MB) and browsers could not render it: `report.split_report` now writes a report over 4 million characters as a main page (1.1 MB here, with the pooled table and forest plots after the synthesis) and appendix pages of about 2 MB in `report_parts/`, with links between pages kept; the interface serves the same pages (`/report`, `/report_parts/<page>`), built once per version of the store. | built 2026-11-10, checked 2026-11-11 |
| J6 | **Figure-only results** (`sragent/figures.py`, `extract.read_figures`): the PDF page that carries a row's figure is shown to the extraction model with that figure's rows; what it reads goes only into empty columns, each listed in `row["estimated"]` (study page: ≈ and "estimated from Fig N"; CSV: `estimated_from_figure`). Pilot 2026-11-10 on 3 PDFs: 27 of 28 figure rows read for $0.15; direction agreed with the text in all; one page (Fig 3 of 31004367, 7 rows) checked by eye and matched. Limits: studies whose text came from PMC without a PDF are skipped (56 of 179 have no PDF of their own); small panels of dense figures are less precise; a row that merges several analytes is not read. Not yet: run automatically after extraction (a pipeline setting), fetch missing PMC PDFs. | built; run on the aspirin review: 132 figure calls, 260 rows hold estimates |

## Order

**A → B → C → D → E → F → G → H → I**; J ran alongside: J1-J6 done; what is left is under J5, "Still to do".

| Dependency | Why |
|---|---|
| N1 before 3 | Resume after a budget stop needs jobs that save as they go |
| N3 before synthesis in a live run | Confirming risk of bias after a synthesis exists would trigger paid revisions |
| 5 before N7 | Newly included studies show their extraction and risk-of-bias cost |
| 29 before 17 | The default verifier is chosen from the comparison |
