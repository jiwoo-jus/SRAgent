"""Configuration loading.

A run is configured by one YAML file in configs/<benchmark|review>/<topic>/ that includes the folder's
topic.yaml and chooses its models: `models.<stage>.<role>` (writer, verifier) over `models.default`, and
`eval.models` for the evaluation roles; `llm_defaults` holds the request settings every model shares. Facts about
each model (provider, price) are written once, in the model catalog configs/models.yaml, and filled in by model name.
LLM credentials are never stored in configs; they are read from an `api_env.yaml` file (or from environment
variables) whose path is given in `llm_defaults.api_env`.

Paths to other config files (include, api_env, local.endpoints_file, models_file) are relative to the
file they are written in. Data paths (run_dir, cache_dir, pipeline.fulltext_dir, and the topic's
records.data_dir, records.file, protocol_file) are relative to the project root, where `sragent` is run.

The outputs of a run file go to the same path under runs/: configs/<kind>/<topic>/<run>.yaml writes
runs/<kind>/<topic>/<run>/. `run_dir` is only written to override that.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

from . import stagespec   # the one list of stages

DEFAULTS: dict[str, Any] = {
    "run_dir": None,              # None: runs/<the run file's path under configs/, without .yaml>
    "cache_dir": ".cache",
    # Cost limits of this run folder in USD (required; budget_limits): `total_usd`, one hard ceiling, and a limit for
    # every stage that calls a model (MODEL_STAGES), a number or `unlimited`. A call runs only when both have room.
    "budget": None,
    "contact_email": "",          # sent to NCBI, Unpaywall and OpenAlex (Unpaywall requires it)
    "ncbi_api_key": "",           # optional, raises E-utilities rate limit
    "models_file": "configs/models.yaml",   # model catalog: provider and price per model name (project root)
    "llm_defaults": {             # request settings every model uses; a model cell can override any of them
        "api_env": "api_env.yaml",
        "temperature": 0.0,
        "max_tokens": 8192,
        "timeout": 600,
        "max_retries": 4,
        "json_mode": True,        # use response_format=json_object when available
        "concurrency": 4,
    },
    # The model of each stage and role (04 §6): models.<stage>.<role>, else models.default.<role>. Roles: writer
    # (produces the stage's output) and verifier (the second reading; unset = models.default.writer). A cell is a model
    # name or {model, reasoning_effort, provider, pricing, base_url, extra_body, and any llm_defaults key}; provider and price come
    # from the model catalog when not written. Stages: protocol search screen eligibility snowball extract rob synthesis,
    # and feedback (your feedback and the revisions it starts). A stage's cells are fixed once it has started.
    "models": {"default": {"writer": "gpt-6.1-sol"}},
    "local": {
        "endpoints_file": "local_endpoints.yaml",   # live vLLM endpoint list (project root)
        "poll_interval": 2,
        "retry_interval": 5,
        "probe_timeout": 5,
        "wait_timeout": None,   # wait for updated/recovered endpoints until cancelled
    },
    "eval": {                         # `sragent eval` only
        "models": {},                 # judge (LLM-as-judge), judge2 (inter-judge agreement), reviewer (simulated person,
                                      #   Claim 1); a cell like those of `models`; unset = models.default.writer
        "budget_usd": 8.0,            # per evaluation run (each run has its own usage.jsonl)
        "seed": 2026,
        "grounding_max_studies": 15,  # null = all supplied studies
        "claim1_sets": 2,
        "claim1_repeats": 1,
        "claim1_source_chars": 5000,
        "claim1_total_chars": 450000,   # cap on the whole materials bundle (~115k tokens); the per-study cap shrinks (for both conditions) to fit
        "claim1_genuine_checks": 5,
        "claim2_cases_per_type": 1,
        "claim2_repeats": 1,
        "claim2_build_attempts": 3,
        "judge_agreement_items": 25,
        "human_audit_items": 15,
    },
    "pipeline": {
        "human_mode": "human_in_the_loop",   # human_in_the_loop | agent_only (see provenance.py)
        "max_records": None,          # cap #records screened (cost control)
        "screen_sample_seed": 13,
        "fulltext": True,             # try Europe PMC open-access full text
        "fulltext_dir": None,         # where supplied PDFs are read from; None = <run_dir>/fulltext
        "fulltext_unclear": "await",  # still unclear after the full text: await (a person decides) | include | exclude
        "reassess_excluded": "all",   # after a criterion changes, an excluded record is read again: all = always, so every record
                                      #   has an answer on every criterion; needed = only if an answer could lift its exclusion
        "fulltext_screen_chars": 15000,   # prompt budget for full-text screening (section-prioritised, see evidence.packed)
        "context_chars": 60000,       # prompt budget for extraction / RoB; longer papers are read in parts
        "max_stored_chars": 400000,   # the whole text is stored; prompts select from it
        "open_access_lookup": True,   # Unpaywall (needs contact_email) / OpenAlex legal OA copies
        "keep_fetched_files": True,   # keep what the agent fetched in fulltext/auto/: the text (.txt) and the PMC PDF when available
        "wait_for_pdfs": False,       # stop before the synthesis while requested full texts are missing (real reviews)
        "confirm_search": False,      # stop before the search until the review team has confirmed the query (To-do list)
        "verify_rob": "off",          # risk-of-bias drafts with a reason for a second look (the agent's own doubt, a judgment
                                      #   without a cited sentence, an overall milder than a domain): off -> to a person;
                                      #   flagged -> the verifier settles them, a person sees only what it cannot settle
        "verify_screening": "off",    # a second agent (the stage's verifier) reads screening decisions: off | pending | conflict | full_text | all
                                      #   pending: only records undecided after their full text (it decides, or leaves it to a person)
                                      #   conflict: also full-text decisions the screener's overall answer disagrees with
                                      #   full_text: every full-text decision; all: also title/abstract exclusions (disputes go to a person)
        "batch_stages": [],           # screen, eligibility: those stages' model calls go through Anthropic's Message Batches
                                      #   (half price; usually within the hour, at most 24 hours; other providers: normal calls)
        "verify_screening_blind": False,   # the screening verifier reads the record and the criteria without the screener's
                                      #   answer; the two readings are compared afterwards (an independent second reading)
        "pdf_parser": "auto",         # auto | azure_cu | pdftotext
        "azure_cu_analyzer": "prebuilt-documentSearch",   # or prebuilt-layout (no figure descriptions, cheaper)
        "vision_pages": False,        # without Azure: transcribe table/figure pages with the (multimodal) agent model
        "vision_max_pages": 6,
        "citation_chasing": True,     # backward + forward snowballing from included studies (OpenAlex)
        "citation_chasing_rounds": 1,
        "citation_chasing_max_citing": 400,
        "validate_terms": True,
        "verify_synthesis": True,
        "repair_synthesis": True,     # one repair round for statements the self-check faulted (narrow or retire)
        "skip_stages": [],            # stages to leave out, e.g. [rob]; plan, screen and report cannot be skipped
    },
    # How the update engine treats each kind of output when it is out of date (engine.py):
    #   auto     bring it up to date and apply (the agent's own work)
    #   propose  bring it up to date in a sandbox and propose the result for a person to accept
    #   manual   only list it; a person decides what to do
    "update_policy": {"screen": "auto", "extraction": "auto", "rob": "auto", "synthesis": "auto"},
    "feedback": {
        "verify_feedback": True,      # check user feedback against evidence first
        "propagate": True,            # dependency-aware (True) vs local-edit (False)
        "auto_accept": False,         # apply proposals without human confirmation
        "post_check": True,           # re-check every revised output, one repair round
        "max_repair_rounds": 1,
        "global_audit": True,         # review-wide consistency audit after dependency-aware revisions
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _resolve_paths(user: dict, base: Path) -> None:
    """Make the file paths written in one config file absolute, relative to that file."""
    def fix(d, key):
        if isinstance(d, dict) and d.get(key):
            f = Path(d[key]).expanduser()
            d[key] = str(f if f.is_absolute() else (base / f).resolve())
    fix(user, "models_file")
    fix(user.get("local"), "endpoints_file")
    fix(user.get("llm_defaults"), "api_env")
    for cells in list((user.get("models") or {}).values()) + [((user.get("eval") or {}).get("models") or {})]:
        for cell in (cells or {}).values() if isinstance(cells, dict) else ():
            fix(cell, "api_env")


def _load_layer(p: Path, depth: int = 0) -> dict:
    """Load a YAML file, recursively resolving `include:` (later layers override earlier)."""
    if depth > 8:
        raise ValueError("include nesting too deep")
    with open(p) as f:
        user = yaml.safe_load(f) or {}
    _resolve_paths(user, p.parent)
    merged: dict = {}
    for inc in user.pop("include", []) or []:
        merged = deep_merge(merged, _load_layer((p.parent / inc).resolve(), depth + 1))
    return deep_merge(merged, user)


def load_config(path: str | os.PathLike | None, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        p = Path(path)
        cfg = deep_merge(cfg, _load_layer(p))
        cfg["_config_dir"] = str(p.parent.resolve())
    if overrides:
        cfg = deep_merge(cfg, overrides)
    cfg["_catalog"] = load_catalog(cfg.get("models_file"))
    if cfg["run_dir"] is None:
        cfg["run_dir"] = default_run_dir(path)
    p = cfg["pipeline"]
    from .rows import check_field, is_rows
    bad_rows = [e for f in (cfg.get("topic") or {}).get("extraction_fields") or [] if isinstance(f, dict) and is_rows(f)
                for e in check_field(f)]
    if bad_rows:
        raise ValueError("; ".join(bad_rows))
    if (cfg.get("topic") or {}).get("meta_analysis"):
        from .meta import plan_of
        plan_of(cfg["topic"])          # the analysis plan names real columns
    if p.get("verify_screening") not in (None, "off", "pending", "conflict", "full_text", "all"):
        raise ValueError(f"pipeline.verify_screening: {p['verify_screening']!r}; use off, pending, conflict, full_text or all")
    if p["human_mode"] == "agent_only" and p["fulltext_unclear"] == "await":
        raise ValueError("pipeline.fulltext_unclear: await leaves records for a person to decide; "
                         "with human_mode: agent_only set it to include or exclude")
    if p["reassess_excluded"] not in ("needed", "all"):
        raise ValueError(f"pipeline.reassess_excluded: {p['reassess_excluded']!r}; use needed or all")
    if "budget_usd" in cfg:
        raise ValueError("budget_usd was replaced by budget: a total and a limit per stage, e.g. "
                         "budget: {total_usd: 30, stages: {protocol: unlimited, screen: 5, ...}} (configs/README.md)")
    unknown = set(cfg) - set(DEFAULTS) - {"topic", "_config_dir", "_catalog", "job_model"}
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))} (configs/README.md lists the run file's sections)")
    bad = set(cfg["llm_defaults"]) - set(DEFAULTS["llm_defaults"])
    if bad:
        raise ValueError(f"llm_defaults: unknown keys {', '.join(sorted(bad))}; models are chosen under models: "
                         "(models.default.writer, models.<stage>.<role>)")
    _check_models(cfg)
    if cfg.get("budget") is not None:   # a written budget: is checked here; LLMHub (any model call) requires one
        budget_limits(cfg)
    for k, v in (cfg.get("update_policy") or {}).items():
        if k not in ("screen", "extraction", "rob", "synthesis") or v not in ("auto", "propose", "manual"):
            raise ValueError(f"update_policy.{k}: {v!r}; keys are screen, extraction, rob, synthesis and values auto, propose or manual")
    p["skip_stages"] = [stagespec.run_until(x) for x in (p.get("skip_stages") or [])]
    bad = set(p.get("batch_stages") or []) - set(BATCH_STAGES)
    if bad:
        raise ValueError(f"pipeline.batch_stages: {', '.join(sorted(bad))}; only {', '.join(BATCH_STAGES)} can run as batches")
    bad = [x for x in p["skip_stages"] if x in ("protocol", "screen", "report")]
    if bad:
        raise ValueError(f"pipeline.skip_stages: {', '.join(bad)} cannot be skipped")
    if p["max_records"] and not is_benchmark(cfg):
        raise ValueError("pipeline.max_records drops records at random; it is only for benchmark topics "
                         "(records.source: synergy), where the known answers are always kept")
    return cfg


def default_run_dir(path: str | os.PathLike | None) -> str:
    """runs/<path of the run file under configs/, without .yaml>; runs/<file stem> outside a configs/ folder."""
    if not path:
        return "runs/default"
    parts = Path(path).resolve().with_suffix("").parts
    if "configs" in parts:
        parts = parts[len(parts) - parts[::-1].index("configs"):]
    else:
        parts = parts[-1:]
    return "runs/" + "/".join(parts)


def is_benchmark(cfg: dict) -> bool:
    """A benchmark topic has known answers (gold labels), so runs can be sampled and evaluated."""
    return ((cfg.get("topic") or {}).get("records") or {}).get("source") == "synergy"


# ---------------------------------------------------------------- model catalog

CATALOG_FACTS = ("provider", "pricing", "base_url")   # facts about a model, filled in from configs/models.yaml
PROVIDERS = ("openai", "gemini", "anthropic", "local", "mock")


def load_catalog(path: str | os.PathLike | None) -> dict:
    """{model name: {provider, pricing, base_url}} from the catalog file ({} when there is none)."""
    if not path:
        return {}
    p = Path(path).expanduser()
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        raise ValueError(f"{p}: write the models under `models:`, e.g. models: {{claude-haiku-5-5: {{provider: anthropic, pricing: ...}}}}")
    out = {}
    for name, e in models.items():
        if not isinstance(e, dict) or e.get("provider") not in PROVIDERS:
            raise ValueError(f"{p}: models.{name}.provider must be one of {', '.join(PROVIDERS[:-1])}")
        bad = set(e) - set(CATALOG_FACTS) - {"note"}
        if bad:
            raise ValueError(f"{p}: models.{name}: only {', '.join(CATALOG_FACTS)} and note belong in the catalog "
                             f"(choices such as {', '.join(sorted(bad))} go in the run file)")
        pr = e.get("pricing")
        if e["provider"] != "local" and not (isinstance(pr, dict) and "input_per_m" in pr and "output_per_m" in pr):
            raise ValueError(f"{p}: models.{name}.pricing needs input_per_m and output_per_m (USD per 1M tokens)")
        out[str(name)] = {k: copy.deepcopy(e[k]) for k in CATALOG_FACTS if k in e}
    return out


def _with_model(cfg: dict, base: dict, ov: dict) -> dict:
    """base overridden by ov; then the catalog's facts about the resulting model, except those ov writes."""
    sc = _over(base, ov)
    facts = (cfg.get("_catalog") or {}).get(sc.get("model")) or {}
    for k, v in facts.items():
        if k not in ov:
            sc[k] = copy.deepcopy(v)
    if base.get("provider") and sc.get("provider") != base.get("provider"):
        # another provider than the defaults: provider-specific request options (vLLM chat_template_kwargs, json_mode)
        # are not inherited; an API provider would reject them
        for k in ("extra_body", "json_mode"):
            sc[k] = copy.deepcopy(ov[k]) if k in ov else copy.deepcopy(DEFAULTS["llm_defaults"].get(k, {}))
    return sc


