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

