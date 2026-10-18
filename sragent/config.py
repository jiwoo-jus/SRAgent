"""Configuration loading.

A run is configured by one YAML file in configs/<benchmark|review>/<topic>/ that includes the folder's
topic.yaml and a model setup (configs/models/). LLM credentials are never stored in configs; they are read from an
`api_env.yaml` file (or from environment variables) whose path is given in `llm_defaults.api_env`.

Paths to other config files (include, api_env, local.endpoints_file, models_dir) are relative to the
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
    "models_dir": "configs/models",   # model setups; the web interface offers the models found in them
    "llm_defaults": {
        "provider": "openai",     # openai | gemini | local | mock (tests)
        "model": "gpt-6.1-sol",
        "api_env": "api_env.yaml",
        "base_url": None,         # optional API endpoint override (local servers are listed in local.endpoints_file)
        "temperature": 0.0,
        "max_tokens": 8192,
        "reasoning_effort": None, # openai reasoning models: low | medium | high
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
    # role-specific overrides of llm_defaults
    "roles": {
        "agent": {},      # does the review work
        "judge": {},      # LLM-as-judge for evaluation
        "reviewer": {},   # simulated human reviewer for Claim 1
        "judge2": {},     # second judge for inter-judge agreement
    },
    "eval": {                         # `sragent eval` only
        "budget_usd": 8.0,            # per evaluation run (each run has its own usage.jsonl)
        "seed": 2026,
        "grounding_max_studies": 15,  # null = all supplied studies
        "claim1_sets": 2,
        "claim1_repeats": 1,
        "claim1_source_chars": 5000,
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
    },
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
    fix(user, "models_dir")
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
    if cfg["run_dir"] is None:
        cfg["run_dir"] = default_run_dir(path)
    p = cfg["pipeline"]
    if p["human_mode"] == "agent_only" and p["fulltext_unclear"] == "await":
        raise ValueError("pipeline.fulltext_unclear: await leaves records for a person to decide; "
                         "with human_mode: agent_only set it to include or exclude")
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
    """LLM settings for a role = llm_defaults overridden by roles[role]."""
    return deep_merge(cfg["llm_defaults"], cfg.get("roles", {}).get(role, {}) or {})


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
        "local_api_key": "LOCAL_API_KEY",
    }
    for k, env in env_map.items():
        if os.environ.get(env):
            secrets[k] = os.environ[env]
    return secrets
