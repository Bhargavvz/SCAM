"""Unzip the dataset's SQLite copy into DB_PATH (the dataset folder itself is never modified)."""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from agent.config import Settings, load_settings


def ensure_db(settings: Settings) -> Path:
    if settings.db_path.exists():
        return settings.db_path
    if not settings.db_zip_path.exists():
        raise FileNotFoundError(f"neither {settings.db_path} nor {settings.db_zip_path} exists")
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.db_path.with_name(settings.db_path.name + ".part")
    with zipfile.ZipFile(settings.db_zip_path) as z:
        member = next(n for n in z.namelist() if n.endswith(".sqlite"))
        with z.open(member) as src, tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    tmp.replace(settings.db_path)
    return settings.db_path


if __name__ == "__main__":
    print(ensure_db(load_settings()))
