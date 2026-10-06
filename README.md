# SRAgent

SRAgent builds an evidence-linked review from a question and a set of literature records. It screens studies, extracts data, assesses study quality, and writes a cited narrative synthesis. This version adds quality assessment and review reports.

## Setup

From this folder, create a Conda environment and install the project:

```bash
conda env create --prefix "$PWD/.conda" --file environment.yml
conda activate "$PWD/.conda"
python -m pip install -e ".[dev]"
[ -f api_env.yaml ] || cp api_env.example.yaml api_env.yaml
```

Setup is needed only once per folder. If `.conda/conda-meta/history` already
exists and this project is installed there, skip creation and installation.
For each new terminal or when switching version folders, run:

```bash
conda activate "$PWD/.conda"
sragent --help
```

Each folder has its own Python, dependencies, and editable SRAgent installation.
You can run different versions simultaneously in separate terminals without
reinstalling. Changing directories alone does not switch the active environment.
From the project root, `./.conda/bin/sragent` also works without activation.
Run each version from its own folder so its relative configs, caches, and run
outputs stay separate. When copying a project to a new location, exclude `.conda/`
and create a new environment there; Conda environments contain absolute paths.

All examples below use the installed `sragent` command. `python -m sragent.cli`
is another way to invoke the same command-line interface using the active Python
environment. For example, these are equivalent when that environment has this
version installed and you run them from this folder:

```bash
sragent run --config configs/emicizumab_local.yaml
python -m sragent.cli run --config configs/emicizumab_local.yaml
```

The same equivalence applies to every subcommand, including `local-endpoints`;
it has nothing to do with choosing a local or cloud model.

