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
