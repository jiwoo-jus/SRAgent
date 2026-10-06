"""Configuration loading.

A run is configured by one YAML file (see configs/*.yaml). LLM credentials are
never stored in configs; they are read from an `api_env.yaml` file (or from
environment variables) whose path is given in `llm_defaults.api_env`.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "run_dir": "runs/default",
    "cache_dir": ".cache",
    "budget_usd": 5.0,
    "contact_email": "",          # sent to NCBI / Crossref as a courtesy (optional)
    "ncbi_api_key": "",           # optional, raises E-utilities rate limit
    "llm_defaults": {
        "provider": "openai",     # openai | gemini | local | mock (tests)
        "model": "gpt-6.1-sol",
        "api_env": "api_env.yaml",
        "base_url": None,         # optional API endpoint override
        "temperature": 0.0,
        "max_tokens": 8192,
        "reasoning_effort": None, # openai reasoning models: low | medium | high
        "extra_body": {},         # optional provider request parameters
        "timeout": 600,
        "max_retries": 4,
        "json_mode": True,        # use response_format=json_object when available
        "pricing": {"input_per_m": 0.0, "output_per_m": 0.0},
        "concurrency": 4,
    },
    "local": {
        "endpoints_file": "local_endpoints.yaml",  # relative to the run config directory
        "poll_interval": 2,
        "retry_interval": 5,
        "probe_timeout": 5,
        "wait_timeout": None,   # wait for updated/recovered endpoints until cancelled
    },
    # role-specific overrides of llm_defaults
    "roles": {
        "agent": {},      # does the review work
    },
    "pipeline": {
        "max_records": None,          # cap #records screened (cost control)
        "screen_sample_seed": 13,
        "fulltext": True,             # try Europe PMC open-access full text
        "fulltext_unclear": "await",  # await | include | exclude (unclear after full text)
        "fulltext_screen_chars": 15000,
        "max_fulltext_chars": 60000,
        "validate_terms": True,
        "verify_synthesis": True,
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


def _load_layer(p: Path, depth: int = 0) -> dict:
    """Load a YAML file, recursively resolving `include:` (later layers override earlier)."""
    if depth > 8:
        raise ValueError("include nesting too deep")
    with open(p) as f:
        user = yaml.safe_load(f) or {}
    local = user.get("local") or {}
    if local.get("endpoints_file"):
        endpoint_file = Path(local["endpoints_file"]).expanduser()
        if not endpoint_file.is_absolute():
            local["endpoints_file"] = str((p.parent / endpoint_file).resolve())
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
    return cfg


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