# ---------------------------------------------------------------- models per stage and role

MODEL_STAGES = stagespec.model_slots()        # the stages with model cells (models.<stage>.<role>), then feedback
STAGE_LABELS = {k: stagespec.label(k) for k in MODEL_STAGES}
EVAL_ROLES = ("judge", "judge2", "reviewer")
BATCH_STAGES = ("screen", "eligibility")      # the stages whose bulk calls can go through a batch API
UNLIMITED = "unlimited"


def budget_limits(cfg: dict) -> dict:
    """The run file's `budget:` as {"total_usd": float, "stages": {stage: float, or None for unlimited}}; a missing
    section, a missing or unknown stage, or a value that is not a number or `unlimited` is an error."""
    b = cfg.get("budget")
    example = ("budget: {total_usd: 30, stages: {" + ", ".join(f"{s}: unlimited" for s in MODEL_STAGES) + "}}")
    if not isinstance(b, dict):
        raise ValueError(f"budget is required: the total and a limit for each stage that calls a model, e.g. {example}")
    bad = set(b) - {"total_usd", "stages"}
    if bad:
        raise ValueError(f"budget: unknown keys {', '.join(sorted(bad))}; write total_usd and stages")
    total = b.get("total_usd")
    if isinstance(total, bool) or not isinstance(total, (int, float)) or total < 0:
        raise ValueError(f"budget.total_usd: {total!r}; a number of US dollars (the hard ceiling of this run folder)")
    stages = b.get("stages")
    if not isinstance(stages, dict):
        raise ValueError(f"budget.stages: a limit for each stage that calls a model, e.g. {example}")
    missing = [s for s in MODEL_STAGES if s not in stages]
    unknown = sorted(set(stages) - set(MODEL_STAGES))
    if missing or unknown:
        raise ValueError("budget.stages: " + "; ".join(
            ([f"missing {', '.join(missing)}"] if missing else []) + ([f"unknown {', '.join(unknown)}"] if unknown else []))
            + f". List every stage that calls a model ({', '.join(MODEL_STAGES)}) with a number or {UNLIMITED}")
    out = {}
    for s in MODEL_STAGES:
        v = stages[s]
        if v == UNLIMITED:
            out[s] = None
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[s] = float(v)
        else:
            raise ValueError(f"budget.stages.{s}: {v!r}; a number of US dollars or {UNLIMITED}")
    return {"total_usd": float(total), "stages": out}


