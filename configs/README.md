# Configs

```
configs/
  models/                     WHICH LLMs do the work (shared by every run)
    gemini.yaml                 Gemini for every role
    claude.yaml                 Claude: Haiku agent/reviewer, Sonnet judge
    local.yaml                  your own vLLM servers (listed in ../../local_endpoints.yaml)
  benchmark/                  topics WITH a known answer: to measure the agent
    emicizumab_pk/
      topic.yaml                what to review (question, criteria, records, fields)
      gemini_agentonly.yaml     a run = topic + models + settings
      ...
  review/                     topics WITHOUT a known answer: real systematic reviews
    _template/                  copy this folder to start one (topic.yaml + run.yaml)
```

**A run file's path is its name, and its outputs follow it.** `configs/<benchmark|review>/<topic>/<run>.yaml` writes to `runs/<benchmark|review>/<topic>/<run>/`; nothing has to be written in the file for that. Any run file can be opened in the web interface (`sragent ui --config <file>`) or run from the command line (`sragent run --config <file>`).

**What exists and where it stands:** `sragent runs` lists every run file with its models, people mode, records and the state of its outputs (not started / review done / evaluated).

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
| `benchmark/emicizumab_pk/gemini_agentonly.yaml` | Gemini | all | agent only (people only supply PDFs). The main results (GUIDE 11.0); `runs/.../gemini_agentonly/eval/README.md` lists its evaluations |
| `review/_template/run.yaml` | Gemini | all | human in the loop; template |

Other runs are made from these when needed, e.g. the second evaluation condition (GUIDE 10.0): copy `gemini_agentonly.yaml` as `gemini_humanloop.yaml`, set `human_mode: human_in_the_loop` and drop `fulltext_unclear`. For local servers, copy a run file and include `../../models/local.yaml` (e.g. `local_agentonly.yaml`).

## models/

| File | Roles |
|---|---|
| `gemini.yaml` | Gemini for every role (one API key). Agent gemini-3.8-flash, judge gemini-3.1-pro-preview, second judge gemma-4-31b-it. |
| `claude.yaml` | Claude, lowest-cost setup (one Anthropic API key, `anthropic_api_key` in `api_env.yaml`). Agent and reviewer claude-haiku-5-5, judge claude-sonnet-5-5, second judge gemma-4-31b-it (needs the Gemini key). |
| `local.yaml` | Your own vLLM servers for every role (free). The servers and the roles each may serve are listed in `local_endpoints.yaml` in the project root (next to `api_env.yaml`); `sragent local-endpoints` edits that list. |

Roles: **agent** does the review; **verifier** (optional) checks feedback against the evidence and re-checks revised outputs, and is the agent unless set; **judge**, **judge2** and **reviewer** are used only by `sragent eval`. The web interface offers every API model named in these files (with its price) plus the local endpoint list, so a price is written in one place only.

## How to write a config

### Rules

