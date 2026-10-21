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

Other runs are made from these when needed, e.g. the second evaluation condition (GUIDE 10.0): copy `gemini_agentonly.yaml` as `gemini_humanloop.yaml`, set `human_mode: human_in_the_loop` and drop `fulltext_unclear`. For another model, change `llm_defaults.model` (and the roles) to names from the catalog. For your own vLLM servers, write `provider: local` and `model: auto` under `llm_defaults` (below).

## The model catalog (`models.yaml`)

| Model | Provider | Price per 1M tokens (in / out) |
|---|---|---|
| `gemini-3.8-flash` | gemini | $0.75 / $3.75 |
| `gemini-3.1-pro-preview` | gemini | $2.00 / $12.00 |
| `gemma-4-31b-it` | gemini | $0.10 / $0.30 (placeholder) |
| `claude-haiku-5-5` | anthropic | $0.10 / $0.50 up to 100K-token prompts, $0.50 / $2.50 beyond |
| `claude-sonnet-5-5` | anthropic | $2.00 / $10.00 |

The catalog holds **facts about each model**: `provider`, `pricing` (`{input_per_m, output_per_m}`, optionally `long_context: {threshold_tokens, input_per_m, output_per_m}`), optionally `base_url` and a `note`. A run file chooses models **by name**, and their provider and price are filled in from here (a catalogued model's provider is a fact: it is taken from the catalog even if a run file writes another), so a price is written in one place only (it drives the budget guard and every cost estimate). Choices (reasoning effort, `max_tokens`, `concurrency`, `api_env`, `fallbacks`) go in the run file, not here. The web interface offers every model in the catalog. To use another model, add it here. Removing a model that a run file still uses is refused by the editor. Local vLLM models need no entry.

Roles: **agent** does the review; **verifier** (optional) checks feedback against the evidence, re-checks revised outputs and checks every synthesis statement, and is the agent role unless set (so with `stage_models.synthesize` on a stronger model, the statements are still checked by another model); **judge**, **judge2** and **reviewer** are used only by `sragent eval`; set **reviewer** explicitly and keep it the same across runs you compare (Claim 1 measures the materials, not the reviewer).

## Recommended models (October 2026 prices)

The rule: a cheap model for the high-volume work whose mistakes a later step catches; a stronger model for the few calls that everything else rests on; and checks by a model of another family than the writer. `review/adhd_appetite_adverse_events/` is set up this way.

| Where | Model, effort | Why |
|---|---|---|
| `plan`, `search` | Sonnet, high | one call each; the criteria and the query decide what the review can find |
| `screen` (title/abstract), `snowball` | Haiku, medium | hundreds of calls, inclusive by design; a wrong forward is caught at full text |
| `fulltext` | Haiku, high | decides inclusion; high effort costs a few cents more and stays ~10x cheaper than Sonnet (Sonnet medium is the upgrade if the budget allows: about $1.5 per 130 full texts) |
| `extract` | Sonnet, medium | the values the synthesis, the risk of bias and every revision rest on; the benchmark showed the largest quality gap between the two models here |
| `rob` | Haiku, medium | drafts a person confirms |
| `synthesize` | Sonnet, high | the conclusions; a handful of calls |
| `feedback` | Sonnet, medium | revisions and propagation: few calls, high stakes |
| `roles.verifier` | GPT-6.1 Sol, medium | checks statements and feedback: a third family, independent of the writer (Claude) and the judge (Gemini) |
| `roles.judge` | Gemini 3.1 Pro, low | evaluation; another family than the agent |
| `roles.judge2` | GPT-6.1 Sol, low | second judge of a third family, so judge agreement bounds family bias |
| `roles.reviewer` | Gemini 3.8 Flash, low | Claim 1's simulated reviewer; keep it fixed across the runs you compare |

Costs (benchmark sizes: about 300 records, 130 full texts, 26 included): a review runs for about $2, of which extraction with Sonnet is about $1. Haiku everywhere was $1.36; Sonnet everywhere would be about $12.

## How to write a config

### Rules

1. **Three kinds of file, one job each.** `topic.yaml` only has `topic:`. The catalog `models.yaml` only has `models:` (facts about models). A run file has `include:` (its folder's `topic.yaml`), the models it chooses (`llm_defaults`, `roles`, `stage_models`) and run settings.
2. **Layering.** `include:` files are loaded first, in order; keys in the including file override them. Nested sections merge key by key, so a run file writes only what it changes. Anything not written takes the default from `DEFAULTS` in `sragent/config.py`, which lists every key.
3. **Roles inherit.** Each role (`agent`, `verifier`, `judge`, `reviewer`, `judge2`) = `llm_defaults` + its own entry under `roles:`. Write in a role only what differs. The provider and price of a role's model come from the catalog, unless the role writes its own.
4. **Paths.** `include`, `api_env`, `local.endpoints_file` and `models_file` are relative to the file they are written in. `cache_dir`, `pipeline.fulltext_dir` and the topic's `records.data_dir`, `records.file` and `protocol_file` are relative to the project root (run `sragent` from there). `run_dir` is derived from the run file's path; write it only to override that.
5. **Naming.** One folder per topic. Benchmark run files are `<models>_<characteristic>.yaml` (e.g. `gemini_agentonly`, `local_agentonly`); a review's run file is `run.yaml`.
6. **Settings that define a review are fixed once it has started.** The topic (question, eligibility, screening rules, records, extraction fields, risk-of-bias tool and domains) and `pipeline.human_mode`, `max_records`, `screen_sample_seed` and `fulltext_unclear` are recorded, and so is each stage's model and effort once that stage has started, in the run's `store.json` when it first saves. If any of them is edited afterwards, by the web interface, a text editor or a changed include, the run stops before any LLM call and shows what changed. Then: set them back; make a new run file for the new design (its own `runs/` folder); or accept the change on purpose with `sragent accept-config --config <run file> --reason "..."` (or Accept in the web interface). An accepted change is written to `provenance.jsonl` and shown in the report (Appendix D) and in every evaluation of the run. Budget, contact email, the models of stages not yet started, the evaluation roles, `feedback.*` and `eval.*` can change at any time. A run started before this check existed records its settings on its next save.
7. **No keys in configs.** API keys live in `api_env.yaml` (project root, git-ignored) or environment variables.

### Run file (`configs/<benchmark|review>/<topic>/<run>.yaml`)

```yaml
# <Benchmark|Review> run: <topic> · <models> · <records> · <people>.
include:
  - topic.yaml                         # what to review (same folder)
llm_defaults:                          # models by name; provider and price come from configs/models.yaml
  model: claude-haiku-5-5              # the agent, and every role and stage that does not say otherwise
  api_env: ../../../api_env.yaml       # the keys file in the project root (relative to this file)
  reasoning_effort: low
  max_tokens: 16000
  concurrency: 6
roles:                                 # evaluation roles (sragent eval); only what differs
  judge:  {model: claude-sonnet-5-5, reasoning_effort: medium}
  judge2: {model: gemma-4-31b-it, reasoning_effort: null}
stage_models:                          # optional: another model or effort for single stages
  plan:    {model: claude-sonnet-5-5, reasoning_effort: high}
  extract: {model: claude-sonnet-5-5, reasoning_effort: medium}
  screen:  {reasoning_effort: medium}  # same model as the agent, other effort
budget_usd: 5.0                        # hard cost cap for this run folder
pipeline:
  human_mode: human_in_the_loop        # or agent_only
  max_records: 60                      # benchmark only; omit for all records
eval:                                  # benchmark only, for `sragent eval`
  budget_usd: 8.0
```

**Your own vLLM servers** (free): `llm_defaults: {provider: local, model: auto, reasoning_effort: null, concurrency: 32}`. The servers and the roles each may serve are listed in `local_endpoints.yaml` in the project root; `sragent local-endpoints` edits that list (GUIDE section 4). Pool settings go under `local:` (below) only when they differ from the defaults.

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
| `budget_usd` (5.0) | cumulative cost cap for `run_dir`; the run stops cleanly when reached |
| `contact_email` | sent to NCBI, Unpaywall and OpenAlex; Unpaywall needs it. May also be put in `api_env.yaml` (kept out of git) instead of the run file |
| `ncbi_api_key` | optional, higher PubMed rate limit |
| `models_file` (`configs/models.yaml`) | the model catalog |

**`llm_defaults` and `roles.<role>` (run file)**

| Key | Meaning |
|---|---|
| `model` | model name from the catalog; `auto` for a single-model local server |
| `provider`, `pricing`, `base_url` | from the catalog; write them only for a model that is not in it (`provider`: `openai`, `gemini`, `anthropic` or `local`; `pricing`: `{input_per_m, output_per_m}` in USD) |
| `api_env` (`api_env.yaml`) | file with the API keys |
| `reasoning_effort` | `low`, `medium`, `high` (Claude also `xhigh`, `max`) or null (model default) |
| `temperature` (0.0), `max_tokens` (8192), `timeout` (600 s), `max_retries` (4) | request settings |
| `json_mode` (true) | ask for JSON output (`response_format`) |
| `extra_body` | extra request parameters, e.g. Qwen `chat_template_kwargs` |
| `concurrency` (4) | parallel requests from this process |
| `fallbacks` | Claude only: `default` re-runs a declined request on another model (server-side refusal fallback) |

**`stage_models` (run file)**: the agent's model and effort per pipeline stage, written over the agent role. Stages: `plan`, `search`, `screen` (title/abstract), `fulltext` (full-text screening), `snowball` (citation chasing, which screens the papers it finds), `extract`, `rob`, `synthesize` (writing and checking statements), and `feedback` (handling feedback, revising dependent outputs, updates after your decisions). The report makes no LLM calls. Each entry takes the role keys (`model`, `reasoning_effort`, `max_tokens`, ...); provider and price come from the catalog, so a model must be in it (or carry its own `pricing`). The web interface's Run page edits this section (Models per stage). A stage's model is fixed once that stage has started (rule 6); stages not yet started can be changed freely. A model picked in the web interface for one re-run or one piece of feedback applies to that job only. The report lists the model each stage ran with.

**`local` (run file, for `provider: local`)**: `endpoints_file` (`local_endpoints.yaml` in the project root), `poll_interval` (2 s), `retry_interval` (5 s), `probe_timeout` (5 s), `wait_timeout` (null = wait until cancelled). See GUIDE section 4.

**`pipeline` (run file)**

| Key | Meaning |
|---|---|
| `human_mode` (`human_in_the_loop`) | `agent_only`: people may only supply requested PDFs; judgment actions are refused. `human_in_the_loop`: people also decide, edit and approve. All human input is logged in `provenance.jsonl`. |
| `max_records` (null) | benchmark only: screen a sample of this many records; every known answer is kept |
| `screen_sample_seed` (13) | seed for that sample |
| `fulltext` (true) | look for open-access full texts |
| `fulltext_dir` (null = `<run_dir>/fulltext`) | where supplied PDFs are read from |
| `fulltext_unclear` (`await`) | still unclear after the full text: `await` (a person decides), `include` (included and flagged) or `exclude`. With `human_mode: agent_only` it must be `include` or `exclude` (loading refuses `await`). |
| `fulltext_screen_chars` (15000) | prompt budget for full-text screening (section-prioritised: abstract, results, tables, methods first). The screener only sees this much of the paper, so a small value makes it answer "unclear, not reported" for data that sit later in the text; API models take 60000, the whole paper |
| `context_chars` (60000) | prompt budget for extraction and RoB; longer papers are read in parts |
| `max_stored_chars` (400000) | how much of a full text is stored |
| `open_access_lookup` (true) | Unpaywall / OpenAlex legal open-access copies |
| `pdf_parser` (`auto`) | `auto`, `azure_cu` or `pdftotext` |
| `azure_cu_analyzer` | Azure Content Understanding analyzer |
| `vision_pages` (false), `vision_max_pages` (6) | read table/figure pages with a multimodal agent model |
| `citation_chasing` (true), `citation_chasing_rounds` (1), `citation_chasing_max_citing` (400) | follow references and citations of included studies |
| `validate_terms` (true) | check drug and disease terms in RxNorm, PubChem, ChEMBL, MONDO |
| `verify_synthesis` (true) | check every synthesis statement against the cited data |
| `repair_synthesis` (true) | one repair round: a statement the check faulted is narrowed to what the data show, checked again, and retired if still unsupported |
| `wait_for_pdfs` (false) | real reviews: the first synthesis is written only once every requested full text has been supplied or declared not obtainable, so it is written once on the settled set; the run stops before the synthesis and says what is missing (`needed_pdfs.csv`), and `sragent run` continues when the list is settled. A synthesis that exists is never held back; later PDFs still propagate through the update engine |
| `skip_stages` ([]) | stages to leave out, e.g. `[rob]` or `[snowball]`; `plan`, `screen` and `report` cannot be skipped |

**`update_policy` (run file)**: how the update engine treats each kind of output when it is out of date (a dependency changed after it was computed): `screen`, `extraction`, `rob`, `synthesis`, each `auto` (brought up to date and applied: the default), `propose` (brought up to date in a sandbox and offered in the To-do list to accept or reject) or `manual` (only listed; a person decides). Example: `update_policy: {synthesis: propose}` keeps every change to the conclusions under your approval. Outputs a person decided, confirmed or edited are never overwritten whatever the policy.

**`feedback` (run file)**: `verify_feedback` (true, check feedback against the evidence first), `propagate` (true, revise dependent outputs), `auto_accept` (false, apply proposals without confirmation), `post_check` (true), `max_repair_rounds` (1), `global_audit` (true).

**`eval` (run file; `sragent eval` only)**: `budget_usd` (8.0 per evaluation run), `seed` (2026), `grounding_max_studies` (15; null = all), `claim1_sets` (2), `claim1_repeats` (1), `claim1_source_chars` (5000), `claim1_total_chars` (300000; the per-study cap shrinks for both conditions until the evidence-linked bundle fits), `claim1_genuine_checks` (5), `claim2_cases_per_type` (1), `claim2_repeats` (1), `claim2_build_attempts` (3), `judge_agreement_items` (25), `human_audit_items` (15). See GUIDE section 10.

**`topic` (topic file)**

| Key | Meaning |
|---|---|
| `key` | short id |
| `question` | the review question |
| `seed_eligibility` | eligibility text; the planner turns it into atomic criteria (editable in `runs/<name>/protocol.yaml`) |
| `screening_rules` | deterministic exclusions before any LLM call: `empty_record`, `conference_abstract`, `erratum`, `editorial`, `review`, `off_topic_no_abstract` (default: all except `review`) |
| `records.source` | `synergy` = benchmark with known answers (with `synergy_key`, `data_dir`); `pubmed` (with `pubmed_retmax`, `maxdate`) or `file` (with `file`, a JSONL path) = real review |
| `records.maxdate` | search end date, also used for citation chasing |
| `extraction_fields` | `{name, description, kind}`; `kind: drug` or `disease` turns on database term checks |
| `rob_tool_name`, `rob_domains` | quality tool name and domains `{name, description}` |
| `protocol_file` | optional fixed protocol YAML instead of LLM planning |