def unlimited_budget(total_usd: float) -> dict:
    """A `budget:` section with a total and no stage limits (evaluations, tests)."""
    return {"total_usd": total_usd, "stages": {s: UNLIMITED for s in MODEL_STAGES}}
EFFORTS = {"anthropic": ("low", "medium", "high", "xhigh", "max"), "openai": ("low", "medium", "high"),
           "gemini": ("low", "medium", "high")}
_FACTS = ("model", "provider", "pricing", "base_url", "reasoning_effort", "fallbacks",
          "extra_body")   # provider request parameters (e.g. vLLM chat_template_kwargs): a cell's own, not shared


def _cell(v) -> dict:
    """A model cell as a mapping: a bare model name is {model: name}."""
    return {"model": v} if isinstance(v, str) else dict(v or {})


def _layers(cfg: dict, stage: str | None, role: str) -> dict:
    """llm_defaults, then models.default.<role>, then models.<stage>.<role>."""
    m = cfg["models"]
    c = _with_model(cfg, cfg["llm_defaults"], _cell((m.get("default") or {}).get(role)))
    return _with_model(cfg, c, _cell((m.get(stage) or {}).get(role))) if stage else c


def model_for(cfg: dict, stage: str | None, role: str = "writer") -> dict:
    """The settings of the model for one stage and role (stage None: models.default). A verifier with no cell in
    models.default starts from models.default.writer, not from the stage's writer: a stage written by a stronger
    model is still read again by another one. An evaluation role (judge, judge2, reviewer) is eval.models.<role> over
    models.default.writer. job_model, a model a person picked for one job, replaces every writer of that job (never
    a verifier)."""
    if role in EVAL_ROLES:
        return _with_model(cfg, _layers(cfg, None, "writer"), _cell((cfg["eval"].get("models") or {}).get(role)))
    if role == "verifier":
        base = _layers(cfg, None, "verifier" if (cfg["models"].get("default") or {}).get("verifier") else "writer")
        return _with_model(cfg, base, _cell((cfg["models"].get(stage) or {}).get("verifier"))) if stage else base
    c = _layers(cfg, stage, "writer")
    return _with_model(cfg, c, _cell(cfg["job_model"])) if cfg.get("job_model") else c


