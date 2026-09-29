import os
from dataclasses import replace

import pytest

from agent.config import load_settings
from agent.setup_data import ensure_db


@pytest.fixture(scope="session")
def base_settings():
    s = load_settings()
    ensure_db(s)
    return s


@pytest.fixture
def settings(base_settings, tmp_path):
    return replace(base_settings, live_db_path=tmp_path / "live.sqlite",
                   retain_ledger_path=tmp_path / "ledger.jsonl")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test: set RUN_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
