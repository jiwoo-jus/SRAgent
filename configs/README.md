# Configs

```
configs/
  models.yaml                 the model catalog: provider and price of each model (written once)
  benchmark/                  topics WITH a known answer: to measure the agent
    emicizumab_pk/
      topic.yaml                what to review (question, criteria, records, fields)
      gemini_agentonly.yaml     a run = topic + the models it chooses + settings
      ...
  review/                     topics WITHOUT a known answer: real systematic reviews
    _template/                  copy this folder to start one (topic.yaml + run.yaml)
```

**A run file's path is its name, and its outputs follow it.** `configs/<benchmark|review>/<topic>/<run>.yaml` writes to `runs/<benchmark|review>/<topic>/<run>/`; nothing has to be written in the file for that. Any run file can be opened in the web interface or run from the command line (`sragent run --config <file>`). `sragent ui` (with or without `--config <file>`) has a **Configs** page that lists every run file, topic and the model catalog, opens a run file with one click, edits any of these files with a check on every change (it loads the run file with its includes, as `sragent run` would), and creates a new review topic or run file from a short form.

**What exists and where it stands:** `sragent runs` lists every run file with its models, people mode, records and the state of its outputs (not started / review done / evaluated / paused).

## Benchmark or review?

| | `benchmark/` | `review/` |
|---|---|---|
| Known answer (gold labels) | yes | no |
| Purpose | measure and test the agent | a real systematic review |
| `sragent eval` | yes: scores the run against the known answer | refused (nothing to score against) |
| `pipeline.max_records` (sample) | allowed: the known answers are always kept | refused (it would drop studies at random) |
| Records | `records.source: synergy` | `pubmed` or `file` |
| Run file names | `<models>_<characteristic>.yaml` (several variants per topic) | `run.yaml` (one is usual; name a second one freely) |

Evaluation is not a separate kind of run: `sragent eval` reads a finished benchmark run and writes into its folder (`eval/runs/<id>/`). The `eval:` settings can be added to the run file at any time before that.

## Run files

| Run file | Models | Records | People |
|---|---|---|---|
| `benchmark/emicizumab_pk/gemini_agentonly.yaml` | Gemini (agent gemini-3.8-flash, judge gemini-3.1-pro-preview, second judge gemma-4-31b-it) | all | agent only (people only supply PDFs). The main results (GUIDE 11.0); `runs/.../gemini_agentonly/eval/README.md` lists its evaluations |
| `benchmark/emicizumab_pk/claude_stages_agentonly.yaml` | Claude per stage: Sonnet high for protocol, search and synthesis, Haiku medium for the rest; Gemini judges | all | agent only. The baseline before the dependency-graph refactor (evaluation `20261019-184008_all`) |
| `review/_template/run.yaml` | Gemini | all | human in the loop; template |

Other runs are made from these when needed, e.g. the second evaluation condition (GUIDE 10.0): copy `gemini_agentonly.yaml` as `gemini_humanloop.yaml`, set `human_mode: human_in_the_loop` and drop `fulltext_unclear`. For another model, change the cells under `models:` (and `eval.models`) to names from the catalog. For your own vLLM servers, write `{provider: local, model: auto}` as `models.default.writer` (below).

## The model catalog (`models.yaml`)

| Model | Provider | Price per 1M tokens (in / out) |
|---|---|---|
| `gemini-3.8-flash` | gemini | $0.75 / $3.75 |
| `gemini-3.1-pro-preview` | gemini | $2.00 / $12.00 |
| `gemma-4-31b-it` | gemini | $0.10 / $0.30 (placeholder) |
| `claude-haiku-5-5` | anthropic | $0.10 / $0.50 up to 100K-token prompts, $0.50 / $2.50 beyond |
| `claude-sonnet-5-5` | anthropic | $2.00 / $10.00 |