def _check_models(cfg: dict) -> None:
    """models: known stages and roles, cells that resolve to a known provider and price, efforts the provider offers."""
    m = cfg.get("models")
    if not isinstance(m, dict) or not (m.get("default") or {}).get("writer"):
        raise ValueError("models.default.writer is required: the model of every stage and role not written otherwise")
    keys = set(DEFAULTS["llm_defaults"]) | set(_FACTS)
    cells = [(f"eval.models.{r}", cfg["eval"].get("models", {}).get(r), None, r) for r in (cfg["eval"].get("models") or {})]
    for stage, roles in m.items():
        if stage != "default" and stage not in MODEL_STAGES:
            hint = f" ({stagespec.label(stage).lower()} makes no model calls)" if stage in stagespec.BY_KEY else ""
            raise ValueError(f"models.{stage}: unknown stage{hint}; use default or one of {', '.join(MODEL_STAGES)}")
        if not isinstance(roles, dict):
            raise ValueError(f"models.{stage}: write the roles, e.g. {{writer: claude-haiku-5-5, verifier: gpt-6.1-sol}}")
        for role, v in roles.items():
            ok = stagespec.ROLES if stage == "default" else stagespec.roles_of(stage)
            if role not in ok:
                raise ValueError(f"models.{stage}.{role}: unknown role; {stage} has {', '.join(ok)}")
            cells.append((f"models.{stage}.{role}", v, None if stage == "default" else stage, role))
    for where, v, stage, role in cells:
        if v is not None and not isinstance(v, (str, dict)):
            raise ValueError(f"{where}: a model name or a mapping such as {{model: claude-sonnet-5-5, reasoning_effort: high}}")
        bad = set(_cell(v)) - keys
        if bad:
            raise ValueError(f"{where}: unknown keys {', '.join(sorted(bad))}")
        if role not in EVAL_ROLES and role not in stagespec.ROLES:
            raise ValueError(f"eval.models.{role}: unknown role; use {', '.join(EVAL_ROLES)}")
        c, own = model_for(cfg, stage, role), _cell(v)
        if not c.get("provider") or (own.get("model") and own["model"] not in (cfg.get("_catalog") or {})
                                     and not own.get("pricing") and c["provider"] not in ("local", "mock")):
            raise ValueError(f"{where}: {c.get('model')} is not in the model catalog ({cfg.get('models_file')}); add it "
                             "there or write provider and pricing: {input_per_m: ..., output_per_m: ...}")
        eff, offered = c.get("reasoning_effort"), EFFORTS.get(c.get("provider"))
        if eff is not None and offered and eff not in offered:
            raise ValueError(f"{where}.reasoning_effort: {eff!r} is not offered for {c['provider']} "
                             f"(use {', '.join(offered)} or null for the model default)")


def _over(agent: dict, ov: dict) -> dict:
    """A cell over the settings below it; a cell's own price replaces the one below (never merged into it)."""
    sc = deep_merge(agent, ov)
    if ov.get("pricing"):
        sc["pricing"] = copy.deepcopy(ov["pricing"])
    return sc


# ---------------------------------------------------------------- secrets

def _tolerant_parse(text: str) -> dict:
    """Parse api_env.yaml even if some lines are `KEY= value` (not valid YAML)."""
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items()}
    except yaml.YAMLError:
        pass
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::|=)\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def load_secrets(api_env: str | None, config_dir: str | None = None) -> dict:
    secrets: dict[str, str] = {}
    if api_env:
        cands = []
        if config_dir:
            cands.append(Path(config_dir) / api_env)
        cands += [Path.cwd() / api_env, Path(api_env).expanduser()]
        for c in cands:
            if c.exists():
                secrets = _tolerant_parse(c.read_text())
                break
    env_map = {
        "openai_api_key": "OPENAI_API_KEY",
        "gemini_api_key": "GEMINI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "local_api_key": "LOCAL_API_KEY",
    }
    for k, env in env_map.items():
        if os.environ.get(env):
            secrets[k] = os.environ[env]
    return secrets
