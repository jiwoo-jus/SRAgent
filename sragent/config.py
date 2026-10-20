"""Configuration loading.

A run is configured by one YAML file in configs/<benchmark|review>/<topic>/ that includes the folder's
topic.yaml and chooses its models (llm_defaults, roles, stage_models). Facts about each model (provider,
price) are written once, in the model catalog configs/models.yaml, and filled in by model name.
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

DEFAULTS: dict[str, Any] = {
    "run_dir": None,              # None: runs/<the run file's path under configs/, without .yaml>
    "cache_dir": ".cache",
    "budget_usd": 5.0,
    "contact_email": "",          # sent to NCBI, Unpaywall and OpenAlex (Unpaywall requires it)
    "ncbi_api_key": "",           # optional, raises E-utilities rate limit
    "models_file": "configs/models.yaml",   # model catalog: provider and price per model name (project root)
    "llm_defaults": {
        "provider": "openai",     # openai | gemini | anthropic | local | mock (tests)
        "model": "gpt-6.1-sol",
        "api_env": "api_env.yaml",
        "base_url": None,         # optional API endpoint override (local servers are listed in local.endpoints_file)
        "temperature": 0.0,
        "max_tokens": 8192,
        "reasoning_effort": None, # openai/gemini: low | medium | high; anthropic: low | medium | high | xhigh | max
        "extra_body": {},         # optional provider request parameters (e.g. Qwen chat_template_kwargs)
        "timeout": 600,
        "max_retries": 4,
        "json_mode": True,        # use response_format=json_object when available
        "pricing": {"input_per_m": 0.0, "output_per_m": 0.0},
        "concurrency": 4,
    },
    "local": {
        "endpoints_file": "local_endpoints.yaml",   # live vLLM endpoint list (project root)
        "poll_interval": 2,
        "retry_interval": 5,
        "probe_timeout": 5,
        "wait_timeout": None,   # wait for updated/recovered endpoints until cancelled
    },
    # model and effort per pipeline stage, over the agent role: {stage: {model, reasoning_effort, provider, ...}}.
    # Stages: plan search screen fulltext snowball extract rob synthesize, and feedback (feedback handling,
    # revisions, sync). Unset = the agent role. Provider and price come from the model catalog when not written.
    # A stage's model is fixed once the stage has started (config_lock.py).
    "stage_models": {},
    # role-specific overrides of llm_defaults
    "roles": {
        "agent": {},      # does the review work
        "judge": {},      # LLM-as-judge for evaluation
        "reviewer": {},   # simulated human reviewer for Claim 1
        "judge2": {},     # second judge for inter-judge agreement
        "verifier": {},   # optional: checks feedback against the evidence; empty = the agent does it
    },
    "eval": {                         # `sragent eval` only
        "budget_usd": 8.0,            # per evaluation run (each run has its own usage.jsonl)
        "seed": 2026,
        "grounding_max_studies": 15,  # null = all supplied studies
        "claim1_sets": 2,
        "claim1_repeats": 1,
        "claim1_source_chars": 5000,
        "claim1_total_chars": 300000,   # cap on the whole materials bundle; the per-study cap shrinks (for both conditions) to fit
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
        "fulltext_screen_chars": 15000,   # prompt budget for full-text screening (section-prioritised, see evidence.packed)
        "context_chars": 60000,       # prompt budget for extraction / RoB; longer papers are read in parts
        "max_stored_chars": 400000,   # the whole text is stored; prompts select from it
        "open_access_lookup": True,   # Unpaywall (needs contact_email) / OpenAlex legal OA copies
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
    for role in (user.get("roles") or {}).values():
        fix(role, "api_env")


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
    if p["human_mode"] == "agent_only" and p["fulltext_unclear"] == "await":
        raise ValueError("pipeline.fulltext_unclear: await leaves records for a person to decide; "
                         "with human_mode: agent_only set it to include or exclude")
    _resolve_stage_models(cfg)
    for k, v in (cfg.get("update_policy") or {}).items():
        if k not in ("screen", "extraction", "rob", "synthesis") or v not in ("auto", "propose", "manual"):
            raise ValueError(f"update_policy.{k}: {v!r}; keys are screen, extraction, rob, synthesis and values auto, propose or manual")
    bad = [x for x in (p.get("skip_stages") or []) if x in ("plan", "screen", "report")]
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


def role_cfg(cfg: dict, role: str) -> dict:
    """LLM settings for a role = llm_defaults overridden by roles[role]; the provider and price of its model
    come from the model catalog unless the role writes its own."""
    return _with_model(cfg, cfg["llm_defaults"], cfg.get("roles", {}).get(role, {}) or {})


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
    sc = _over_agent(base, ov)
    facts = (cfg.get("_catalog") or {}).get(sc.get("model")) or {}
    for k, v in facts.items():
        if k not in ov:
            sc[k] = copy.deepcopy(v)
    return sc


# ---------------------------------------------------------------- per-stage models

LLM_STAGES = ("plan", "search", "screen", "fulltext", "snowball", "extract", "rob", "synthesize", "feedback")
STAGE_LABELS = {"plan": "Protocol", "search": "Search", "screen": "Title/abstract screening",
                "fulltext": "Full-text screening", "snowball": "Citation chasing", "extract": "Data extraction",
                "rob": "Risk of bias", "synthesize": "Synthesis", "feedback": "Feedback, revisions and updates"}
EFFORTS = {"anthropic": ("low", "medium", "high", "xhigh", "max"), "openai": ("low", "medium", "high"),
           "gemini": ("low", "medium", "high")}
_STAGE_KEYS = set(DEFAULTS["llm_defaults"]) | {"fallbacks"}


def stage_of_tag(tag: str) -> str:
    """The pipeline stage an LLM call belongs to, from its tag (used outside a pipeline stage)."""
    t = tag or ""
    for prefix, stage in (("screen_title_abstract", "screen"), ("screen_full_text", "fulltext"), ("extract", "extract"),
                          ("rob", "rob"), ("synthesis", "synthesize"), ("plan", "plan"), ("search", "search")):
        if t == prefix or t.startswith(prefix + "_"):
            return stage
    return "feedback"


def _resolve_stage_models(cfg: dict) -> None:
    """Check stage_models (each stage's provider and price come from the model catalog when not written)."""
    sm = cfg.get("stage_models") or {}
    if not isinstance(sm, dict):
        raise ValueError("stage_models must map a stage to its settings, e.g. extract: {model: ..., reasoning_effort: medium}")
    agent = role_cfg(cfg, "agent")
    catalog = cfg.get("_catalog") or {}
    for stage, ov in list(sm.items()):
        if stage not in LLM_STAGES:
            hint = " (the report makes no LLM calls)" if stage == "report" else ""
            raise ValueError(f"stage_models.{stage}: unknown stage{hint}; use one of {', '.join(LLM_STAGES)}")
        if ov is None:
            sm[stage] = {}
            continue
        if not isinstance(ov, dict):
            raise ValueError(f"stage_models.{stage}: write a mapping, e.g. {{model: claude-sonnet-5-5, reasoning_effort: high}}")
        bad = set(ov) - _STAGE_KEYS
        if bad:
            raise ValueError(f"stage_models.{stage}: unknown keys {', '.join(sorted(bad))}")
        ov = dict(ov)
        if ov.get("model") and ov["model"] != agent.get("model") and ov["model"] not in catalog and not ov.get("pricing") \
                and ov.get("provider", agent.get("provider")) != "local":
            raise ValueError(f"stage_models.{stage}: {ov['model']} is not in the model catalog "
                             f"({cfg.get('models_file')}); add it there or write pricing: {{input_per_m: ..., output_per_m: ...}}")
        sc = _with_model(cfg, agent, ov)
        eff = sc.get("reasoning_effort")
        ok = EFFORTS.get(sc.get("provider"))
        if eff is not None and ok and eff not in ok:
            raise ValueError(f"stage_models.{stage}.reasoning_effort: {eff!r} is not offered for {sc['provider']} "
                             f"(use {', '.join(ok)} or null for the model default)")
        sm[stage] = ov
    cfg["stage_models"] = sm


def stage_cfg(cfg: dict, stage: str | None) -> dict:
    """LLM settings for the agent in one pipeline stage = the agent role overridden by stage_models[stage]."""
    agent = role_cfg(cfg, "agent")
    ov = (cfg.get("stage_models") or {}).get(stage or "") or {}
    return _with_model(cfg, agent, ov) if ov else agent


def _over_agent(agent: dict, ov: dict) -> dict:
    """A stage's settings over the agent's; a stage's own price replaces the agent's (never merged into it)."""
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