For cloud models, fill in `api_env.yaml`, or set `OPENAI_API_KEY` / `GEMINI_API_KEY`. Set model IDs and token prices in `configs/backend_api.yaml` for your account. LLM calls use OpenAI, Gemini, or a separately hosted vLLM server. The SRAgent client needs no GPU. Local runs need no cloud API key; see [Local models](#local-models).

If `sragent` reports an unexpected `invalid choice`, first activate this
folder's environment and check its installation:

```bash
conda activate "$PWD/.conda"
python -m pip show sragent  # "Editable project location" should be this folder.
sragent --help
```

If this environment was accidentally pointed at another project, repair it once
with `python -m pip install --no-deps --no-build-isolation -e .` after activation.
Normal switching between folders does not require reinstalling.

## Inputs


| Input                                | Where to set it                                                                                                                                               |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Review question and eligibility text | `configs/topics/emicizumab_pk.yaml`: `topic.question` and `topic.seed_eligibility`. Add `extraction_fields` and `rob_domains`.                                                  |
| Record source                        | The same topic file:`topic.records.source` accepts `synergy`, `pubmed`, or `file`. The example uses the bundled `data/donners_2021_records.jsonl` benchmark.  |
| Imported records                     | For`source: file`, set `topic.records.file` to a JSONL file. Each line should contain `rid`, `title`, and `abstract`; `pmid`, `doi`, and `year` are optional. |
| Run settings                         | `configs/emicizumab_api.yaml`: `run_dir`, `cache_dir`, `budget_usd`, and `pipeline.max_records`. It includes the topic and backend YAML files.                |
| API credentials                      | `api_env.yaml` or environment variables, loaded by `sragent/config.py`.                                                                                       |
| Paper files                          | `<run_dir>/fulltext/<rid>.pdf` or `<rid>.txt`; text-based PDFs are read by `sragent/pdf.py`.                                                                  |

Run commands from the project root: relative data and output paths use the working directory. A YAML `include` path is relative to its containing config file.

## Pipeline

[sragent/cli.py](sragent/cli.py) handles commands; [sragent/pipeline.py](sragent/pipeline.py) runs these stages in order. Node names below refer to entries in `<run_dir>/store.json`; `rid` is the study record ID.


| Step       | Code                                                                                                                 | Input → output                                                                                          |
| ------------ | ---------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `plan`     | [sragent/stages/planning.py](sragent/stages/planning.py)                                                             | Question and seed eligibility →`protocol.yaml`, `protocol` and `crit:<id>` nodes.                       |
| `search`   | [sragent/stages/search.py](sragent/stages/search.py)                                                                 | Protocol and record source → query/duplicate counts in`search`, deduplicated `rec:<rid>` nodes.         |
| `screen`   | [sragent/stages/screening.py](sragent/stages/screening.py)                                                           | Records and criteria →`screen:<rid>` decisions, reasons, and supporting evidence.                       |
| `fulltext` | [sragent/stages/fulltext.py](sragent/stages/fulltext.py), [sragent/stages/screening.py](sragent/stages/screening.py) | Included/uncertain records and available paper text →`ft:<rid>` text and updated eligibility decisions. |
| `extract`  | [sragent/stages/extraction.py](sragent/stages/extraction.py)                                                         | Included studies and configured fields →`ext:<rid>` values, source spans, and term checks.              |
| `rob` | [sragent/stages/rob.py](sragent/stages/rob.py) | Extractions, source text, and quality domains → `rob:<rid>` judgments and evidence. |
| `synthesize` | [sragent/stages/synthesis.py](sragent/stages/synthesis.py) | Included extractions and quality assessments → `syn:<id>` statements, study citations, and support checks. |
| `report` | [sragent/stages/report.py](sragent/stages/report.py) | Review state → `report.md` and `report_evidence.html`. |

Full-text retrieval uses Europe PMC or supplied files and falls back to the abstract when usable full text is unavailable. Outputs record which source was used.

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
- `fulltext/needed_pdfs.csv`: missing or useful paper files, including their expected filenames.
- `report.md` and `report_evidence.html`: the review narrative and source-linked report.

Reusable LLM and literature-response caches are stored in `.cache/`. Run outputs, caches, and credentials are ignored by Git.

Add paper files before the first `fulltext` stage. These stages reuse existing full-text nodes; use a new `--run-dir` to repeat the pipeline with different source files.

## Local models

Cloud and local models use the same pipeline commands. Select the backend with
`--config`:


| Backend          | Run command                                          | Connection settings                                                                                     |
| ------------------ | ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| OpenAI / Gemini  | `sragent run --config configs/emicizumab_api.yaml`   | Cloud model IDs and credentials in`configs/backend_api.yaml` and `api_env.yaml`.                        |
| Your vLLM server | `sragent run --config configs/emicizumab_local.yaml` | Local settings in`configs/backend_local.yaml`; live server addresses in `configs/local_endpoints.yaml`. |

`sragent local-endpoints` tells SRAgent **where your already-running vLLM servers
are**. It saves the server list; it does not start vLLM, request GPUs, or run a
review. `--probe` additionally connects to each server and lists its served model
IDs without generating text.

A **compute node** is a cluster machine hosting your GPU job. Use its hostname
from the `NODELIST(REASON)` column of `squeue`, such as `a0227`. Names like `NODE1`
and `compute-node-1` in generic examples are placeholders to replace, not literal
hostnames or GPU numbers. Only list nodes on which you have actually started a
vLLM API server; an allocated `bash` job alone does not imply vLLM is running.

For example, **if vLLM is running on both `a0227` and `a0229`**, each listening on
`127.0.0.4:8000`, run the following from this version's project folder:

```bash
# Register the two server hosts and check which models they serve.
sragent local-endpoints --config configs/emicizumab_local.yaml --nodes a0107 a0183 --probe

# Draft the protocol using the local model.
sragent run --config configs/emicizumab_local.yaml --plan-only
# Review/edit runs/emicizumab_local/protocol.yaml before continuing.
sragent run --config configs/emicizumab_local.yaml
sragent status --config configs/emicizumab_local.yaml
```

For one server on `a0227`, use `--nodes a0227`. Two GPUs serving one model on that
node still mean **one** entry. For twelve independently served endpoints, list
all twelve hostnames. For a single distributed server spanning nodes, list only
the node exposing its API. Hostnames above are examples; use your current server
hosts.

SRAgent manages SSH tunnels to `127.0.0.4:8000` on the listed hosts. Noninteractive
SSH must work from the machine running SRAgent, for example `ssh a0227 true`.
For a server reachable directly or through your own tunnel, use `--urls` instead:

```bash
# Example: vLLM is reachable at this URL from the SRAgent machine.
sragent local-endpoints --config configs/emicizumab_local.yaml --urls http://127.0.0.1:18001/v1 --probe
```

While a review runs, open another terminal in the same project folder and rerun
`local-endpoints` with the **complete new list** of server hosts or URLs. This
replaces the old list. Active runs reload it automatically, use available servers,
and wait if all are temporarily unavailable. You do not need to restart the review to apply an endpoint update. Model IDs are discovered automatically.

The other commands documented above use this same `--config` option: substitute
`configs/emicizumab_local.yaml` to work on the local review. Each version supports
only the commands and stages listed in its README. The supplied cloud and local
configs use separate output folders, `runs/emicizumab_api/` and
`runs/emicizumab_local/`; switching config files does not migrate a review.

See [LOCAL_MODELS.txt](LOCAL_MODELS.txt) for live-update examples, authentication,
model selection, concurrency, and mixed-provider roles.

## Tests

```bash
python -m pytest -q tests/
```

`tests/test_offline.py` checks the implemented workflow; `tests/test_api.py` checks configuration and API request construction with a stub client. `tests/test_local.py` covers endpoint changes, failover, caching and tunnel management. Tests block network access and do not spend API credits.
