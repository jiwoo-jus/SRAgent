# SRAgent

SRAgent is a human-in-the-loop agent for systematic reviews. Given a review question and a set of literature records, it screens studies, extracts data, assesses study quality, and writes a cited narrative synthesis. Every claim links back to the sentences in the source paper that support it, and every output is a versioned node in a dependency graph, so feedback on one output is checked against the evidence and carried to every output that depends on it.

This is a research prototype. Decisions, quality assessments and synthesis statements are drafts for a human reviewer.

The full documentation (data, workflow, inputs and outputs, evaluation design and results) is in **[GUIDE.md](GUIDE.md)**.

## Pipeline

| Step         | What it does                                                                                                                  | Output                              |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| `plan`       | Turns the question and seed eligibility text into an editable protocol (PICO and atomic criteria)                             | `protocol.yaml`                     |
| `search`     | Loads records from SYNERGY, PubMed, or a JSONL file and removes duplicates                                                    | `rec:<rid>` nodes                   |
| `screen`     | Judges each criterion with cited sentences, derives a decision by rule; final inclusion only after a full-text review         | `screen:<rid>`                      |
| `fulltext`   | Gets full text from your files, Europe PMC, or a legal open-access copy, and screens it                                       | `ft:<rid>`                          |
| `snowball`   | Follows references and citations of the included studies                                                                      | new `rec:<rid>` nodes               |
| `extract`    | Extracts configured fields with source sentences and checks drug and disease terms against RxNorm, PubChem, ChEMBL, and MONDO | `ext:<rid>`                         |
| `rob`        | Drafts quality judgments per domain with evidence                                                                             | `rob:<rid>`                         |
| `synthesize` | Writes cited statements and checks each against the cited data                                                                | `syn:<id>`                          |
| `report`     | Builds the final report and an evidence view                                                                                  | `report.md`, `report_evidence.html` |

Code for each step is in `sragent/stages/`. `sragent/pipeline.py` runs them in order, and each step resumes where it stopped. Results are stored in `store.json` (`sragent/store.py`).

## Configs: benchmark or real review

```
configs/models/       which LLMs do the work: gemini.yaml, claude-haiku-5-5_claude-sonnet-5-5_gemma-4-31b-it.yaml (Claude), local.yaml
configs/benchmark/    topics WITH a known answer: measure the agent (sragent eval works)
configs/review/       topics WITHOUT a known answer: real reviews (copy review/_template/)
```

A run file's path is its name: `configs/<kind>/<topic>/<run>.yaml` writes to `runs/<kind>/<topic>/<run>/`. `sragent runs` lists every run file and the state of its outputs.

The benchmark topic is the pharmacokinetics of emicizumab in humans, with the known answer of the SYNERGY review `Donners_2021` (15 included studies). Known answers are kept out of the agent's store.

| Run file | Models · records · people |
|---|---|
| `benchmark/emicizumab_pk/gemini_agentonly.yaml` | Gemini · all · agent only (the main results) |
| `review/_template/run.yaml` | Gemini · all · human in the loop (template) |

Details and how to write a config: `configs/README.md`.

## Setup

```
conda env create --prefix "$PWD/.conda" --file environment.yml
conda activate "$PWD/.conda"
python -m pip install -e ".[dev]"
cp api_env.example.yaml api_env.yaml
```

Fill in `api_env.yaml` (or set `OPENAI_API_KEY`, `GEMINI_API_KEY` or `ANTHROPIC_API_KEY`) and set model IDs in `configs/models/`. Requires Python 3.10 or newer. Run commands from the project root so relative paths resolve.

## Run

The web interface shows the steps, a to-do list of decisions, a Run panel with the model and effort per stage and a live cost estimate, and a drop folder for full-text PDFs. Its **Configs** page lists every run file (click one to open it), edits any config with a check on every change, and sets up a new review from a short form:

```
sragent ui                                                     # Configs page: open a run file or create one
sragent ui --config configs/review/<your_topic>/run.yaml       # or open one run file directly
```

On a remote machine add `--no-browser --port <free port>` and forward that port (GUIDE.md section 7.1). Settings that define a review (topic, people mode, sampling, and each stage's model once that stage has started) are fixed after the start; an edit pauses the run until it is set back or accepted with `sragent accept-config` (configs/README.md, rule 6).

From the command line (the benchmark):

```
sragent run --config configs/benchmark/emicizumab_pk/gemini_agentonly.yaml --plan-only
# edit runs/benchmark/emicizumab_pk/gemini_agentonly/protocol.yaml if needed
sragent run --config configs/benchmark/emicizumab_pk/gemini_agentonly.yaml
sragent status --config configs/benchmark/emicizumab_pk/gemini_agentonly.yaml
sragent eval --config configs/benchmark/emicizumab_pk/gemini_agentonly.yaml --suite all     # benchmark topics only
```

Use `--until STAGE` to stop early. Other commands: `runs` (every run file and its state), `accept-config` (continue a review whose fixed settings were edited; recorded), `rescreen` (re-derive decisions after editing the protocol, no LLM calls), `pdfs` (process added full-text PDFs), `sync`, `feedback`, `rerun`, `propagate`, `accept`, `reject`, `revalidate-terms`, `eval-audit`, and `data --synergy KEY`. See GUIDE.md section 7.

Outputs go to the run's folder under `runs/`: `protocol.yaml`, `store.json` and `store_changelog.jsonl` (decisions, evidence, history), `report.md` and `report_evidence.html`, `usage.jsonl` and `cost.json` (API usage and cost), and `fulltext/needed_pdfs.csv` (papers whose full-text review is blocked; put `<rid>.pdf` in `fulltext/`). Caches are in `.cache/`. Outputs, caches, and credentials are ignored by Git.

## Local models

The same pipeline runs on your own vLLM servers.

```
sragent local-endpoints --config configs/benchmark/emicizumab_pk/gemini_agentonly.yaml --probe
```

`local-endpoints` takes any run file. To run on local servers, copy a run file and include `../../models/local.yaml` instead of `gemini.yaml`.

`local-endpoints` only registers servers that are already running and does not start vLLM. Use `--urls` instead of `--nodes` for a server you reach through your own tunnel. Rerun it with the full new list at any time and active runs pick it up. `local_endpoints.yaml` in the project root is the server list; it also shows how to give nodes to roles (`--roles`, `--add`). See GUIDE.md section 4 for the settings.

## Tests

```
python -m pytest -q tests/
```

Tests run offline (network access is blocked) and use no API credits.

## Repository layout

```
sragent/    pipeline, stages, sources, feedback, evaluation, web interface, LLM and local-model clients
configs/    model setups, benchmark and review topics with their run files (see configs/README.md)
data/       bundled SYNERGY records for the example
docs/       screenshots of the web interface
tests/      offline tests
```
