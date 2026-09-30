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


settings = Settings(
    source_db=_path(os.environ.get("SOURCE_DB", "../sc_memory_extension/data/inventory_supply_chain_v1_1_ext.sqlite")),
    db_path=_path(os.environ.get("MERIDIAN_DB", "data/meridian.sqlite")),
    manifest_path=_path(os.environ.get("MANIFEST_PATH", "data/manifest.json")),
    dataset_dir=_path(os.environ.get("DATASET_DIR", "../sc_memory_extension/dataset")),
    today=os.environ.get("APP_TODAY", "2025-12-31"),
    groq_api_key=os.environ.get("GROQ_API_KEY", ""),
    groq_model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
    groq_base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
    ui_user=os.environ.get("UI_USER", ""),
    ui_password=os.environ.get("UI_PASSWORD", ""),
    session_secret=os.environ.get("UI_SESSION_SECRET", ""),
)