The catalog holds **facts about each model**: `provider`, `pricing` (`{input_per_m, output_per_m}`, optionally `long_context: {threshold_tokens, input_per_m, output_per_m}`), optionally `base_url` and a `note`. A run file chooses models **by name**, and their provider and price are filled in from here (a catalogued model's provider is a fact: it is taken from the catalog even if a run file writes another), so a price is written in one place only (it drives the budget guard and every cost estimate). Choices (reasoning effort, `max_tokens`, `concurrency`, `api_env`, `fallbacks`) go in the run file, not here. The web interface offers every model in the catalog. To use another model, add it here. Removing a model that a run file still uses is refused by the editor. Local vLLM models need no entry.

Roles: the **writer** produces a stage's output; the **verifier** reads again what has a reason for it (a record the screener could not decide, a flagged risk-of-bias draft, an eligibility doubt raised at extraction, a synthesis statement, your feedback and the revisions it starts). A verifier with no model of its own is `models.default.writer`, not the stage's writer, so a stage written by a stronger model is still read again by another one. **judge**, **judge2** and **reviewer** are used only by `sragent eval` (`eval.models`); set **reviewer** explicitly and keep it the same across runs you compare (Claim 1 measures the materials, not the reviewer).

## Recommended models (October 2026 prices)

The rule: a cheap model for the high-volume work whose mistakes a later step catches; a stronger model for the few calls that everything else rests on; and checks by a model of another family than the writer. `review/adhd_appetite_adverse_events/` is set up this way. "Where" is a cell under `models:` (the writer, unless it says verifier).

| Where | Model, effort | Why |
|---|---|---|
| `protocol`, `search` | Sonnet, high | one call each; the criteria and the query decide what the review can find |
| `screen` (title/abstract), `snowball` | Haiku, medium | hundreds of calls, inclusive by design; a wrong forward is caught at full text |
| `eligibility` | Haiku, high | decides inclusion (full-text screening); high effort costs a few cents more and stays ~10x cheaper than Sonnet (Sonnet medium is the upgrade if the budget allows: about $1.5 per 130 full texts) |
| `extract` | Sonnet, medium | the values the synthesis, the risk of bias and every revision rest on; the benchmark showed the largest quality gap between the two models here |
| `rob` | Haiku, medium | drafts a person confirms |
| `synthesis` | Sonnet, high | the conclusions; a handful of calls |
| `feedback` | Sonnet, medium | revisions and propagation: few calls, high stakes |
| `default.verifier` | GPT-6.1 Sol, medium | every second reading: a third family, independent of the writer (Claude) and the judge (Gemini) |
| `eval.models.judge` | Gemini 3.1 Pro, low | evaluation; another family than the writer |
| `eval.models.judge2` | GPT-6.1 Sol, low | second judge of a third family, so judge agreement bounds family bias |
| `eval.models.reviewer` | Gemini 3.8 Flash, low | Claim 1's simulated reviewer; keep it fixed across the runs you compare |

Costs (benchmark sizes: about 300 records, 130 full texts, 26 included): a review runs for about $2, of which extraction with Sonnet is about $1. Haiku everywhere was $1.36; Sonnet everywhere would be about $12.

## How to write a config

### Rules

1. **Three kinds of file, one job each.** `topic.yaml` only has `topic:`. The catalog `models.yaml` only has `models:` (facts about models). A run file has `include:` (its folder's `topic.yaml`), the models it chooses (`models:`, and `eval.models` for evaluation), the request settings every model shares (`llm_defaults`) and run settings.
2. **Layering.** `include:` files are loaded first, in order; keys in the including file override them. Nested sections merge key by key, so a run file writes only what it changes. Anything not written takes the default from `DEFAULTS` in `sragent/config.py`, which lists every key.
3. **Models inherit.** The model of a stage and role is `llm_defaults`, then `models.default.<role>`, then `models.<stage>.<role>`; write in a cell only what differs (a model name alone, or `{model, reasoning_effort, ...}`). A verifier with no cell of its own is `models.default.writer`. The evaluation roles are `eval.models.<role>` over `models.default.writer`. The provider and price of a cell's model come from the catalog, unless the cell writes its own.
4. **Paths.** `include`, `api_env`, `local.endpoints_file` and `models_file` are relative to the file they are written in. `cache_dir`, `pipeline.fulltext_dir` and the topic's `records.data_dir`, `records.file` and `protocol_file` are relative to the project root (run `sragent` from there). `run_dir` is derived from the run file's path; write it only to override that.
5. **Naming.** One folder per topic. Benchmark run files are `<models>_<characteristic>.yaml` (e.g. `gemini_agentonly`, `local_agentonly`); a review's run file is `run.yaml`.
6. **Settings that define a review are fixed once it has started.** The topic (question, eligibility, screening rules, records, extraction fields, risk-of-bias tool and domains) and `pipeline.human_mode`, `max_records`, `screen_sample_seed` and `fulltext_unclear` are recorded, and so are each stage's model cells (writer and verifier, model and effort) once that stage has started, in the run's `store.json` when it first saves. If any of them is edited afterwards, by the web interface, a text editor or a changed include, the run stops before any LLM call and shows what changed. Then: set them back; make a new run file for the new design (its own `runs/` folder); or accept the change on purpose with `sragent accept-config --config <run file> --reason "..."` (or Accept in the web interface). An accepted change is written to `provenance.jsonl` and shown in the report (Appendix D) and in every evaluation of the run. Budget, contact email, the models of stages not yet started, the `feedback` cells, `feedback.*` and `eval.*` (with `eval.models`) can change at any time. A run folder made by an older version of SRAgent is not read; run the review again in a new run folder.
7. **No keys in configs.** API keys live in `api_env.yaml` (project root, git-ignored) or environment variables.

### Run file (`configs/<benchmark|review>/<topic>/<run>.yaml`)

```yaml
# <Benchmark|Review> run: <topic> · <models> · <records> · <people>.
include:
  - topic.yaml                         # what to review (same folder)
llm_defaults:                          # request settings every model uses
  api_env: ../../../api_env.yaml       # the keys file in the project root (relative to this file)
  max_tokens: 16000
  concurrency: 6
models:                                # models by name; provider and price come from configs/models.yaml
  default: {writer: {model: claude-haiku-5-5, reasoning_effort: low}, verifier: gpt-6.1-sol}
  protocol: {writer: {model: claude-sonnet-5-5, reasoning_effort: high}}
  extract: {writer: {model: claude-sonnet-5-5, reasoning_effort: medium}}
  screen: {writer: {reasoning_effort: medium}}   # the default writer's model, another effort
budget:                                # USD limits of this run folder (required)
  total_usd: 5                         # one hard ceiling for every call
  stages:                              # every stage that calls a model: a number or unlimited
    protocol: unlimited
    search: unlimited
    screen: 2                          # title/abstract screening may spend at most $2
    eligibility: unlimited
    snowball: unlimited
    extract: unlimited
    rob: unlimited
    synthesis: unlimited
    feedback: unlimited
pipeline:
  human_mode: human_in_the_loop        # or agent_only
  max_records: 60                      # benchmark only; omit for all records
eval:                                  # benchmark only, for `sragent eval`
  models: {judge: {model: claude-sonnet-5-5, reasoning_effort: medium}, judge2: gemma-4-31b-it}
  budget_usd: 8.0
```

**Your own vLLM servers** (free): `models: {default: {writer: {provider: local, model: auto}}}` and `llm_defaults: {concurrency: 32}`. The servers and the roles (`writer`, `verifier`, `judge`, ...) each may serve are listed in `local_endpoints.yaml` in the project root; `sragent local-endpoints` edits that list (GUIDE section 4). Pool settings go under `local:` (below) only when they differ from the defaults.

### Topic file (`configs/<benchmark|review>/<topic>/topic.yaml`)

See `review/_template/topic.yaml` (a real review) and `benchmark/emicizumab_pk/topic.yaml` (a benchmark).

### Keys

Defaults are in parentheses.

**Top level (run file)**

| Key | Meaning |
|---|---|
| `include` | files loaded first (the folder's topic.yaml) |
| `run_dir` (derived: `runs/<run file's path under configs/>`) | output folder; write it only to override |
| `cache_dir` (`.cache`) | LLM and HTTP cache shared by all runs |
| `budget` (required) | what `run_dir` may spend, in USD: `total_usd`, one hard ceiling, and `stages`, a limit for every stage that calls a model (`protocol`, `search`, `screen`, `eligibility`, `snowball`, `extract`, `rob`, `synthesis`, `feedback`), each a number or `unlimited`. A missing stage is an error (`retrieve` and `report` make no model calls). A call runs only when the total and its stage both have room; a job that reaches a limit stops cleanly and keeps what it finished, and run again after the limit is raised it continues (the web interface: **Raise and resume**). Spending is cumulative (usage.jsonl); the section can change at any time (Settings → Budget). Evaluations have their own `eval.budget_usd` |
| `contact_email` | sent to NCBI, Unpaywall and OpenAlex; Unpaywall needs it. May also be put in `api_env.yaml` (kept out of git) instead of the run file |
| `ncbi_api_key` | optional, higher PubMed rate limit |
| `models_file` (`configs/models.yaml`) | the model catalog |

**`llm_defaults` (run file)**: the request settings every model uses; a model cell can override any of them.

| Key | Meaning |
|---|---|
| `api_env` (`api_env.yaml`) | file with the API keys |
| `temperature` (0.0), `max_tokens` (8192), `timeout` (600 s), `max_retries` (4) | request settings |
| `json_mode` (true) | ask for JSON output (`response_format`) |
| `concurrency` (4) | parallel requests from this process |

**`models` (run file)**: the model of each stage and role. `models.default.writer` (required; `gpt-6.1-sol` unless written) is the model of every stage and role not written otherwise; `models.default.verifier` is the model of every second reading (unset: `models.default.writer`). `models.<stage>.writer` and `models.<stage>.verifier` override them for one stage. Stages: `protocol`, `search`, `screen` (title/abstract), `eligibility` (full-text screening; its verifier also settles the eligibility doubts raised at extraction), `snowball` (citation chasing, which screens the papers it finds), `extract`, `rob`, `synthesis` (writing, and the verifier's check of each statement), and `feedback` (your feedback: locating, checking and the revisions it starts). Only `screen`, `eligibility`, `rob`, `synthesis` and `feedback` have a verifier. Full-text retrieval (`retrieve`) and the report make no model calls. A cell is a model name or a mapping:

| Key | Meaning |
|---|---|
| `model` | model name from the catalog; `auto` for a single-model local server |
| `provider`, `pricing`, `base_url` | from the catalog; write them only for a model that is not in it (`provider`: `openai`, `gemini`, `anthropic` or `local`; `pricing`: `{input_per_m, output_per_m}` in USD) |
| `reasoning_effort` | `low`, `medium`, `high` (Claude also `xhigh`, `max`) or null (model default) |
| `extra_body` | extra request parameters, e.g. Qwen `chat_template_kwargs`; not passed on to a cell of another provider |
| `fallbacks` | Claude only: `default` re-runs a declined request on another model (server-side refusal fallback) |
| any `llm_defaults` key | e.g. `max_tokens` for one stage |

The web interface's Settings page edits this section (a table of stages and roles). A stage's cells are fixed once that stage has started (rule 6); stages not yet started can be changed freely. A model picked in the web interface for one re-run or one piece of feedback replaces the writers of that job only (never a verifier). Every call is booked to its stage and role (`usage.jsonl`, the Activity page and each stage page); the report lists the model each stage ran with.

**`local` (run file, for `provider: local`)**: `endpoints_file` (`local_endpoints.yaml` in the project root), `poll_interval` (2 s), `retry_interval` (5 s), `probe_timeout` (5 s), `wait_timeout` (null = wait until cancelled). See GUIDE section 4.

**The switches that shape a review.** All under `pipeline:` in the run file except `budget`. Change them on the **Configs** page (open `run.yaml`, edit, save) or in a text editor. "Fixed" ones need an accept once the review has started (rule 6); the others take effect on the next run.

| Key | Values (default first) | What it changes | Fixed after start |
|---|---|---|---|
| `budget` | `total_usd` + a limit per stage | how much the run folder may spend; Settings → Budget edits it | no |
| `human_mode` | `human_in_the_loop`, `agent_only` | whether people decide, edit and approve, or only supply PDFs | yes |
| `fulltext_unclear` | `await`, `include`, `exclude` | a study still unclear after its full text: a person decides, or the rule includes / excludes it | yes |
| `verify_screening` | `off`, `pending`, `conflict`, `full_text`, `all` | which screening decisions a second agent reads (levels below) | no |
| `verify_screening_blind` | `false`, `true` | the screening verifier sees the screener's answer, or reads independently | no |
| `verify_rob` | `off`, `flagged` | risk-of-bias drafts with a reason for a second look go to you, or to the verifier first | no |
| `reassess_excluded` | `all`, `needed` | after a criterion changes, which excluded records are read again | no |
| `confirm_search` | `false`, `true` | the search waits for a query the team confirmed | no |
| `wait_for_pdfs` | `false`, `true` | the first synthesis waits until requested PDFs are supplied or declared not obtainable | no |
| `citation_chasing` | `true`, `false` | follow references and citations of included studies (stage 6); `false` skips it | no |
| `batch_stages` | `[]`, `[screen, eligibility]` | those stages' writer calls go through Anthropic batches at half price, slower | no |
| `skip_stages` | `[]`, e.g. `[rob]` | stages left out entirely | no |

**Second readings of screening (`verify_screening`).** Each level reads what the one before it reads, and more. One verifier call per record read; the Verify button on the Full-text screening page follows the level and shows the count and cost first.

| Level | The verifier reads | Typical share of full-text decisions |
|---|---|---|
| `off` | nothing | 0 |
| `pending` | full-text decisions the screener could not make (undecided) | the undecided ones |
| `conflict` | also full-text decisions the screener's overall answer disagrees with: its criterion answers give include and it answered exclude or uncertain, or the reverse | about 1 in 6 (aspirin review: 91 of 527) |
| `full_text` | also every other full-text include and exclude | all |
| `all` | also every title/abstract exclusion by the agent | all, plus title/abstract exclusions |

When the two agents agree, the decision stands. When the verifier decides a record the screener left undecided, its decision stands ("included (verifier)"). When they disagree on a decision, the record becomes undecided and goes to you with both readings (agent-only: `fulltext_unclear` applies, flagged); a disputed title/abstract exclusion is forwarded to the full text. A decision you made is never read again.

**`pipeline` (run file)**: every key.

| Key | Meaning |
|---|---|
| `human_mode` (`human_in_the_loop`) | `agent_only`: people may only supply requested PDFs; judgment actions are refused. `human_in_the_loop`: people also decide, edit and approve. All human input is logged in `provenance.jsonl`. |
| `max_records` (null) | benchmark only: screen a sample of this many records; every known answer is kept |
| `screen_sample_seed` (13) | seed for that sample |
| `fulltext` (true) | look for open-access full texts |
| `fulltext_dir` (null = `<run_dir>/fulltext`) | where supplied PDFs are read from |
| `fulltext_unclear` (`await`) | still unclear after the full text: `await` (a person decides), `include` (included and flagged) or `exclude`. With `human_mode: agent_only` it must be `include` or `exclude` (loading refuses `await`). |
| `reassess_excluded` (`all`) | after a criterion is added or changed, which excluded records the model reads again. `all`: every excluded record, so each has an answer on every criterion. `needed`: only those an answer on that criterion could bring back (cheaper); the others stay excluded, are shown as "Not assessed" on that criterion, and are read if a later change makes the answer matter. This is the setting for a change made by importing `protocol.yaml` or asking the agent; **Edit criteria** on the protocol card lets you choose for that change in its preview, with both costs shown. Records excluded by a screening rule are never read again. |
| `fulltext_screen_chars` (15000) | prompt budget for full-text screening (section-prioritised: abstract, results, tables, methods first). The screener only sees this much of the paper, so a small value makes it answer "unclear, not reported" for data that sit later in the text; API models take 60000, the whole paper |
| `context_chars` (60000) | prompt budget for extraction and RoB; longer papers are read in parts |
| `max_stored_chars` (400000) | how much of a full text is stored |
| `open_access_lookup` (true) | Unpaywall / OpenAlex legal open-access copies |
| `pdf_parser` (`auto`) | `auto`, `azure_cu` or `pdftotext` |
| `azure_cu_analyzer` | Azure Content Understanding analyzer |
| `vision_pages` (false), `vision_max_pages` (6) | read table/figure pages with a multimodal agent model. Only adds to a PDF that already has text: a scanned PDF with no text layer stays unreadable (add a text version, or the study is reported as unreadable) |
| `citation_chasing` (true), `citation_chasing_rounds` (1), `citation_chasing_max_citing` (400) | stage 6, before data extraction: for each included study, the papers it cites and the papers citing it (OpenAlex, else NCBI links; up to `citation_chasing_max_citing` citing papers each). Papers already in the review, published after the search end date, or not mentioning an intervention term are dropped; the rest go through title/abstract screening, Full texts and full-text screening like any record (no model calls of its own, but those screenings cost). About 6 new candidates per included study on the benchmark. `false` skips it; the report then leaves citation searching out of its methods and flow diagram, so state in your write-up that it was not done |
| `validate_terms` (true) | check drug and disease terms in RxNorm, PubChem, ChEMBL, MONDO |
| `verify_synthesis` (true) | check every synthesis statement against the cited data |
| `repair_synthesis` (true) | one repair round: a statement the check faulted is narrowed to what the data show, checked again, and retired if still unsupported |
| `verify_rob` (`off`) | risk-of-bias drafts with a reason for a second look: the agent's own doubt about a domain, a low/high judgment citing no sentence, or an overall milder than a domain. `off`: they are required To-do items. `flagged`: the verifier (`models.rob.verifier`) reads the source, the draft and the reasons, and its judgments replace the draft's (the agent's are kept beside them, "set by the verifier"); only what it cannot settle reaches you. Drafts without a reason are optional in both cases and marked as drafts in the report. The New-review form sets `flagged` for human-in-the-loop reviews. One verifier call per flagged draft |
| `verify_screening` (`off`) | `off`, `pending`, `conflict`, `full_text` or `all`: which screening decisions a second agent (`models.screen.verifier` or `models.eligibility.verifier`) reads; see **Second readings of screening** above. The New-review form sets `pending` for human-in-the-loop reviews |
| (no key) structured replies | On Claude models the prompts that run once per record or study (screening, its verifier, extraction, risk of bias and its verifier, the statement check) ask the API for their exact JSON shape (`sragent/schemas.py`), so those replies are valid JSON by construction; about 500 more input tokens per call (about 2,300 for extraction). `json_mode: false` in a model cell turns it off. Other providers keep plain JSON mode |
| `batch_stages` (`[]`) | `screen`, `eligibility`: those stages' writer calls go through Anthropic's Message Batches at half price. The job waits for the answers (usually within the hour, at most 24 hours; 1,000 requests per batch, written to the review before the next is sent); a job stopped while waiting collects its batch next time, without paying twice. Only as many requests as the budget has room for are sent. A request with no usable answer is asked normally. Verifier calls and other providers use normal calls |
| `verify_screening_blind` (false) | the screening verifier is shown the record and the criteria, not the screener's assessments or decision; the two readings are compared afterwards with the same outcomes. An independent second reading (the default verifier sees the first answer, which pulls it towards agreeing); the benchmark compares the two before this becomes the default |
| `confirm_search` (false) | human-in-the-loop: the search runs only with a query the review team confirmed on the Search stage page (Search strategy: the agent's query and five alternatives with their PubMed counts, changes asked in plain words, any query counted); without it the agent's query runs. The New-review form sets it for human-in-the-loop reviews |
| `keep_fetched_files` (true) | what the agent fetched is kept in `fulltext/auto/` beside your PDFs: the parsed text (`<id>.txt`) and, for articles with a PMC id, the PDF from Europe PMC or NCBI (`<id>.pdf`, best effort). `sragent fetch-files` does it for an existing review |
| `wait_for_pdfs` (false) | real reviews: the first synthesis is written only once every requested full text has been supplied or declared not obtainable, so it is written once on the settled set; the run stops before the synthesis and says what is missing (`needed_pdfs.csv`), and `sragent run` continues when the list is settled. A synthesis that exists is never held back; later PDFs still propagate through the update engine |
| `skip_stages` ([]) | stages to leave out, e.g. `[rob]` or `[snowball]`; `protocol`, `screen` and `report` cannot be skipped (the earlier `fulltext` skips both `retrieve` and `eligibility`) |

**`update_policy` (run file)**: how the update engine treats each kind of output when it is out of date (a dependency changed after it was computed): `screen`, `extraction`, `rob`, `synthesis`, each `auto` (brought up to date and applied: the default), `propose` (brought up to date in a sandbox and offered in the To-do list to accept or reject) or `manual` (only listed; a person decides). Example: `update_policy: {synthesis: propose}` keeps every change to the conclusions under your approval. Outputs a person decided, confirmed or edited are never overwritten whatever the policy.

**`feedback` (run file)**: `verify_feedback` (true, check feedback against the evidence first), `propagate` (true, revise dependent outputs), `auto_accept` (false, apply proposals without confirmation), `post_check` (true), `max_repair_rounds` (1), `global_audit` (true).

**`eval` (run file; `sragent eval` only)**: `budget_usd` (8.0 per evaluation run), `seed` (2026), `grounding_max_studies` (15; null = all), `claim1_sets` (2), `claim1_repeats` (1), `claim1_source_chars` (5000), `claim1_total_chars` (450000, about 115k tokens; the per-study cap shrinks for both conditions until the evidence-linked bundle fits), `claim1_genuine_checks` (5), `claim2_cases_per_type` (1), `claim2_repeats` (1), `claim2_build_attempts` (3), `judge_agreement_items` (25), `human_audit_items` (15). See GUIDE section 10.

**`topic` (topic file)**

| Key | Meaning |
|---|---|
| `key` | short id |
| `question` | the review question |
| `seed_eligibility` | eligibility text; the planner turns it into atomic criteria (editable in `runs/<name>/protocol.yaml`) |
| `screening_rules` | deterministic exclusions before any LLM call: `empty_record`, `conference_abstract`, `erratum`, `editorial`, `review`, `off_topic_no_abstract` (default: all except `review`) |
| `records.source` | `synergy` = benchmark with known answers (with `synergy_key`, `data_dir`); `pubmed` (with `pubmed_retmax`, `maxdate`) or `file` (with `file`, a JSONL path) = real review |
| `records.maxdate` | search end date, also used for citation chasing |
| `extraction_fields` | `{name, description, kind}`; `kind: drug` or `disease` turns on database term checks. `kind: rows` with `columns` extracts one row per result for a quantitative synthesis (below) |
| `rob_tool_name`, `rob_domains` | quality tool name and domains `{name, description}` |
| `protocol_file` | optional fixed protocol YAML instead of LLM planning |

**Rows items (`kind: rows`), for a meta-analysis.** An ordinary item is one short text per study. A rows item is a table: one row per result (outcome × comparison × dose × time point), each row with its own cited sentences.

```yaml
- name: outcome_data
  kind: rows
  description: "One row per result that compares aspirin with a control."
  columns:
    outcome_family: [sFlt-1, PlGF, invasion, other]     # a list: one of these values
    n_aspirin: number                                   # a number, copied as the paper writes it
    mean_aspirin: number
    dispersion_type: [SD, SEM, 95% CI, not stated]
    challenge: {type: text, description: "none, hypoxia, H2O2 ... as written"}
    source: [text, table, figure]
```

The model copies numbers exactly as written and computes nothing (no SEM to SD, no values read off a figure: a result shown only in a figure is a row with source `figure` and no numbers). Every number is checked against the row's cited sentences; one found nowhere in the paper is shown beside the row, and a value that is not on a column's list is kept and marked. The item's value is a short summary ("8 rows: sFlt-1 ×3, invasion ×5"). The study page shows the rows as a table, and **Edit values** edits them as one JSON object per line. Every row of every included study goes to `meta_rows.csv` (Data extraction page → *Rows for the meta-analysis (CSV)*, `sragent op extract.export_rows --config C`, and with every report) for the analysis, for example in R with metafor. Columns are written in the topic file; the data-items card keeps them as they are.

**Pooling (`topic.meta_analysis`).** The statistics are general code (`sragent/meta.py`); a review only says, in its topic file, which rows item to pool, which rows count, how to group them and which columns hold what:

```yaml
meta_analysis:
  field: outcome_data
  include: {comparison_type: [aspirin vs control]}     # rows used
  group_by: [outcome_family, model_tier, readout]      # one pooled estimate per combination
  min_studies: 3
  classify:                             # a group_by dimension the rows do not hold, derived by rules
    - set: readout                      # "what was measured": blood and expression are not one quantity
      column: analyte_name              # the text the patterns are matched against (any case)
      also: [measure_level, model]      # more columns added to that text (the sample counts)
      rules: [{label: release / circulating level, pattern: 'serum|plasma|medium|secret'}, ...]
      otherwise: {column: measure_level, map: {protein: protein level}}
  columns:
    treated: {n: n_aspirin, value: value_aspirin, spread: dispersion_aspirin, upper: dispersion_aspirin_upper}
    control: {n: n_control, value: value_control, spread: dispersion_control, upper: dispersion_control_upper}
    value_type: value_type          # mean, geometric mean -> ratio of means; number of events, percent -> risk ratio
    spread_type: dispersion_type    # SD, SEM, 95% CI
    normalised: {column: measure_basis, value: normalised to control}
    binary_only_if: {column: unit_of_n, values: [participants]}   # elsewhere a percentage is a mean (% apoptotic cells)
    risk_of_bias_high: high         # the overall rating left out in a sensitivity analysis
```

`sragent op synthesis.meta_analysis --config C` (no model calls) writes `<run folder>/meta/`: `summary.md` (the pooled table), `effects.csv` (one line per study and group), `results.json` and a forest plot per pooled group. Effects are log ratios, treated over control; several rows of one study in a group are averaged into one effect; pooling is random effects with the Hartung-Knapp interval, I² and a prediction interval; the sensitivity columns leave out values estimated from figures and studies at high risk of bias; rows that cannot be used are counted by reason. The section is not a fixed setting: it can change without an accept. Grouping is what keeps unlike things apart: a concentration in maternal serum, the same protein inside a trophoblast and its transcript are three quantities, and nothing is pooled across a `group_by` dimension. `classify` adds a dimension the extracted rows do not hold, read from the wording of a measurement (and of the sample, through `also`), so "what was measured" can group the analysis without the extraction model having to label it. Optional keys: `refine` (finer groups from a text column by rules, first match wins: `{when: {outcome_family: clinical outcome}, column: analyte_name, set: outcome_family, rules: [{label: preeclampsia, pattern: 'pre-?eclampsia'}], otherwise: other}` gives groups like `clinical outcome: preeclampsia`), `same_study` (lists of papers that report one trial: in a group only one counts; two studies with the same effect are listed as possible duplicates), `columns.direction` (the column counted for groups that are not pooled). `exclude` (rows left out by a rule on a text column, counted by its label: `[{column: model, pattern: '\\b(mouse|mice|rats?)\\b', label: animal model}]`). `columns.risk_of_bias_high` is an overall rating, or `{judgment: high, domains: [...]}` when only some domains are about bias (others, such as dose or model relevance, say how well a study applies). A median with its quartiles or range is converted to a mean and SD (Wan et al. 2014) and marked. In the web interface, the Synthesis page shows the pooled table (click a row for its forest plot; **Compute again** needs no model) and the Data extraction page has **Read figures** (with the cost first) and a selectable **Extract** for studies not extracted yet. With a plan, the synthesis is written per pooled group from its pooled result and that group's rows, plus one call for what was not pooled, and the report carries the pooled table and the forest plots.

**The report for readers (`report_plan.yaml`).** The standard report (`report_evidence.html`) lists everything for verification. A report for readers is planned: a file `report_plan.yaml` beside the run file gives the title, the key messages, the concept map (how extracted results are grouped for figures), the settings, the dose scales and the sections, each made of paragraphs, lists, figures and tables. `sragent op report.manuscript --config C` renders it as `<run folder>/manuscript.html` with each figure also in `manuscript_figures/`; `sragent op report.inventory --config C` lists what the data can show (studies per model and concept, the pooled groups, the figure types). Neither makes a model call.

Figure types: `prisma`, `forest`, `direction_plot`, `evidence_map`, `dose_plot`, `rob_summary`, `mechanism_map`, `pathway` (a signalling cascade whose nodes the plan declares and whose evidence the system fills in; `flow: down` for a deep chain, `settings: [a, b]` for one evidence line per setting). Table types: `pooled` (one column per grouping dimension, so what was pooled with what is readable), `concepts` (one line per molecule **and setting**, with the doses used, one summarising direction and the per-study counts), `modulation` (what kind of change each molecule shows in each setting: expression, activation, activity, release or function), `dose_check` (whether each study's dose is the dose a patient receives), or columns and rows written in the plan.

Keeping unlike evidence apart is the plan's job too:

```yaml
settings:                                 # nothing is counted or pooled across these
  # `given` is where the drug was given: a dose written per day was given to the patient, a concentration was added
  # in vitro. Tissue from a treated patient is in-vivo evidence; her serum dosed in a tube is in-vitro evidence.
  - {key: cells, label: Aspirin added in vitro, short: cells, given: in vitro, tiers: [cell line, primary cells, explant or tissue, pregnant women]}
  - {key: exvivo, label: Tissue from treated women, short: ex vivo, given: in vivo, tiers: [cell line, primary cells, explant or tissue]}
  - {key: women, label: Pregnant women, short: women, given: in vivo, tiers: [pregnant women]}
group_labels: {outcome_family: Outcome, model_tier: Model, readout: What was measured}
concepts:                                 # every row of a figure or table reads at one level
  column: analyte_name
  tier_column: model_tier
  domains: [{key: angio, label: Angiogenic balance}, {key: function, label: Cellular function}]
  items:
    - {name: sFlt-1, domain: angio, pattern: 'sflt|flt-?1', leads_to: [tube formation]}   # the function it ends in
    - {name: tube formation, domain: function, pattern: 'tube|angiogen', short: tube formation}
dose:                                     # the concentration the cells were given
  value: aspirin_dose
  unit: aspirin_dose_unit
  unit_label: µM
  factors: {uM: 1, mM: 1000, "ug/mL": 5.55}
  band: [10, 100]                         # what the patient reaches, for the figure and the dose check
  clinical: {unit_label: mg/day, factors: {mg/day: 1}, band: [75, 150]}    # the dose the patient took
```

A figure or table covers one setting with `setting: <key>` (or its own `tiers:`). Rows are grouped by domain in the order the domains are declared, so a domain is never split into two blocks; a molecule with `leads_to` is labelled "name → function", which is the plan's reading of its own cascades, and a `leads_to` naming nothing is an error. The readout classes are the ones the pooling groups by (`topic.meta_analysis.classify`): one definition, so a table line and a pooled estimate always mean the same quantity.

Numbers the system can compute are written as tokens: `{{pool:<group>|<measure>}}` (a group name may leave out dimensions where what is left names one group, and is an error where it names two), `{{n:included}}` (also `{{n:search_date}}`, `{{n:screened}}`, `{{n:assessed}}`, `{{n:human_decisions}}`, `{{n:high_risk}}`, `{{n:studies:<setting>}}`, `{{n:rows_left_out:<reason>}}`), `{{dir:<item>|<direction>|<setting>}}` (how many studies of that setting report it), `{{dose:<setting>|<scale>|<verdict>}}` (how many studies used the dose a patient receives). Citations are `[@<study id>]`, references to figures and tables `{fig:<id>}` and `{tab:<id>}`. The renderer refuses a plan whose citations are not included studies, whose pooled groups do not exist or are ambiguous, or whose settings and concepts are not declared. See `docs/refactor/07-report-planning.md`.

**Results shown only in a figure.** `sragent op extract.read_figures --config C` (optionally `--args '{"rids": [...]}'`) shows the PDF page of each such figure to the extraction model and fills the row's empty columns with what it reads from the plot. These are estimates, not reported numbers: each column filled this way is marked (≈ on the study page, `estimated_from_figure` in the CSV), so an analysis can leave them out. A reported number or a value you entered is never replaced. A study whose text came without a PDF is skipped; add `<id>.pdf` to the full-text folder to read its figures. `sragent op extract.studies --config C --args '{"rids": [...]}'` extracts chosen included studies only (a pilot).
