"""Settings, read from the environment (and .env at the project root)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _path(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


@dataclass(frozen=True)
class Settings:
    source_db: Path
    db_path: Path
    manifest_path: Path
    dataset_dir: Path
    today: str
    groq_api_key: str
    groq_model: str
    groq_base_url: str
    ui_user: str
    ui_password: str
    session_secret: str
    hindsight_base_url: str
    hindsight_api_key: str
    bank_id: str
    memory_sync: bool
    memory_batch_size: int


settings = Settings(
    source_db=_path(os.environ.get("SOURCE_DB", "../sc_memory_extension/data/inventory_supply_chain_v1_1_ext.sqlite")),
    db_path=_path(os.environ.get("MERIDIAN_DB", "data/meridian.sqlite")),
    manifest_path=_path(os.environ.get("MANIFEST_PATH", "data/manifest.json")),
    dataset_dir=_path(os.environ.get("MERIDIAN_DATASET_DIR", "dataset")),
    today=os.environ.get("APP_TODAY", "2025-12-31"),
    groq_api_key=os.environ.get("GROQ_API_KEY", ""),
    groq_model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
    groq_base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
    ui_user=os.environ.get("UI_USER", ""),
    ui_password=os.environ.get("UI_PASSWORD", ""),
    session_secret=os.environ.get("UI_SESSION_SECRET", ""),
    hindsight_base_url=os.environ.get("HINDSIGHT_BASE_URL", ""),
    hindsight_api_key=os.environ.get("HINDSIGHT_API_KEY", ""),
    bank_id=os.environ.get("MERIDIAN_BANK_ID") or os.environ.get("HINDSIGHT_BANK_ID", "meridian-scm"),
    memory_sync=os.environ.get("MEMORY_SYNC", "1") == "1",
    memory_batch_size=int(os.environ.get("MEMORY_BATCH_SIZE", "20")),
)

# The vendored decision agent (backend/agent) reads its own settings from the environment. Point it at Meridian's
# database, dataset and runtime files with absolute paths so both packages agree on every location.
for _k, _v in {
    "DB_PATH": settings.db_path,
    "LIVE_DB_PATH": ROOT / "data" / "agent_live.sqlite",
    "RETAIN_LEDGER_PATH": ROOT / "data" / "agent_retain_ledger.jsonl",
    "RETENTION_LOG_PATH": ROOT / "data" / "retention_log.jsonl",
    "CORPUS_PATH": settings.dataset_dir / "memory_corpus.jsonl",
    "DATASET_DIR": settings.dataset_dir,
    "DEFAULT_AS_OF": settings.today,
    "HINDSIGHT_BANK_ID": settings.bank_id,
}.items():
    os.environ.setdefault(_k, str(_v))
os.environ.setdefault("LLM_PROVIDER", "groq")
os.environ.setdefault("LLM_MODEL", settings.groq_model)
os.environ.setdefault("LLM_COMPACT", "1")
