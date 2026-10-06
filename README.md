# SRAgent

SRAgent turns a review question into an editable protocol, retrieves literature records, and screens their titles and abstracts. This version is for checking the search and eligibility decisions before moving to full-text review.

## Setup

From this folder, create a Conda environment and install the project:

```bash
conda env create --name sragent --file environment.yml
conda activate sragent
python -m pip install -e ".[dev]"
cp api_env.example.yaml api_env.yaml
```

If `sragent` already exists, skip creation and activate it. After changing versions, rerun the install command from that version’s folder; the editable install uses the most recently installed folder.

Fill in `api_env.yaml`, or set `OPENAI_API_KEY` / `GEMINI_API_KEY`. Set model IDs and token prices in `configs/backend_api.yaml` for your account. LLM calls use the OpenAI or Gemini API; no GPU is needed.

## Inputs

| Input | Where to set it |
|---|---|
| Review question and eligibility text | `configs/topics/emicizumab_pk.yaml`: `topic.question` and `topic.seed_eligibility`. |
| Record source | The same topic file: `topic.records.source` accepts `synergy`, `pubmed`, or `file`. The example uses the bundled `data/donners_2021_records.jsonl` benchmark. |
| Imported records | For `source: file`, set `topic.records.file` to a JSONL file. Each line should contain `rid`, `title`, and `abstract`; `pmid`, `doi`, and `year` are optional. |
| Run settings | `configs/emicizumab_api.yaml`: `run_dir`, `cache_dir`, `budget_usd`, and `pipeline.max_records`. It includes the topic and backend YAML files. |
| API credentials | `api_env.yaml` or environment variables, loaded by `sragent/config.py`. |

Run commands from the project root: relative data and output paths use the working directory. A YAML `include` path is relative to its containing config file.

## Pipeline

[sragent/cli.py](sragent/cli.py) handles commands; [sragent/pipeline.py](sragent/pipeline.py) runs these stages in order. Node names below refer to entries in `<run_dir>/store.json`; `rid` is the study record ID.

| Step | Code | Input → output |
|---|---|---|
| `plan` | [sragent/stages/planning.py](sragent/stages/planning.py) | Question and seed eligibility → `protocol.yaml`, `protocol` and `crit:<id>` nodes. |
| `search` | [sragent/stages/search.py](sragent/stages/search.py) | Protocol and record source → query/duplicate counts in `search`, deduplicated `rec:<rid>` nodes. |
| `screen` | [sragent/stages/screening.py](sragent/stages/screening.py) | Records and criteria → `screen:<rid>` decisions, reasons, and supporting evidence. |

This pipeline ends after title/abstract screening. Uncertain records remain available for review in `store.json`.

`sragent/evidence.py` resolves sentence references, `sragent/store.py` keeps node versions and dependencies, and `sragent/llm.py` handles API calls, caching, and usage tracking.

## Run and inspect

```bash
sragent run --config configs/emicizumab_api.yaml --plan-only
# Review/edit runs/emicizumab_api/protocol.yaml before continuing.
sragent run --config configs/emicizumab_api.yaml
sragent status --config configs/emicizumab_api.yaml
```

Use `--until STAGE` to stop at any step in the table. Repeating a run skips completed work. Use `configs/emicizumab_api_gemini.yaml` for Gemini-only calls.

With the example config, outputs are in `runs/emicizumab_api/`:

- `protocol.yaml`: the editable protocol.
- `store.json`: records, decisions, evidence, and node history; `store_changelog.jsonl`: the change log.
- `usage.jsonl`, `cost.json`, and `run.log`: API usage, calculated cost, and execution messages.

Reusable LLM and literature-response caches are stored in `.cache/`. Run outputs, caches, and credentials are ignored by Git.

## Tests

```bash
python -m pytest -q tests/
```

`tests/test_offline.py` checks the implemented workflow; `tests/test_api.py` checks configuration and API request construction with a stub client. Tests block network access and do not spend API credits.