1. **Three kinds of file, one job each.** `topic.yaml` only has `topic:`. A model file only has `llm_defaults:`, `roles:` and (local) `local:`. A run file has `include:` (its folder's `topic.yaml` and one model file) plus run settings.
2. **Layering.** `include:` files are loaded first, in order; keys in the including file override them. Nested sections merge key by key, so a run file writes only what it changes. Anything not written takes the default from `DEFAULTS` in `sragent/config.py`, which lists every key.
3. **Roles inherit.** Each role (`agent`, `verifier`, `judge`, `reviewer`, `judge2`) = `llm_defaults` + its own entry under `roles:`. Write in a role only what differs.
4. **Paths.** `include`, `api_env`, `local.endpoints_file` and `models_dir` are relative to the file they are written in. `cache_dir`, `pipeline.fulltext_dir` and the topic's `records.data_dir`, `records.file` and `protocol_file` are relative to the project root (run `sragent` from there). `run_dir` is derived from the run file's path; write it only to override that.
5. **Naming.** One folder per topic. Benchmark run files are `<models>_<characteristic>.yaml` (e.g. `gemini_agentonly`, `local_agentonly`); a review's run file is `run.yaml`.
6. **No keys in configs.** API keys live in `api_env.yaml` (project root, git-ignored) or environment variables.

### Run file (`configs/<benchmark|review>/<topic>/<run>.yaml`)

```yaml
# <Benchmark|Review> run: <topic> · <models> · <records> · <people>.
include:
  - topic.yaml                         # what to review (same folder)
  - ../../models/gemini.yaml           # which LLMs
budget_usd: 5.0                        # hard cost cap for this run folder
pipeline:
  human_mode: human_in_the_loop        # or agent_only
  max_records: 60                      # benchmark only; omit for all records
eval:                                  # benchmark only, for `sragent eval`
  budget_usd: 8.0
```

### Model file (`configs/models/<name>.yaml`)

```yaml
llm_defaults:                          # shared by every role
  provider: gemini                     # openai | gemini | local
  model: gemini-3.8-flash
  api_env: ../../api_env.yaml
  reasoning_effort: low
  pricing: {input_per_m: 0.75, output_per_m: 3.75}   # USD per 1M tokens (budget guard, UI estimate)
roles:                                 # only what differs from llm_defaults
  judge: {model: gemini-3.1-pro-preview, pricing: {input_per_m: 2.00, output_per_m: 12.00}}
```

### Topic file (`configs/<benchmark|review>/<topic>/topic.yaml`)

See `review/_template/topic.yaml` (a real review) and `benchmark/emicizumab_pk/topic.yaml` (a benchmark).

### Keys

Defaults are in parentheses.

**Top level (run file)**

| Key | Meaning |
|---|---|
| `include` | files loaded first (topic, model setup) |
| `run_dir` (derived: `runs/<run file's path under configs/>`) | output folder; write it only to override |
| `cache_dir` (`.cache`) | LLM and HTTP cache shared by all runs |
| `budget_usd` (5.0) | cumulative cost cap for `run_dir`; the run stops cleanly when reached |
| `contact_email` | sent to NCBI, Unpaywall and OpenAlex; Unpaywall needs it |
| `ncbi_api_key` | optional, higher PubMed rate limit |
| `models_dir` (`configs/models`) | model setup files; the web interface offers the API models named in them |

**`llm_defaults` and `roles.<role>` (model file)**

| Key | Meaning |
|---|---|
| `provider` (`openai`) | `openai`, `gemini` or `local` |
| `model` | model ID; `auto` for a single-model local server |
| `api_env` (`api_env.yaml`) | file with the API keys |
| `base_url` | optional API endpoint override (not used for `local`) |
| `reasoning_effort` | `low`, `medium`, `high` or null (model default) |
| `temperature` (0.0), `max_tokens` (8192), `timeout` (600 s), `max_retries` (4) | request settings |
| `json_mode` (true) | ask for JSON output (`response_format`) |
| `extra_body` | extra request parameters, e.g. Qwen `chat_template_kwargs` |
| `concurrency` (4) | parallel requests from this process |
| `pricing` | `{input_per_m, output_per_m}` in USD; used for cost estimates and `budget_usd` |

**`local` (model file `local.yaml`)**: `endpoints_file` (`local_endpoints.yaml` in the project root), `poll_interval` (2 s), `retry_interval` (5 s), `probe_timeout` (5 s), `wait_timeout` (null = wait until cancelled). See GUIDE section 4.

**`pipeline` (run file)**

| Key | Meaning |
|---|---|
| `human_mode` (`human_in_the_loop`) | `agent_only`: people may only supply requested PDFs; judgment actions are refused. `human_in_the_loop`: people also decide, edit and approve. All human input is logged in `provenance.jsonl`. |
| `max_records` (null) | benchmark only: screen a sample of this many records; every known answer is kept |
| `screen_sample_seed` (13) | seed for that sample |
| `fulltext` (true) | look for open-access full texts |
| `fulltext_dir` (null = `<run_dir>/fulltext`) | where supplied PDFs are read from |
| `fulltext_unclear` (`await`) | still unclear after the full text: `await` (a person decides), `include` (included and flagged) or `exclude`. With `human_mode: agent_only` it must be `include` or `exclude` (loading refuses `await`). |
| `fulltext_screen_chars` (15000) | prompt budget for full-text screening |
| `context_chars` (60000) | prompt budget for extraction and RoB; longer papers are read in parts |
| `max_stored_chars` (400000) | how much of a full text is stored |
| `open_access_lookup` (true) | Unpaywall / OpenAlex legal open-access copies |
| `pdf_parser` (`auto`) | `auto`, `azure_cu` or `pdftotext` |
| `azure_cu_analyzer` | Azure Content Understanding analyzer |
| `vision_pages` (false), `vision_max_pages` (6) | read table/figure pages with a multimodal agent model |
| `citation_chasing` (true), `citation_chasing_rounds` (1), `citation_chasing_max_citing` (400) | follow references and citations of included studies |
| `validate_terms` (true) | check drug and disease terms in RxNorm, PubChem, ChEMBL, MONDO |
| `verify_synthesis` (true) | check every synthesis statement against the cited data |

**`feedback` (run file)**: `verify_feedback` (true, check feedback against the evidence first), `propagate` (true, revise dependent outputs), `auto_accept` (false, apply proposals without confirmation), `post_check` (true), `max_repair_rounds` (1), `global_audit` (true).

**`eval` (run file; `sragent eval` only)**: `budget_usd` (8.0 per evaluation run), `seed` (2026), `grounding_max_studies` (15; null = all), `claim1_sets` (2), `claim1_repeats` (1), `claim1_source_chars` (5000), `claim1_genuine_checks` (5), `claim2_cases_per_type` (1), `claim2_repeats` (1), `claim2_build_attempts` (3), `judge_agreement_items` (25), `human_audit_items` (15). See GUIDE section 10.

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
