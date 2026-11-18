# SRAgent refactor dossier

Five documents, written on 2026-11-05 against commit `59eea5d`, from a file-by-file reading of the
code, a cross-subsystem audit with each issue re-checked against the source, and the owner's
requirements: clear stages with their own pages, one place per process, a solid dependency graph shown
before any change, aligned controls across stages, clear model roles, and better use of the agentic system.

| Document | What it answers |
|---|---|
| [01 · The pipeline as it is](01-pipeline-as-is.md) | The decision tree of every stage and of the cross-cutting flows (engine, feedback, PDFs, re-runs), with the UI overlaid: which branch a person can reach from where. The declared-versus-written dependency graph and the state that lives outside it. |
| [02 · The UI as it is](02-ui-as-is.md) | Routes and views, the three "where are we" computations behind one screen, the stage × control matrix (what is missing for which stage), the same action from several places, the job and answer queue, mode gating, naming. |
| [03 · The inconsistent flows, with their reasons](03-inconsistencies.md) | 112 issues (67 verified against the code, 45 pending), each with evidence and the reason it exists; ten root causes and the part of the plan that removes each. |
| [04 · The refactor plan](04-refactor-plan.md) | The five rules, the canonical vocabulary, the target pipeline interface, the target graph, the operations layer, the stage-page template and the dependency preview, model roles, agentic upgrades, migration phases, decisions to take first. |
| [05 · Models, tools, visibility](05-models-tools-visibility.md) | How a model is chosen today, roles and stages, the stage × information matrix (what each model sees), the tool catalogue, and what is deliberately hidden from the models. |
| [06 · Update plan after the refactor](06-update-plan.md) | Added 2026-11-10. The work after phase 5 in build order (engine fixes, budgets, page fixes and the full-text flow, settings, rest of phase 5, phase 6, checks, GPT/Gemini), with the issues found on the aspirin review placed in it. |
| [07-report-planning.md](07-report-planning.md) | A report planned with the team: report plan, figure library, renderer; the planning stage to build |

Reading order for a decision: 03 §1 (the ten reasons) → 04 §0-§1 (rules and vocabulary) → 04 §9
(decisions) → the rest of 04. Reading order for implementation: 01 → 02 → 04 → 03 (as the checklist).

Diagrams are Mermaid; GitHub renders them, VS Code needs a Mermaid preview extension. A single HTML
page with every diagram rendered is published alongside (link in the handover message).

Conventions: `file.py:NN` references point at commit `59eea5d`; `🖥` marks something reachable in the
web UI, `⌨` command line only, `⛔` nowhere; stage keys are quoted as they appear in the code today
(`plan`, `fulltext`, `synthesize`) in 01-03 and as proposed (`protocol`, `retrieve`, `eligibility`,
`synthesis`) in 04.
