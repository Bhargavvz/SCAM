import pytest

from agent.config import ROOT, load_settings

KEYS = ["DB_PATH", "LIVE_DB_PATH", "HINDSIGHT_BANK_ID", "LLM_MODEL", "LLM_STRUCTURED_EFFORT",
        "LLM_RATIONALE_EFFORT", "LLM_TEMPERATURE_STRUCTURED", "DEFAULT_AS_OF", "WRITEBACK_RETAIN",
        "MENTAL_MODELS_IN_REFLECT", "RETENTION_LOG_PATH"]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    return tmp_path / "missing.env"


def test_defaults_and_relative_paths(clean_env):
    s = load_settings(clean_env)
    assert s.db_path == ROOT / "data" / "inventory_supply_chain_v1_1_ext.sqlite"
    assert s.llm_model == "claude-opus-5"
    assert s.llm_structured_effort == "low" and s.llm_rationale_effort == "high"
    assert s.llm_temperature_structured is None
    assert s.hindsight_bank_id == ""
    assert s.writeback_retain is True
    assert s.mental_models_in_reflect is True
    assert s.retention_log_path == ROOT / "runtime" / "retention_log.jsonl"


def test_env_overrides(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.sqlite"))
    monkeypatch.setenv("HINDSIGHT_BANK_ID", "bank-a")
    monkeypatch.setenv("LLM_TEMPERATURE_STRUCTURED", "0.1")
    monkeypatch.setenv("WRITEBACK_RETAIN", "0")
    s = load_settings(clean_env)
    assert s.db_path == tmp_path / "x.sqlite"
    assert s.hindsight_bank_id == "bank-a"
    assert s.llm_temperature_structured == 0.1
    assert s.writeback_retain is False


@pytest.mark.parametrize("key,value", [("LLM_STRUCTURED_EFFORT", "extreme"), ("DEFAULT_AS_OF", "2025/12/31")])
def test_invalid_values_raise(clean_env, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        load_settings(clean_env)
