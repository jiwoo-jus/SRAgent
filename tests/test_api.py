"""API configuration and transport checks without paid requests."""
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
from sragent.config import LLM_STAGES, load_config, role_cfg, stage_cfg
from sragent.llm import LLM, CostTracker

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_api_transport(provider, monkeypatch):
    calls = {}
    def create(**kwargs):
        calls["request"] = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3, total_tokens=13))
    def client(**kwargs):
        calls["client"] = kwargs
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=client))
    monkeypatch.setenv(provider.upper() + "_API_KEY", "test-only-key")
    cfg = load_config(None, {"llm_defaults": {"provider": provider, "model": "test-model", "api_env": None}})
    llm = LLM(cfg, "agent", CostTracker(1), None)
    assert llm.chat([{"role": "user", "content": "Return JSON"}]) == {"ok": True}
    assert calls["client"]["api_key"] == "test-only-key"
    assert calls["request"]["model"] == "test-model"
    assert calls["request"]["response_format"] == {"type": "json_object"}
    if provider == "gemini":
        assert calls["client"]["base_url"].startswith("https://generativelanguage.googleapis.com/")

def test_unsupported_provider():
    cfg = load_config(None, {"llm_defaults": {"provider": "unsupported"}})
    with pytest.raises(ValueError, match="Unsupported API provider"):
        LLM(cfg, "agent", CostTracker(1), None)

def test_example_configs():
    runs = [p for kind in ("benchmark", "review") for p in (ROOT / "configs" / kind).rglob("*.yaml") if p.name != "topic.yaml"]
    assert runs
    for path in runs:
        cfg = load_config(path)
        assert Path(role_cfg(cfg, "agent")["api_env"]).parent == ROOT
        assert cfg["run_dir"] == "runs/" + str(path.relative_to(ROOT / "configs").with_suffix(""))   # derived
        assert ROOT / cfg["local"]["endpoints_file"] == ROOT / "local_endpoints.yaml"   # project root
        providers = {"openai", "gemini", "anthropic", "local"}
        for role in cfg["roles"]:
            assert role_cfg(cfg, role)["provider"] in providers
        for stage in LLM_STAGES:                                   # stage_models resolve to a known provider and price
            sc = stage_cfg(cfg, stage)
            assert sc["provider"] in providers and sc.get("pricing") is not None
        import yaml
        assert not any("models" in str(i) for i in (yaml.safe_load(path.read_text()) or {}).get("include") or []), \
            f"{path} includes a model file; models are chosen in the run file (configs/models.yaml is the catalog)"
        for c in [role_cfg(cfg, r) for r in cfg["roles"]] + [stage_cfg(cfg, st) for st in LLM_STAGES]:
            if c["provider"] != "local":                          # every API model has a price (budget guard)
                assert c["model"] in cfg["_catalog"], f"{path}: {c['model']} is not in configs/models.yaml"


def test_model_catalog(tmp_path):
    """Run files choose models by name; provider and price come from the catalog unless written."""
    import pytest
    import yaml
    from sragent.ui.configs import Library
    (tmp_path / "configs" / "review" / "t").mkdir(parents=True)
    (tmp_path / "configs" / "models.yaml").write_text((ROOT / "configs/models.yaml").read_text())
    (tmp_path / "configs" / "review" / "t" / "topic.yaml").write_text((ROOT / "configs/review/_template/topic.yaml").read_text())
    run = tmp_path / "configs" / "review" / "t" / "run.yaml"
    base = {"include": ["topic.yaml"], "models_file": "../../models.yaml",
            "llm_defaults": {"model": "claude-haiku-5-5", "reasoning_effort": "low"},
            "roles": {"judge": {"model": "claude-sonnet-5-5"}, "judge2": {"model": "gemma-4-31b-it", "pricing": {"input_per_m": 1, "output_per_m": 2}}},
            "stage_models": {"extract": {"model": "gemini-3.8-flash"}}}
    run.write_text(yaml.safe_dump(base))
    cfg = load_config(run)
    a, j, j2, ex = role_cfg(cfg, "agent"), role_cfg(cfg, "judge"), role_cfg(cfg, "judge2"), stage_cfg(cfg, "extract")
    assert (a["provider"], a["pricing"]["long_context"]["threshold_tokens"]) == ("anthropic", 100000)
    assert j["provider"] == "anthropic" and j["pricing"] == {"input_per_m": 2.0, "output_per_m": 10.0}   # not Haiku's tiers
    assert j2["provider"] == "gemini" and j2["pricing"] == {"input_per_m": 1, "output_per_m": 2}         # written price wins
    assert (ex["provider"], ex["model"], ex["reasoning_effort"], ex["pricing"]["input_per_m"]) == ("gemini", "gemini-3.8-flash", "low", 0.75)
    run.write_text(yaml.safe_dump({**base, "stage_models": {"rob": {"model": "not-in-catalog"}}}))
    with pytest.raises(ValueError, match="not in the model catalog"):
        load_config(run)
    run.write_text(yaml.safe_dump(base))
    lib = Library(tmp_path / "configs")
    cat = (tmp_path / "configs" / "models.yaml").read_text()
    without_sonnet = yaml.safe_dump({"models": {k: v for k, v in yaml.safe_load(cat)["models"].items() if k != "claude-sonnet-5-5"}})
    r = lib.check("configs/models.yaml", without_sonnet)
    assert not r["ok"] and any("claude-sonnet-5-5 is still used" in e for e in r["errors"])
    assert {m["name"] for m in lib.catalog_models()} >= {"claude-haiku-5-5", "gemini-3.8-flash"}
    import os
    cwd = os.getcwd()
    os.chdir(tmp_path)                                           # the New review form writes a run file that loads
    try:
        res = lib.new_topic({"key": "new_t", "question": "Q?", "seed_eligibility": "Include X.", "agent_model": "claude-haiku-5-5",
                             "agent_effort": "medium", "budget_usd": 3})
        c = load_config(tmp_path / res["path"])
        assert role_cfg(c, "agent")["model"] == "claude-haiku-5-5" and role_cfg(c, "agent")["reasoning_effort"] == "medium"
        assert role_cfg(c, "agent")["pricing"]["input_per_m"] == 0.1 and res["check"]["ok"]
        assert c["pipeline"]["fulltext_unclear"] == "await"     # human in the loop: a person decides unclear records
        res2 = lib.new_run({"topic_path": "configs/review/new_t/topic.yaml", "name": "ao", "agent_model": "claude-haiku-5-5",
                            "human_mode": "agent_only", "fulltext_unclear": "exclude"})
        assert load_config(tmp_path / res2["path"])["pipeline"]["fulltext_unclear"] == "exclude"
    finally:
        os.chdir(cwd)
