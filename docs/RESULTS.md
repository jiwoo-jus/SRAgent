# Evaluation results log

One row per **complete** evaluation run (`sragent eval --suite all`) at a fixed commit, appended with

    sragent eval-summary --config <run file> --run <run id> --note "..." --append docs/RESULTS.md

Rows are comparable only when the same columns mean the same thing, so every row is produced by that command from the
run's `results.json` and manifest, never typed by hand. Development runs (partial suites, code changing between runs)
are not recorded here: their reports stay in `runs/<run>/eval/runs/<id>/EVAL_REPORT.md`. Rows that become paper
results are copied into GUIDE section 11 with their discussion.

Columns: `e2e recall/precision` (end-to-end, the agent's own search and citation chasing), `included` (studies the
agent included), `extraction supported` (values the judge found supported by their cited sentences), `evidence located`
(values whose cited sentences resolve), `synthesis supported` and `overgeneralisation` (judge, stage-wise synthesis),
`claim1 report->evidence` (planted-error detection, report-only -> evidence-linked), `claim2 downstream local->dep`
(downstream completion, local edit -> dependency-aware, new errors in brackets), `judge kappa (controls)` (inter-judge
kappa and known-answer accuracy), `cost USD` (the evaluation run alone).

| date | run | code | run file | models | suites | e2e recall/precision | included | extraction supported | evidence located | synthesis supported | overgeneralisation | claim1 report->evidence | claim2 downstream local->dep (new errors) | judge kappa (controls) | cost USD | status | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-20 | 20261020-162231_all_v3b | 495d549 | configs/benchmark/emicizumab_pk/recommended_v3_agentonly.yaml | agent haiku-5-5 medium; plan/search/extract/synthesize/feedback sonnet-5-5 high; verifier gpt-6.1-sol medium; judge gem-3.1-pro medium; judge2 gpt-6.1-sol medium; reviewer gem-3.8-flash low | claim1, claim2, end_to_end, judge_validation, stagewise_extraction_rob, stagewise_screening, stagewise_synthesis | 0.93 / 0.93 | 16 | 0.70 | 0.99 | 1.00 | 0.00 | 0.40 -> 0.53 | 0.19 -> 0.82 (0) | 0.79 (1.00) | 8.63 | complete | git 43731ea. First complete run after the fix rounds (dev sizes: claim1 3 sets x 1 repeat, claim2 2 cases/type). Known: the planted eligibility error flipped I3 alone, which the any_of protocol does not exclude, so B cases (0.45 downstream) understate dependency-aware revision (fixed after this run); judge2 is a different family (GPT) than before (Gemma); Europe PMC partly down (43 of 93 full texts). Cost includes two attempts interrupted by the Gemini daily quota. |
