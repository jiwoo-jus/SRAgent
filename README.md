# SRAgent

SRAgent is a human-in-the-loop agent for systematic reviews. Given a review question and a set of literature records, it screens studies, extracts data, assesses study quality, and writes a cited narrative synthesis. Every claim links back to the sentence in the source paper that supports it.

This is a research prototype. Quality assessments and synthesis statements are drafts for a human reviewer.

## Baseline pipeline

The baseline runs the full input to output pipeline below on a bundled example (see [Example task](#example-task)).


| Step         | What it does                                                                                                                  | Output                              |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------- |
| `plan`       | Turns the question and seed eligibility text into an editable protocol (PICO and atomic criteria)                             | `protocol.yaml`                     |
| `search`     | Loads records from SYNERGY, PubMed, or a JSONL file and removes duplicates                                                    | `rec:<rid>` nodes                   |
| `screen`     | Judges each criterion (met, not met, unclear) with a supporting quote, then derives a decision by rule                        | `screen:<rid>`                      |
| `fulltext`   | Gets full text from supplied files or Europe PMC, falls back to the abstract                                                  | `ft:<rid>`                          |
| `extract`    | Extracts configured fields with source sentences and checks drug and disease terms against RxNorm, PubChem, ChEMBL, and MONDO | `ext:<rid>`                         |
| `rob`        | Drafts quality judgments per domain with evidence                                                                             | `rob:<rid>`                         |
| `synthesize` | Writes cited statements and checks each against the cited data                                                                | `syn:<id>`                          |
| `report`     | Builds the final report and an evidence view                                                                                  | `report.md`, `report_evidence.html` |

Code for each step is in `sragent/stages/`. `sragent/pipeline.py` runs them in order, and each step resumes where it stopped. Results are stored as versioned nodes in `store.json` (`sragent/store.py`).

## Example task

The example reviews the pharmacokinetics of emicizumab in humans. It uses the SYNERGY benchmark `Donners_2021` (253 bundled records, 15 gold includes) so screening can be compared with the published review. The question, eligibility text, extraction fields, and quality domains are in `configs/topics/emicizumab_pk.yaml`.

## Setup

```
conda env create --prefix "$PWD/.conda" --file environment.yml
conda activate "$PWD/.conda"
python -m pip install -e ".[dev]"
cp api_env.example.yaml api_env.yaml
```

Fill in `api_env.yaml` (or set `OPENAI_API_KEY` or `GEMINI_API_KEY`) and set model IDs in `configs/backend_api.yaml`. Requires Python 3.10 or newer.

## Run

```
sragent run --config configs/emicizumab_api.yaml --plan-only
# edit runs/emicizumab_api/protocol.yaml if needed
sragent run --config configs/emicizumab_api.yaml
sragent status --config configs/emicizumab_api.yaml
```

Use `--until STAGE` to stop early. Use `configs/emicizumab_api_gemini.yaml` for Gemini only. Run from the project root so relative paths resolve.

Other commands include `rescreen` (re-derive decisions after editing the protocol, no LLM calls), `revalidate-terms`, and `data --synergy KEY`.

Outputs go to `runs/emicizumab_api/`.

- `protocol.yaml` is the editable protocol.
- `store.json` and `store_changelog.jsonl` hold decisions, evidence, and history.
- `report.md` and `report_evidence.html` are the review and its source-linked view.
- `usage.jsonl`, `cost.json`, and `run.log` track API usage and cost.
- `fulltext/needed_pdfs.csv` lists papers that would help. Put `<rid>.pdf` or `<rid>.txt` in `fulltext/` before the `fulltext` step.

Caches are in `.cache/`. Outputs, caches, and credentials are ignored by Git.

## Local models

The same pipeline runs on your own vLLM servers.

```
sragent local-endpoints --config configs/emicizumab_local.yaml --nodes <node1> <node2> --probe
sragent run --config configs/emicizumab_local.yaml
```

`local-endpoints` only registers servers that are already running and does not start vLLM. Use `--urls` instead of `--nodes` for a server you reach through your own tunnel. Rerun it with the full new list at any time and active runs pick it up. See `configs/local_endpoints.example.yaml` for the file format.

## Tests

```
python -m pytest -q tests/
```

Tests run offline and use no API credits.

## Repository layout

```
sragent/    pipeline, stages, sources, LLM and local-model clients
configs/    topic, backend, and run configs
data/       bundled SYNERGY records for the example
tests/      offline tests
```
