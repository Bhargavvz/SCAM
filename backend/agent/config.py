"""Runtime settings. Everything environment-specific comes from .env / the process environment."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROVIDERS = ("anthropic", "groq")


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (ROOT / p).resolve()


def _float_or_none(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


@dataclass(frozen=True)
class Settings:
    db_path: Path
    db_zip_path: Path
    live_db_path: Path
    retain_ledger_path: Path
    corpus_path: Path
    dataset_dir: Path
    default_as_of: str
    hindsight_base_url: str
    hindsight_api_key: str | None
    hindsight_bank_id: str
    llm_model: str
    llm_structured_effort: str
    llm_rationale_effort: str
    llm_max_tokens: int
    llm_refusal_fallback: str
    llm_temperature_structured: float | None
    llm_temperature_rationale: float | None
    writeback_retain: bool
    retention_log_path: Path
    mental_models_in_reflect: bool
    llm_provider: str = "anthropic"
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_price_input_per_mtok: float | None = None
    llm_price_output_per_mtok: float | None = None
    llm_compact: bool = False
    llm_tokens_per_minute: int | None = None


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env", override=False)
    e = os.environ.get
    s = Settings(
        db_path=_path(e("DB_PATH", "data/inventory_supply_chain_v1_1_ext.sqlite")),
        db_zip_path=_path(e("DB_ZIP_PATH", "dataset/inventory_supply_chain_v1_1_ext.sqlite.zip")),
        live_db_path=_path(e("LIVE_DB_PATH", "runtime/live.sqlite")),
        retain_ledger_path=_path(e("RETAIN_LEDGER_PATH", "runtime/retain_ledger.jsonl")),
        corpus_path=_path(e("CORPUS_PATH", "dataset/memory_corpus.jsonl")),
        dataset_dir=_path(e("DATASET_DIR", "dataset")),
        default_as_of=e("DEFAULT_AS_OF", "2025-12-31"),
        hindsight_base_url=e("HINDSIGHT_BASE_URL", "http://localhost:8888"),
        hindsight_api_key=e("HINDSIGHT_API_KEY") or None,
        hindsight_bank_id=e("HINDSIGHT_BANK_ID", ""),
        llm_model=e("LLM_MODEL", "claude-opus-5"),
        llm_structured_effort=e("LLM_STRUCTURED_EFFORT", "low"),
        llm_rationale_effort=e("LLM_RATIONALE_EFFORT", "high"),
        llm_max_tokens=int(e("LLM_MAX_TOKENS", "16000")),
        llm_refusal_fallback=e("LLM_REFUSAL_FALLBACK", "default"),
        llm_temperature_structured=_float_or_none(e("LLM_TEMPERATURE_STRUCTURED")),
        llm_temperature_rationale=_float_or_none(e("LLM_TEMPERATURE_RATIONALE")),
        writeback_retain=e("WRITEBACK_RETAIN", "1") == "1",
        retention_log_path=_path(e("RETENTION_LOG_PATH", "runtime/retention_log.jsonl")),
        mental_models_in_reflect=e("MENTAL_MODELS_IN_REFLECT", "1") == "1",
        llm_provider=e("LLM_PROVIDER", "anthropic"),
        groq_api_key=e("GROQ_API_KEY") or None,
        groq_base_url=e("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        llm_price_input_per_mtok=_float_or_none(e("LLM_PRICE_INPUT_PER_MTOK")),
        llm_price_output_per_mtok=_float_or_none(e("LLM_PRICE_OUTPUT_PER_MTOK")),
        llm_compact=e("LLM_COMPACT", "0") == "1",
        llm_tokens_per_minute=int(e("LLM_TOKENS_PER_MINUTE")) if e("LLM_TOKENS_PER_MINUTE") else None,
    )
    if s.llm_provider not in PROVIDERS:
        raise ValueError(f"LLM_PROVIDER must be one of {PROVIDERS}, got {s.llm_provider!r}")
    for name in ("llm_structured_effort", "llm_rationale_effort"):
        if getattr(s, name) not in EFFORTS:
            raise ValueError(f"{name.upper()} must be one of {EFFORTS}, got {getattr(s, name)!r}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s.default_as_of):
        raise ValueError(f"DEFAULT_AS_OF must be YYYY-MM-DD, got {s.default_as_of!r}")
    return s
