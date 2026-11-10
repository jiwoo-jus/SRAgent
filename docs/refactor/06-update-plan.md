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
| Plan | GPT and Gemini parity (18-20) | Chunk I |
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

- The out-of-date search: **leave it; don't run that update.** Planning the search again costs a model
  call and could change the 6,860 records for no reason. N3 clears the warning.
- The forwarded count: 825 is correct (80 included, 186 excluded on full text, 557 waiting for a PDF,
  2 unreadable). N4 fixes the row.
- Until N3 lands: avoid confirming risk-of-bias drafts **after** a synthesis exists, because each
  confirmation would mark the statements that cite that study for a paid revision.

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

## Chunk F · Phase 6a-b

| # | Item | Recommendation |
|---|---|---|
| 21 | Stage audits after screening and extraction | Do |
| 22 | Plan narrator in the preview | Do |

## Chunk G · Checks

| # | Check | Cost |
|---|---|---|
| 27 | First real run of structured outputs (your next Run) | Part of that run |
| 28 | First real run of Claude batches | Cents, if you turn it on |
| 29 | Blind vs anchored verifier on all benchmark records | About $1 with Haiku |
| 30 | Add `contact_email` to the benchmark, to recover the 8 missing full texts | Free |
| 31 | Final end-to-end run after the changes; include a criteria edit partway through, a stop and a Resume, and an added PDF | About $1 with Haiku on the benchmark sample |

## Chunk H · Rest of phase 6

| # | Item | Recommendation |
|---|---|---|
| 23 | Named, versioned prompts | Do, but it stops all cached answers being reused |
| 24 | Evidence locator for extraction | Only if a benchmark shows it helps |
| 25 | Feedback agent that can read sources | Only if a benchmark shows it helps |
| 26 | Planning sessions for protocol and search | Lowest priority |

## Chunk I · GPT and Gemini parity (build is free; checks need those API keys)

| # | Item |
|---|---|
| 18 | Batch processing for OpenAI and Gemini, checking their current terms first |
| 19 | Structured outputs for OpenAI and Gemini; Gemini may need its schemas adjusted |
| 20 | A log line when a batch stage uses a model that can't batch (today it switches to live calls silently) |

Last, because the budget is Claude-only: it can be built and tested offline, but stays unproven on the
real services until someone spends a few cents on each.

## Order

**A → B → C → D → E → F → G → H → I**

| Dependency | Why |
|---|---|
| N1 before 3 | Resume after a budget stop needs jobs that save as they go |
| N3 before synthesis in a live run | Confirming risk of bias after a synthesis exists would trigger paid revisions |
| 5 before N7 | Newly included studies show their extraction and risk-of-bias cost |
| 29 before 17 | The default verifier is chosen from the comparison |
