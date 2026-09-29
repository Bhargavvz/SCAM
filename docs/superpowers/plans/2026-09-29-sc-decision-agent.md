# Supply Chain Memory & Decision Agent - Prototype Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable CLI agent that takes a free-text supply disruption report and returns a decision card. The card is grounded in Hindsight memory and the SQLite ground truth, candidate actions are simulated deterministically, and the recommendation passes guardrails and cites its evidence. The prototype ships with an eval harness, a scorecard and a scripted demo.

**Architecture:** A code-controlled workflow wraps a small Claude tool-use loop.
- Deterministic Python does the heavy lifting: as-of-filtered SQLite views, a simulator ported exactly from the dataset generator, Hindsight recall with client-side date enforcement, and guardrail checks.
- Claude (low effort) investigates with tools and submits a structured recommendation. Code validates it (commitment breaches, feasibility, citations).
- A second Claude call (high effort) writes the prose rationale, and code checks it for numbers the simulator didn't produce.
- Write-back goes to a separate append-only SQLite file and to Hindsight retain. External systems are labelled mock no-ops.

**Tech Stack:** Python 3.11 (uv venv), `sqlite3` (stdlib), `anthropic` SDK (manual tool loop), `hindsight-client`, `pyyaml`, `python-dotenv`, `rich` (console), `pytest`.

**Also in scope (merged build prompt):** stand up Hindsight locally and configure the `supply-chain-memory` bank (mission, 8 directives, disposition). Ingest the 2,201 memory docs and create 12 pattern Mental Models behind a flag. Add a 7-capability question router and CLI on top of the agent. Add the eval extensions (recall/reflect mode, hop_count breakdown, pattern-detection probe with and without Mental Models) and the 7 capability demo queries.

**Spec:**
- `docs/superpowers/specs/2026-09-29-sc-decision-agent-brief.md`: the user's first brief verbatim, plus planning findings at the bottom. This is the primary spec.
- `docs/superpowers/specs/2026-09-29-memory-agent-build-prompt-notes.md`: the second build prompt's requirements (mission, directives and demo queries verbatim) and where the dataset overrides it.

Read both before starting.

## Global Constraints

- Repo root is `sc_memory_extension/` (git, current branch `feat01/SCAM-v1.1`). All work happens on a new branch `feat/decision-agent`. All paths below are relative to the repo root.
- Everything under `dataset/` is a read-only input: never write, rename or regenerate files there. The SQLite copy is unzipped to `data/` (gitignored).
- Python 3.11 in `.venv` (system Python is 3.9, and the Anthropic SDK 1.x needs 3.10+). Run tests with `.venv/bin/python -m pytest`.
- No hardcoded secrets. Bank id, model name, DB paths, effort levels and the base URL come from `.env` / environment (`agent/config.py`). `.env` is gitignored; `.env.example` is committed.
- Default model: `LLM_MODEL=claude-opus-5`. Current models reject `temperature` (HTTP 400). Use `output_config.effort`: `LLM_STRUCTURED_EFFORT=low` for the tool loop, `LLM_RATIONALE_EFFORT=high` for the prose. Send `temperature` only when `LLM_TEMPERATURE_*` is set for a model that accepts it.
- Refusal fallback is on by default (`LLM_REFUSAL_FALLBACK=default` → `client.beta.messages.create(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")`). Leave the value empty to disable.
- The LLM never produces cost, stockout-day or service numbers. Those come only from `agent/simulate.py`, and prose numbers are checked against simulator and DB values.
- As-of rule: no narrative doc dated after `as_of` (end of day, HQ offset `-05:00`) may reach the model, eval or live. SQL is served through TEMP views that hide or mask post-`as_of` rows.
- Runtime writes go only to the live DB (`runtime/live.sqlite` by default; eval and demo use their own files). `decisions_live` / `commitments_live` are append-only (triggers abort UPDATE/DELETE).
- External systems (ERP, supplier portal, email, MES, QMS) are never called. Their would-be actions are strings prefixed `[MOCK - no external call made]`.
- Hindsight client calls pass only the kwargs the installed client accepts (signature introspection, same approach as `dataset/code/hindsight_loader.py`).
- The Hindsight server runs locally in Docker (`ghcr.io/vectorize-io/hindsight`, API :8888, UI :9999, volume `hindsight-data`). It uses its own LLM credentials (`HINDSIGHT_API_LLM_PROVIDER`, `HINDSIGHT_API_LLM_API_KEY`, stable `HINDSIGHT_API_WORKER_ID`). These are read only by `scripts/start_hindsight.sh`, never by the agent.
- One bank: `supply-chain-memory`. Mission and directives are copied verbatim from the merge-notes spec. The loader in `dataset/code/` is run unmodified, with `--log runtime/retention_log.jsonl` so nothing is written into `dataset/`.
- Mental Models are gated by `MENTAL_MODELS_IN_REFLECT` (default `1`). With `0`, native reflect is called with `exclude_mental_models=True`. If the installed client cannot exclude them, construction fails loudly instead of silently including them. Mental Models never reach as-of runs before the corpus horizon, because they are only consulted by native reflect.
- Installing the Docker image and running ingestion cost money and download data. Both need the user's go-ahead: show the loader's `--dry-run` token estimate first.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A free-text report with no RPO id, unknown ids, or a PO with no date revision.** The agent must say it cannot simulate and show no numbers. It must not guess, and it must not crash with a raw traceback. Pinned by `test_build_state_errors` (Task 4) and `test_no_rpo_skips_decision_llm` (Task 8).
2. **Hindsight hits that lack `document_id`, carry ambiguous timestamps, or point at holdout/future docs.** These must be resolved via timestamp+context or dropped. They must never be leaked or silently kept. Pinned by `test_recall_filters_future_and_unresolved` and `test_recall_resolves_by_timestamp_and_context` (Task 5).
3. **LLM-written SQL that tries to bypass the as-of views.** This covers `main.`/`live.` prefixes, multiple statements, writes, PRAGMA, and runaway queries. It must be rejected, and the error returned to the model as a tool error. Pinned by `test_check_query_rejects` and `test_run_sql_timeout` (Task 3), and `test_sql_guard_error_goes_back_to_model` (Task 8).
4. **Write-back from demo/console runs contaminating later eval runs through the shared Hindsight bank.** Live facts must be invisible to runs using a different live DB, and native reflect must be disabled. Pinned by `test_live_hits_only_visible_with_matching_live_db` and `test_native_reflect_guard` (Task 5).
5. **Hindsight unreachable mid-run.** The agent must fail with a clear "Hindsight recall failed … check HINDSIGHT_BASE_URL" error, not proceed on empty memory as if nothing happened before. Pinned by `test_memory_failure_is_reported` (Task 8).

---

## File map

| File | Responsibility |
|---|---|
| `requirements.txt`, `pytest.ini`, `.env.example`, `.gitignore` | env + tooling |
| `agent/config.py` | `Settings` from env; path resolution; validation |
| `agent/setup_data.py` | unzip the SQLite copy into `data/` |
| `agent/check_connectivity.py` | Stage 0 checks (DB, Hindsight known fact, ingestion count) |
| `scripts/start_hindsight.sh` | start / reuse the local Hindsight Docker container |
| `agent/bank_setup.py` | bank mission, 8 directives, disposition - idempotent |
| `agent/mental_models.py` | 12 pattern Mental Models (create / list / delete), tagged `planted-pattern` |
| `agent/capabilities.py` | 7-capability router + handlers (reflect + SQL ground truth) + `CapabilityAnswer` |
| `eval/run_pattern_probe.py`, `eval/pattern_probe.yaml` | pattern-detection probe (12 patterns, with/without Mental Models) |
| `agent/db.py` | read-only connection, attached live DB, as-of TEMP views, live schema + append-only triggers |
| `agent/sql_tools.py` | guarded `run_sql` + curated lookups (commitments, scorecard, events, links, decisions, record existence, schema) |
| `agent/sim_rules.yaml` | constants and risk rules copied from the generator |
| `agent/simulate.py` | `ScenarioState`, `build_state`, `simulate_options`, `simulate_action`, `best_option` |
| `agent/scenarios.py` | holdout loading, MRP context for a scenario, parity comparison |
| `agent/corpus.py` | `DocIndex` over `memory_corpus.jsonl`: resolve hits, as-of visibility, supersession graph |
| `agent/hindsight_tools.py` | `HindsightMemory` (recall / reflect / get_document / retain), `RetainLedger` |
| `agent/llm.py` | Anthropic wrapper: effort, caching, fallback, usage + cost |
| `agent/parsing.py` | entity ids + proposed action from report text |
| `agent/guardrails.py` | action violations, citation verification, ungrounded-number check |
| `agent/card.py` | `DecisionCard`, `QAResult` dataclasses |
| `agent/agent_core.py` | `DecisionAgent.run` (decision loop), `answer_question` (QA mode), `build_agent` |
| `agent/log_decision.py` | write-back rows, retain summary, mock external actions |
| `interface/decision_console.py` | rich CLI rendering + arg parsing |
| `eval/scoring.py`, `eval/scorecard.py`, `eval/run_eval_questions.py`, `eval/run_holdout_scenarios.py` | eval harness + scorecard |
| `demo/scenarios.yaml`, `demo/run_demo.py`, `demo/EXPECTED.md` | scripted walkthrough, expected vs actual |
| `README.md` | architecture, setup, running, limitations |
| `tests/…` | one test module per agent module + `fakes.py` |

---

### Task 1: Scaffold, config, data setup and Stage 0 connectivity

**Files:**
- Create: `requirements.txt`, `pytest.ini`, `.env.example`, `.gitignore`, `agent/__init__.py`, `interface/__init__.py`, `eval/__init__.py`, `demo/__init__.py`, `agent/config.py`, `agent/setup_data.py`, `agent/check_connectivity.py`, `tests/__init__.py`, `tests/conftest.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `agent.config.ROOT: Path`; `agent.config.Settings` (frozen dataclass; fields listed below); `agent.config.load_settings(env_file: Path | None = None) -> Settings`; `agent.setup_data.ensure_db(settings) -> Path`; `agent.check_connectivity.check_db(settings) -> str`, `check_hindsight(settings) -> str`, `main() -> int`. Test fixtures `base_settings`, `settings` (tmp live DB + ledger), `db_factory(as_of) -> sqlite3.Connection` (fixture body completed in Task 2).

- [ ] **Step 1: Create branch, venv and tooling files**

```bash
cd sc_memory_extension
git checkout -b feat/decision-agent
uv venv --python 3.11 .venv
```

`requirements.txt`:
```
anthropic>=0.70
hindsight-client
pyyaml>=6.0
python-dotenv>=1.0
rich>=13.0
pytest>=8.0
```

```bash
uv pip install --python .venv/bin/python -r requirements.txt
```

`pytest.ini`:
```ini
[pytest]
testpaths = tests
markers =
    live: needs a running Hindsight server and Anthropic credentials (run with RUN_LIVE=1)
```

`.gitignore`:
```
.venv/
__pycache__/
.pytest_cache/
.env
data/
runtime/
eval/out/*.sqlite
eval/out/cards/
demo/out/*.sqlite
```

`.env.example`:
```
# ---- data (relative paths are resolved against the repo root)
DB_PATH=data/inventory_supply_chain_v1_1_ext.sqlite
DB_ZIP_PATH=dataset/inventory_supply_chain_v1_1_ext.sqlite.zip
LIVE_DB_PATH=runtime/live.sqlite
RETAIN_LEDGER_PATH=runtime/retain_ledger.jsonl
CORPUS_PATH=dataset/memory_corpus.jsonl
DATASET_DIR=dataset
DEFAULT_AS_OF=2025-12-31
# ---- Hindsight (client side, used by the agent)
HINDSIGHT_BASE_URL=http://localhost:8888
HINDSIGHT_API_KEY=
HINDSIGHT_BANK_ID=supply-chain-memory
RETENTION_LOG_PATH=runtime/retention_log.jsonl
# 1 = native reflect may use the 12 pattern Mental Models; 0 = exclude them (ablation)
MENTAL_MODELS_IN_REFLECT=1
# ---- Hindsight server (read only by scripts/start_hindsight.sh; the server's own LLM for extraction/reflect)
HINDSIGHT_API_LLM_PROVIDER=openai
HINDSIGHT_API_LLM_API_KEY=
HINDSIGHT_API_WORKER_ID=sc-memory-worker-1
# ---- Claude (credentials: ANTHROPIC_API_KEY or an `ant auth login` profile)
LLM_MODEL=claude-opus-5
LLM_STRUCTURED_EFFORT=low
LLM_RATIONALE_EFFORT=high
LLM_MAX_TOKENS=16000
# "default" = server-side refusal fallback (beta server-side-fallback-2026-07-01); empty = off
LLM_REFUSAL_FALLBACK=default
# Only for models that still accept sampling params; current models return 400 if set
LLM_TEMPERATURE_STRUCTURED=
LLM_TEMPERATURE_RATIONALE=
# ---- behaviour
WRITEBACK_RETAIN=1
```

Create empty `agent/__init__.py`, `interface/__init__.py`, `eval/__init__.py`, `demo/__init__.py`, `tests/__init__.py`. Then `cp .env.example .env` and edit `HINDSIGHT_BASE_URL` / `HINDSIGHT_BANK_ID` to where the prior project's bank lives.

- [ ] **Step 2: Write the failing config tests**

`tests/test_config.py`:
```python
from pathlib import Path

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
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL / ERROR, `ModuleNotFoundError: No module named 'agent.config'`

- [ ] **Step 4: Implement `agent/config.py`**

```python
"""Runtime settings. Everything environment-specific comes from .env / the process environment."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
EFFORTS = ("low", "medium", "high", "xhigh", "max")


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
    )
    for name in ("llm_structured_effort", "llm_rationale_effort"):
        if getattr(s, name) not in EFFORTS:
            raise ValueError(f"{name.upper()} must be one of {EFFORTS}, got {getattr(s, name)!r}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s.default_as_of):
        raise ValueError(f"DEFAULT_AS_OF must be YYYY-MM-DD, got {s.default_as_of!r}")
    return s
```

- [ ] **Step 5: Run config tests**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: 4 passed

- [ ] **Step 6: Implement `agent/setup_data.py`, `tests/conftest.py`, `agent/check_connectivity.py`**

`agent/setup_data.py`:
```python
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
```

`tests/conftest.py`:
```python
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
```

`agent/check_connectivity.py`:
```python
"""Stage 0: prove the DB and the Hindsight bank are reachable before building anything on them."""
from __future__ import annotations

import sqlite3
import sys

from agent.config import Settings, load_settings
from agent.setup_data import ensure_db

KNOWN_QUERY = "Why did RPO003179 slip and what is its latest ETA?"
KNOWN_TOKEN = "RPO003179"  # DOC000248 / DOC000254 in the memory split


def _field(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def check_db(settings: Settings) -> str:
    con = sqlite3.connect(f"file:{ensure_db(settings)}?mode=ro", uri=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM disruption_events").fetchone()[0]
    finally:
        con.close()
    if n != 2696:
        raise RuntimeError(f"expected 2696 disruption_events, found {n}")
    return f"DB ok: {settings.db_path.name} disruption_events={n}"


def check_hindsight(settings: Settings) -> str:
    from hindsight_client import Hindsight

    if not settings.hindsight_bank_id:
        raise RuntimeError("HINDSIGHT_BANK_ID is empty - set it in .env")
    client = Hindsight(base_url=settings.hindsight_base_url,
                       **({"api_key": settings.hindsight_api_key} if settings.hindsight_api_key else {}))
    resp = client.recall(bank_id=settings.hindsight_bank_id, query=KNOWN_QUERY)
    results = list(_field(resp, "results") or [])
    if not results:
        raise RuntimeError("recall returned 0 results - is the bank populated?")
    texts = [_field(r, "text") or "" for r in results]
    if not any(KNOWN_TOKEN in t for t in texts):
        raise RuntimeError(f"known fact {KNOWN_TOKEN} not recalled; first result: {texts[0][:160]}")
    first = results[0]
    return ("Hindsight ok: {n} results; known fact found; first hit document_id={d} mentioned_at={m} context={c}"
            .format(n=len(results), d=_field(first, "document_id"), m=_field(first, "mentioned_at"),
                    c=_field(first, "context")))


EXPECTED_MEMORY_DOCS = 2201  # split=memory rows in dataset/memory_corpus.jsonl


def check_ingestion(settings: Settings) -> str:
    import json

    path = settings.retention_log_path
    if not path.exists():
        raise RuntimeError(f"{path} not found - run the loader (Task 1B)")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    ok = {r["doc_id"] for r in rows if r.get("status") == "ok"}
    if len(ok) != EXPECTED_MEMORY_DOCS:
        raise RuntimeError(f"{len(ok)}/{EXPECTED_MEMORY_DOCS} memory docs retained ok - re-run the loader (it resumes)")
    return f"Ingestion ok: {len(ok)}/{EXPECTED_MEMORY_DOCS} memory docs retained"


def main() -> int:
    s = load_settings()
    ok = True
    for check in (check_db, check_ingestion, check_hindsight):
        try:
            print(check(s))
        except Exception as ex:  # report every failing check, not just the first
            ok = False
            print(f"{check.__name__} FAILED: {ex}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Run the DB half of Stage 0 (the Hindsight half runs at the end of Task 1B)**

Run: `.venv/bin/python -m agent.setup_data && .venv/bin/python -c "from agent.config import load_settings; from agent.check_connectivity import check_db; print(check_db(load_settings()))"`
Expected:
```
.../data/inventory_supply_chain_v1_1_ext.sqlite
DB ok: inventory_supply_chain_v1_1_ext.sqlite disruption_events=2696
```
`python -m agent.check_connectivity` (all checks) is expected to fail on Hindsight until Tasks 1A and 1B have stood up and filled the bank.

- [ ] **Step 8: Commit**

```bash
git add requirements.txt pytest.ini .env.example .gitignore agent/ interface/__init__.py eval/__init__.py demo/__init__.py tests/ docs/
git commit -m "feat: scaffold decision agent, config and Stage 0 connectivity checks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A: Local Hindsight server and bank configuration

**Files:**
- Create: `scripts/start_hindsight.sh`, `agent/bank_setup.py`
- Test: `tests/test_bank_setup.py`

**Interfaces:**
- Consumes: `Settings` (Task 1).
- Produces: `agent.bank_setup.MISSION: str`, `TRAITS: list[str]`, `DIRECTIVES: list[tuple[str, str]]` (name, content; 8 items), `DISPOSITION: dict[str, int]`, `reflect_mission() -> str`, `make_client(settings)` (a `hindsight_client.Hindsight`), `configure_bank(client, bank_id: str, state_path: Path) -> list[str]` (log lines; idempotent), `main() -> int`.

- [ ] **Step 1: Write the failing tests**

`tests/test_bank_setup.py`:
```python
from agent.bank_setup import DIRECTIVES, DISPOSITION, MISSION, TRAITS, configure_bank, reflect_mission


class FakeBankClient:
    def __init__(self, with_list=True):
        self.banks, self.config, self.directives = set(), {}, []
        if not with_list:
            self.list_directives = None  # simulate a client without the method

    def create_bank(self, bank_id):
        if bank_id in self.banks:
            raise RuntimeError("409 bank exists")
        self.banks.add(bank_id)

    def update_bank_config(self, bank_id, **kw):
        self.config[bank_id] = kw

    def create_directive(self, bank_id, name, content):
        self.directives.append((bank_id, name, content))

    def list_directives(self, bank_id):
        return [{"name": n} for b, n, _ in self.directives if b == bank_id]


def test_configure_is_idempotent(tmp_path):
    c = FakeBankClient()
    configure_bank(c, "supply-chain-memory", tmp_path / "state.json")
    configure_bank(c, "supply-chain-memory", tmp_path / "state.json")
    assert len(c.directives) == 8
    cfg = c.config["supply-chain-memory"]
    assert cfg["reflect_mission"] == reflect_mission()
    assert {k: cfg[k] for k in DISPOSITION} == {"disposition_skepticism": 4, "disposition_literalism": 4,
                                                 "disposition_empathy": 2}


def test_state_file_used_when_client_cannot_list(tmp_path):
    c = FakeBankClient(with_list=False)
    configure_bank(c, "b", tmp_path / "state.json")
    configure_bank(c, "b", tmp_path / "state.json")
    assert len(c.directives) == 8


def test_prompt_text_is_verbatim():
    assert MISSION.startswith("Institutional supply-chain memory for a multi-plant consumer-goods manufacturer.")
    assert MISSION.endswith("recommend responses grounded in what worked before.")
    assert len(DIRECTIVES) == 8 and len(TRAITS) == 4
    assert DIRECTIVES[2][1] == ("Never recommend cancelling a purchase order without first checking open commitments "
                                "with that supplier.")
    assert all(t in reflect_mission() for t in TRAITS)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_bank_setup.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'agent.bank_setup'`

- [ ] **Step 3: Implement `agent/bank_setup.py`**

```python
"""Configure the `supply-chain-memory` bank: reflect mission (+ the four disposition traits), the 8 directives and
Hindsight's numeric disposition. Idempotent - safe to run before and after ingestion.

Hindsight's disposition is three 1-5 scales, so the build prompt's four traits map to skepticism 4 (cautious about
switches), literalism 4 (data-driven, exact figures), empathy 2, and are also written into the reflect mission."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from agent.config import Settings, load_settings

MISSION = (
    "Institutional supply-chain memory for a multi-plant consumer-goods manufacturer. This bank stores the complete "
    "operational history: suppliers, raw materials, plants, distribution centers, purchase orders, disruption events, "
    "negotiations, commitments, and past decisions with their outcomes. Use it to recall specific historical facts "
    "with evidence, connect causally related events across the supply chain, track open commitments and obligations, "
    "evaluate consequences of candidate actions by referencing precedents, and recommend responses grounded in what "
    "worked before.")
TRAITS = [
    "Cautious about supplier switches - always check commitment obligations first.",
    "Data-driven - quantify cost and stockout-day impacts.",
    "Precedent-aware - always look for similar past situations.",
    "Proactive - flag risks even when not asked about them.",
]
DIRECTIVES = [
    ("Cite evidence", "Cite the evidence (document dates and record IDs such as RPO, PO, EVT, DEC, CMT) behind every "
                      "claim and recommendation."),
    ("List affected commitments", "Always list open commitments that a recommended action could affect."),
    ("Check commitments before cancelling", "Never recommend cancelling a purchase order without first checking open "
                                            "commitments with that supplier."),
    ("Prefer the latest document", "Prefer the most recent document when facts conflict; say which earlier statement "
                                   "was superseded."),
    ("Contrast precedents", "When citing a precedent, state how today's conditions differ from the precedent's "
                            "conditions."),
    ("Flag known supplier patterns", "When a supplier has a known pattern (seasonal delays, size-dependent reliability, "
                                     "etc.), flag it explicitly."),
    ("Show prediction, confidence, last time", "For decision recommendations, always show: predicted outcome, "
                                               "confidence level, and what happened last time."),
    ("Facts vs observations", "Distinguish between facts (from documents) and observations (consolidated patterns) in "
                              "responses."),
]
DISPOSITION = {"disposition_skepticism": 4, "disposition_literalism": 4, "disposition_empathy": 2}


def reflect_mission() -> str:
    return MISSION + "\n\nHow you reason:\n" + "\n".join(f"- {t}" for t in TRAITS)


def make_client(settings: Settings):
    from hindsight_client import Hindsight

    return Hindsight(base_url=settings.hindsight_base_url,
                     **({"api_key": settings.hindsight_api_key} if settings.hindsight_api_key else {}))


def _name(item):
    return item.get("name") if isinstance(item, dict) else getattr(item, "name", None)


def _existing_directive_names(client, bank_id: str, state_path: Path) -> set[str]:
    lister = getattr(client, "list_directives", None)
    if lister is None:
        return set(json.loads(state_path.read_text())) if state_path.exists() else set()
    resp = lister(bank_id=bank_id)
    items = resp if isinstance(resp, list) else (getattr(resp, "items", None) or getattr(resp, "directives", None) or [])
    return {_name(i) for i in items}


def configure_bank(client, bank_id: str, state_path: Path) -> list[str]:
    log = []
    try:
        client.create_bank(bank_id=bank_id)
        log.append(f"created bank {bank_id}")
    except Exception as ex:  # the client raises when the bank already exists; real failures surface just below
        log.append(f"bank {bank_id} already exists ({type(ex).__name__})")
    client.update_bank_config(bank_id, reflect_mission=reflect_mission(), **DISPOSITION)
    log.append(f"reflect mission + disposition {DISPOSITION} set")
    have = _existing_directive_names(client, bank_id, state_path)
    created = []
    for name, content in DIRECTIVES:
        if name not in have:
            client.create_directive(bank_id=bank_id, name=name, content=content)
            created.append(name)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted((have | set(created)) - {None})))
    log.append(f"directives: {len(created)} created, {len(DIRECTIVES) - len(created)} already present")
    return log


def main() -> int:
    s = load_settings()
    if not s.hindsight_bank_id:
        print("HINDSIGHT_BANK_ID is empty - set it in .env", file=sys.stderr)
        return 1
    for line in configure_bank(make_client(s), s.hindsight_bank_id,
                               s.retention_log_path.with_name("bank_setup_state.json")):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_bank_setup.py -v`
Expected: 3 passed

- [ ] **Step 5: Write `scripts/start_hindsight.sh`**

```bash
#!/usr/bin/env bash
# Start (or reuse) a local Hindsight server: API on :8888, Control Plane UI on :9999, data in volume hindsight-data.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
: "${HINDSIGHT_API_LLM_API_KEY:?set HINDSIGHT_API_LLM_API_KEY in .env (the Hindsight server's own LLM key)}"
if docker ps -a --format '{{.Names}}' | grep -qx hindsight; then
  docker start hindsight >/dev/null
else
  docker run -d --name hindsight --restart unless-stopped --shm-size=1g -p 8888:8888 -p 9999:9999 \
    -e HINDSIGHT_API_LLM_PROVIDER="${HINDSIGHT_API_LLM_PROVIDER:-openai}" \
    -e HINDSIGHT_API_LLM_API_KEY="$HINDSIGHT_API_LLM_API_KEY" \
    -e HINDSIGHT_API_WORKER_ID="${HINDSIGHT_API_WORKER_ID:-sc-memory-worker-1}" \
    -v hindsight-data:/home/hindsight/.pg0 \
    ghcr.io/vectorize-io/hindsight:latest >/dev/null
fi
for _ in $(seq 1 90); do
  if curl -s -o /dev/null http://localhost:8888/; then echo "Hindsight API up on :8888, UI on :9999"; exit 0; fi
  sleep 2
done
echo "Hindsight did not answer on :8888 within 3 minutes - see: docker logs hindsight" >&2
exit 1
```
Then `chmod +x scripts/start_hindsight.sh`.

- [ ] **Step 6: Start the server and configure the bank (live - ask the user first)**

The first run pulls `ghcr.io/vectorize-io/hindsight:latest` (a multi-GB download). Ask the user before running it, and have them put the server LLM key in `.env` (`HINDSIGHT_API_LLM_PROVIDER`, `HINDSIGHT_API_LLM_API_KEY`).

Run: `scripts/start_hindsight.sh && .venv/bin/python -m agent.bank_setup`
Expected:
```
Hindsight API up on :8888, UI on :9999
created bank supply-chain-memory
reflect mission + disposition {...} set
directives: 8 created, 0 already present
```
If the installed client's method names differ from `create_bank` / `update_bank_config` / `create_directive`, print `[m for m in dir(Hindsight) if not m.startswith('_')]`. Adapt `configure_bank`, and adapt the fake in the test first. Open http://localhost:9999 and confirm the mission, the 8 directives and the disposition are shown.

- [ ] **Step 7: Commit**

```bash
git add scripts/start_hindsight.sh agent/bank_setup.py tests/test_bank_setup.py
git commit -m "feat: local Hindsight server script and bank mission/directives/disposition setup

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B: Ingest the memory corpus, create the 12 pattern Mental Models, finish Stage 0

**Files:**
- Create: `agent/mental_models.py`
- Test: `tests/test_mental_models.py`

**Interfaces:**
- Consumes: `make_client`, `configure_bank` (Task 1A); `check_connectivity` (Task 1); the unmodified `dataset/code/hindsight_loader.py`.
- Produces: `agent.mental_models.TAG = "planted-pattern"`, `PATTERN_MODELS: dict[str, tuple[str, str]]` (pattern id → (name, source_query)), `model_name(pid) -> str`, `list_pattern_models(client, bank_id) -> dict[str, dict]` (name → {id, name, content}), `create_pattern_models(client, bank_id) -> list[str]`, `delete_pattern_models(client, bank_id) -> list[str]`, `main(argv=None) -> int` (`create | list | delete`).

- [ ] **Step 1: Dry-run the loader and get the user's go-ahead**

Run: `.venv/bin/python dataset/code/hindsight_loader.py --corpus dataset/memory_corpus.jsonl --bank supply-chain-memory --dry-run`
Expected: `2201 memory docs (2023-01-02T... .. 2025-09-30T...), ~N tokens`, then the loader's own mission/directives and 3 sample payloads. The loader prints its own built-in mission. That is fine: Task 1A's configuration is the one applied, and Step 3 re-applies it.
Show the user the token estimate (it is billed by the Hindsight server's LLM provider, and real extraction usage runs higher than the estimate). Wait for a yes.

- [ ] **Step 2: Ingest (live, 15-30 min)**

```bash
.venv/bin/python dataset/code/hindsight_loader.py --corpus dataset/memory_corpus.jsonl --bank supply-chain-memory \
  --base-url http://localhost:8888 --batch 25 --concurrency 5 --log runtime/retention_log.jsonl
```
Expected: progress lines `retained 25/2201 … retained 2201/2201`, with errors at 0 or close to it. The loader first tries `create_bank`, which fails harmlessly because Task 1A already created the bank. If errors > 0, re-run the same command: it resumes and skips docs logged `ok`.

- [ ] **Step 3: Re-apply the bank configuration and run the full Stage 0 gate**

Run: `.venv/bin/python -m agent.bank_setup && .venv/bin/python -m agent.check_connectivity`
Expected:
```
... directives: 0 created, 8 already present
DB ok: inventory_supply_chain_v1_1_ext.sqlite disruption_events=2696
Ingestion ok: 2201/2201 memory docs retained
Hindsight ok: N results; known fact found; first hit document_id=DOC00.... mentioned_at=... context=...
```
Write down whether hits carry `document_id` (it drives Task 5's resolution path), and what `mentioned_at` looks like (e.g. whether it equals the doc timestamp). If `check_hindsight` still fails, stop and show the user the error.

- [ ] **Step 4: Write the failing Mental Model tests**

`tests/test_mental_models.py`:
```python
import json

from agent.mental_models import (PATTERN_MODELS, TAG, create_pattern_models, delete_pattern_models,
                                 list_pattern_models, model_name)

COUNTRY = {"CN": "China"}
MONTH = {4: "April"}


class FakeMMClient:
    def __init__(self):
        self.models, self.updates = {}, []

    def create_mental_model(self, bank_id, name, source_query, tags):
        mid = f"mm{len(self.models) + 1}"
        self.models[mid] = {"id": mid, "name": name, "source_query": source_query, "tags": tags, "content": "..."}
        return {"id": mid}

    def update_mental_model(self, bank_id, mental_model_id, **kw):
        self.updates.append((mental_model_id, kw))

    def list_mental_models(self, bank_id, detail="content"):
        return list(self.models.values())

    def delete_mental_model(self, bank_id, mental_model_id):
        del self.models[mental_model_id]


def test_one_model_per_planted_pattern(base_settings):
    patterns = json.loads((base_settings.dataset_dir / "planted_patterns.json").read_text())["patterns"]
    assert sorted(PATTERN_MODELS) == sorted(p["pattern_id"] for p in patterns)
    for p in patterns:
        query = PATTERN_MODELS[p["pattern_id"]][1]
        for v in p["entities"].values():
            if isinstance(v, str):
                assert v in query or COUNTRY.get(v, "\0") in query, (p["pattern_id"], v)
            elif isinstance(v, int):
                assert MONTH[v] in query
        assert "%" not in query  # standing questions, not the answer key's effect sizes


def test_create_is_idempotent_and_tagged():
    c = FakeMMClient()
    assert len(create_pattern_models(c, "b")) == 12
    assert create_pattern_models(c, "b") == []
    assert all(m["tags"][0] == TAG for m in c.models.values())
    assert all(kw == {"trigger": {"refresh_after_consolidation": True}} for _, kw in c.updates)
    assert model_name("P01") in list_pattern_models(c, "b")


def test_delete_only_removes_pattern_models():
    c = FakeMMClient()
    c.create_mental_model("b", "Team notes", "unrelated", ["other"])
    create_pattern_models(c, "b")
    assert len(delete_pattern_models(c, "b")) == 12
    assert [m["name"] for m in c.models.values()] == ["Team notes"]
```

- [ ] **Step 5: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_mental_models.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'agent.mental_models'`

- [ ] **Step 6: Implement `agent/mental_models.py`**

```python
"""The 12 planted-pattern Mental Models.

Each one is a standing question that names the entity and the behaviour to watch, with no effect sizes. Hindsight
answers it from the ingested memory and refreshes it after consolidation. All are tagged `planted-pattern` and named
"[Pxx] ...", so they can be listed, excluded from reflect (MENTAL_MODELS_IN_REFLECT=0) or deleted as a group."""
from __future__ import annotations

import argparse

from agent.bank_setup import make_client
from agent.config import load_settings

TAG = "planted-pattern"
PATTERN_MODELS = {
    "P01": ("SUP0247 Nov-Dec delivery reliability",
            "How late do SUP0247 raw-material deliveries promised in November and December arrive compared with "
            "other months, and what buffer should planners use for SUP0247 orders in Q4?"),
    "P02": ("February delays from China-based suppliers",
            "Do raw-material lots from China-based suppliers promised in February arrive later than in other months, "
            "by how much, and how should critical February items be ordered?"),
    "P03": ("RM0046 shortages after SUP0223 price increases",
            "Does RM0046 (single-sourced from SUP0223) run short in the weeks after SUP0223 price increases, how "
            "often has that happened, and what should we do when a new increase is announced?"),
    "P04": ("SUP0091 reliability by order size",
            "How does SUP0091's on-time delivery differ between small and large raw-material orders, and how should "
            "large orders to SUP0091 be placed?"),
    "P05": ("W005 expedite cost",
            "How do expedite costs for finished goods delivered into warehouse W005 compare with expedites into "
            "other warehouses, and how should expedites to W005 be handled?"),
    "P06": ("P02 quarter-start maintenance overruns",
            "How often are production runs at plant P02 planned in the first two weeks of a quarter delayed by "
            "maintenance overruns, and how should P02 be scheduled?"),
    "P07": ("RM0016 as substitute for RM0022 at P02",
            "What happened to incoming QA rejects of RM0016 at plant P02 after product IP00973 switched from RM0022 "
            "to RM0016, and what inspection or qualification is needed?"),
    "P08": ("P03 as a transfer donor plant",
            "How often do inter-plant raw-material transfers out of plant P03 leave P03 below safety stock compared "
            "with other donor plants, and which plants should be preferred as donors?"),
    "P09": ("SUP0237 recovery-date reliability",
            "When SUP0237 re-promises a late raw-material lot, how often does it miss the new date compared with other "
            "suppliers, and how should planners treat its revised dates?"),
    "P10": ("Forecast bias for consumables",
            "How do demand forecasts for the consumables category compare with actual demand, relative to other "
            "categories, and how should consumables forecasts be adjusted?"),
    "P11": ("SUP0179 lead-time trend",
            "How have actual lead times from SUP0179 changed month by month since January 2025, and what should we "
            "do about it?"),
    "P12": ("SUP0005 April finished-goods lateness",
            "How often are SUP0005 finished-goods purchase orders due in April late compared with other months, and "
            "how should April deliveries from SUP0005 be handled?"),
}


def model_name(pid: str) -> str:
    return f"[{pid}] {PATTERN_MODELS[pid][0]}"


def _get(obj, *names):
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v is not None:
            return v
    return None


def _items(resp) -> list:
    return resp if isinstance(resp, list) else (_get(resp, "items", "mental_models") or [])


def list_pattern_models(client, bank_id: str) -> dict[str, dict]:
    names = {model_name(p) for p in PATTERN_MODELS}
    out = {}
    for m in _items(client.list_mental_models(bank_id=bank_id, detail="content")):
        name = _get(m, "name")
        if name in names:
            out[name] = {"id": _get(m, "id", "mental_model_id"), "name": name, "content": _get(m, "content")}
    return out


def create_pattern_models(client, bank_id: str) -> list[str]:
    have = list_pattern_models(client, bank_id)
    created = []
    for pid, (_, query) in PATTERN_MODELS.items():
        name = model_name(pid)
        if name in have:
            continue
        resp = client.create_mental_model(bank_id=bank_id, name=name, source_query=query, tags=[TAG, pid])
        client.update_mental_model(bank_id=bank_id, mental_model_id=_get(resp, "id", "mental_model_id"),
                                   trigger={"refresh_after_consolidation": True})
        created.append(name)
    return created


def delete_pattern_models(client, bank_id: str) -> list[str]:
    gone = []
    for name, m in list_pattern_models(client, bank_id).items():
        client.delete_mental_model(bank_id=bank_id, mental_model_id=m["id"])
        gone.append(name)
    return gone


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["create", "list", "delete"])
    a = ap.parse_args(argv)
    s = load_settings()
    client = make_client(s)
    if a.action == "create":
        print("\n".join(create_pattern_models(client, s.hindsight_bank_id)) or "all 12 already exist")
    elif a.action == "delete":
        print("\n".join(delete_pattern_models(client, s.hindsight_bank_id)) or "none to delete")
    else:
        for name, m in sorted(list_pattern_models(client, s.hindsight_bank_id).items()):
            print(f"{name}\n  {(m['content'] or '(content not generated yet)')[:300]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mental_models.py -v`
Expected: 3 passed

- [ ] **Step 8: Create the Mental Models for real**

Run: `.venv/bin/python -m agent.mental_models create && .venv/bin/python -m agent.mental_models list`
Expected: 12 names created; `list` shows 12 entries. Content may read "(content not generated yet)" until Hindsight finishes generating. Re-run `list` after a few minutes and paste 2-3 contents into the task notes. The P01 content should mention late November-December deliveries, or say memory is insufficient. Either way, record it: it is what the Mental Models actually learned from the corpus.

- [ ] **Step 9: Commit**

```bash
git add agent/mental_models.py tests/test_mental_models.py agent/check_connectivity.py
git commit -m "feat: ingest memory corpus and create 12 pattern mental models; Stage 0 green

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: As-of database layer and append-only live tables

**Files:**
- Create: `agent/db.py`
- Modify: `tests/conftest.py` (add `db_factory`)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `Settings.db_path`, `Settings.live_db_path`.
- Produces: `agent.db.init_live(live_path: Path) -> None`; `open_live_writer(live_path) -> sqlite3.Connection` (row_factory=Row); `live_token(live_path) -> str`; `live_memory_ids(live_path) -> set[str]` (each `"{decision_id}.{token}"`); `asof_views(as_of: str) -> dict[str, str]`; `connect(db_path: Path, live_path: Path, as_of: str) -> sqlite3.Connection` (row_factory=Row, read-only, `live` attached, TEMP views installed). Column sets of the views `decisions` and `commitments` include a `source` column (`historical` | `live`).

- [ ] **Step 1: Add the `db_factory` fixture to `tests/conftest.py`**

Append:
```python
from agent.db import connect


@pytest.fixture
def db_factory(settings):
    opened = []

    def make(as_of):
        con = connect(settings.db_path, settings.live_db_path, as_of)
        opened.append(con)
        return con

    yield make
    for con in opened:
        con.close()
```

- [ ] **Step 2: Write the failing tests**

`tests/test_db.py`:
```python
import sqlite3

import pytest

from agent.db import asof_views, live_memory_ids, live_token, open_live_writer


def _one(con, sql, *p):
    return con.execute(sql, p).fetchone()


def test_dataset_is_read_only(db_factory):
    con = db_factory("2025-01-13")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM main.decisions")


def test_invalid_as_of_rejected(settings):
    with pytest.raises(ValueError):
        asof_views("2025-1-13")


def test_events_hidden_after_as_of(db_factory):
    con = db_factory("2023-06-30")
    assert _one(con, "SELECT MAX(detected_at) FROM disruption_events")[0] <= "2023-06-30"
    assert _one(con, "SELECT COUNT(*) FROM event_links l JOIN main.disruption_events d ON d.event_id = l.dst_event_id "
                     "WHERE d.detected_at > '2023-06-30'")[0] == 0


def test_decision_outcome_masked_until_assessed(db_factory):
    # DEC00021 decided 2023-04-14, outcome assessed 2023-04-26 (partial, actual cost 655.89)
    before, after = db_factory("2023-04-25"), db_factory("2023-04-26")
    row = _one(before, "SELECT actual_cost, outcome_label, lesson_text FROM decisions WHERE decision_id='DEC00021'")
    assert tuple(row) == (None, None, None)
    row = _one(after, "SELECT outcome_label, source FROM decisions WHERE decision_id='DEC00021'")
    assert tuple(row) == ("partial", "historical")
    assert _one(db_factory("2023-04-13"), "SELECT COUNT(*) FROM decisions WHERE decision_id='DEC00021'")[0] == 0


def test_commitment_status_as_of(db_factory):
    # CMT00560: volume commitment with SUP0169 made 2024-12-26, breached 2025-12-30
    row = _one(db_factory("2025-01-13"), "SELECT status, resolved_at, evidence_ids FROM commitments WHERE commitment_id='CMT00560'")
    assert tuple(row) == ("open", None, None)
    assert _one(db_factory("2025-12-31"), "SELECT status FROM commitments WHERE commitment_id='CMT00560'")[0] == "breached"


def test_rpo_expected_at_follows_revisions(db_factory):
    # RPO003179: slipped to 2023-06-22 on 2023-06-02, expedited to 2023-06-08 on 2023-06-04, received 2023-06-08
    q = "SELECT expected_at, received_at, status FROM rm_purchase_orders WHERE rm_purchase_order_id='RPO003179'"
    assert tuple(_one(db_factory("2023-06-03"), q)) == ("2023-06-22", None, "open")
    assert tuple(_one(db_factory("2023-06-05"), q)) == ("2023-06-08", None, "open")
    assert tuple(_one(db_factory("2023-06-08"), q)) == ("2023-06-08", "2023-06-08", "received")


def test_scorecard_complete_months_only(db_factory):
    con = db_factory("2025-10-14")
    months = [r[0] for r in con.execute("SELECT month FROM supplier_scorecard_monthly WHERE supplier_id='SUP0247'")]
    assert months and max(months) <= "2025-09-01"


def test_live_rows_are_unioned_and_append_only(settings, db_factory):
    w = open_live_writer(settings.live_db_path)
    with w:
        w.execute("INSERT INTO decisions_live VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("DECL00001", "LIVE:RPO023030", "SUP0247", "RM0083", "P01", "RPO023030", "2025-10-14",
                   "Decision Agent (prototype)", "switch_supplier", "[]", "switch_supplier", "why", 2.0, 0.0,
                   "[]", "[]", "run-x", "2026-09-29T00:00:00+00:00"))
    with pytest.raises(sqlite3.DatabaseError):
        with w:
            w.execute("UPDATE decisions_live SET rationale_text='x'")
    w.close()
    assert _one(db_factory("2025-10-14"), "SELECT source FROM decisions WHERE decision_id='DECL00001'")[0] == "live"
    assert _one(db_factory("2025-10-13"), "SELECT COUNT(*) FROM decisions WHERE decision_id='DECL00001'")[0] == 0
    token = live_token(settings.live_db_path)
    assert live_memory_ids(settings.live_db_path) == {f"DECL00001.{token}"}
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_db.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'agent.db'`

- [ ] **Step 4: Implement `agent/db.py`**

```python
"""SQLite access with the as-of rule built in.

The dataset DB is opened read-only. An attached `live` DB holds the append-only tables the agent writes
at runtime. TEMP views named like the dataset tables shadow them (SQLite resolves unqualified names
temp -> main -> attached), so any unqualified query only returns rows known on `as_of`, with
outcome/receipt/resolution columns masked until the date they became known.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from pathlib import Path

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions_live (
  decision_id TEXT PRIMARY KEY,
  event_ref TEXT,
  supplier_id TEXT, rm_id TEXT, plant_id TEXT, rpo_id TEXT,
  decided_at TEXT NOT NULL,
  decided_by_role TEXT NOT NULL,
  decision_type TEXT NOT NULL,
  options_considered_json TEXT NOT NULL,
  chosen_option TEXT NOT NULL,
  rationale_text TEXT NOT NULL,
  expected_cost REAL,
  expected_stockout_days REAL,
  evidence_doc_ids TEXT NOT NULL,
  evidence_record_ids TEXT NOT NULL,
  run_id TEXT NOT NULL,
  logged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commitments_live (
  commitment_id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL REFERENCES decisions_live(decision_id),
  counterparty_type TEXT NOT NULL,
  counterparty_id TEXT NOT NULL,
  made_by_role TEXT NOT NULL,
  made_at TEXT NOT NULL,
  commitment_text TEXT NOT NULL,
  quantity REAL,
  due_date TEXT NOT NULL,
  penalty_or_credit TEXT,
  logged_at TEXT NOT NULL
);
""" + "".join(
    f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
    f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;\n"
    for t in ("decisions_live", "commitments_live") for op in ("UPDATE", "DELETE"))

_DEC_OUTCOME = ("actual_cost", "actual_stockout_days", "outcome_label", "outcome_assessed_at",
                "lesson_text", "footprint_ids", "outcome_attribution")
_RUN_ACTUALS = ("actual_start", "produced_qty", "delay_days", "delay_reason_code", "rm_shortage_rm_id")


def init_live(live_path: Path) -> None:
    live_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(live_path)
    try:
        with con:
            con.executescript(LIVE_SCHEMA)
            con.execute("INSERT OR IGNORE INTO meta VALUES ('token', ?)", (secrets.token_hex(4),))
    finally:
        con.close()


def open_live_writer(live_path: Path) -> sqlite3.Connection:
    init_live(live_path)
    con = sqlite3.connect(live_path)
    con.row_factory = sqlite3.Row
    return con


def live_token(live_path: Path) -> str:
    init_live(live_path)
    con = sqlite3.connect(live_path)
    try:
        return con.execute("SELECT value FROM meta WHERE key='token'").fetchone()[0]
    finally:
        con.close()


def live_memory_ids(live_path: Path) -> set[str]:
    """Hindsight document_ids of decisions logged in this live DB ("DECL00001.<token>")."""
    token = live_token(live_path)
    con = sqlite3.connect(live_path)
    try:
        return {f"{r[0]}.{token}" for r in con.execute("SELECT decision_id FROM decisions_live")}
    finally:
        con.close()


def _masked(cols, cond: str) -> str:
    return ", ".join(f"CASE WHEN {cond} THEN {c} END AS {c}" for c in cols)


def asof_views(as_of: str) -> dict[str, str]:
    if not DATE_RE.match(as_of or ""):
        raise ValueError(f"as_of must be YYYY-MM-DD, got {as_of!r}")
    A = f"'{as_of}'"
    resolved = "NULLIF(resolved_at, '')"
    concluded = "NULLIF(concluded_at, '')"
    return {
        "disruption_events": f"SELECT * FROM main.disruption_events WHERE detected_at <= {A}",
        "event_impacts": f"""SELECT i.* FROM main.event_impacts i JOIN main.disruption_events e USING (event_id)
                              WHERE e.detected_at <= {A}""",
        "event_links": f"""SELECT l.* FROM main.event_links l
                            JOIN main.disruption_events s ON s.event_id = l.src_event_id
                            JOIN main.disruption_events d ON d.event_id = l.dst_event_id
                            WHERE s.detected_at <= {A} AND d.detected_at <= {A}""",
        "decisions": f"""
            SELECT decision_id, event_id, decided_at, decided_by_role, decision_type, options_considered_json,
                   chosen_option, rationale_text, expected_cost, expected_stockout_days,
                   {_masked(_DEC_OUTCOME, f"NULLIF(outcome_assessed_at, '') <= {A}")}, 'historical' AS source
            FROM main.decisions WHERE decided_at <= {A}
            UNION ALL
            SELECT decision_id, event_ref, decided_at, decided_by_role, decision_type, options_considered_json,
                   chosen_option, rationale_text, expected_cost, expected_stockout_days,
                   NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'live'
            FROM live.decisions_live WHERE decided_at <= {A}""",
        "commitments": f"""
            SELECT commitment_id, decision_id, negotiation_id, counterparty_type, counterparty_id, made_by_role,
                   made_at, commitment_text, quantity, due_date, penalty_or_credit,
                   CASE WHEN {resolved} <= {A} THEN status ELSE 'open' END AS status,
                   CASE WHEN {resolved} <= {A} THEN resolved_at END AS resolved_at,
                   CASE WHEN {resolved} <= {A} THEN evidence_ids END AS evidence_ids,
                   'historical' AS source
            FROM main.commitments WHERE made_at <= {A}
            UNION ALL
            SELECT commitment_id, decision_id, NULL, counterparty_type, counterparty_id, made_by_role, made_at,
                   commitment_text, quantity, due_date, penalty_or_credit, 'open', NULL, NULL, 'live'
            FROM live.commitments_live WHERE made_at <= {A}""",
        "negotiations": f"""
            SELECT negotiation_id, supplier_id, rm_id, started_at, topic, our_ask, their_offer, contract_id,
                   CASE WHEN {concluded} <= {A} THEN concluded_at END AS concluded_at,
                   CASE WHEN {concluded} <= {A} THEN concessions_json END AS concessions_json,
                   CASE WHEN {concluded} <= {A} THEN final_terms END AS final_terms,
                   CASE WHEN {concluded} <= {A} THEN outcome ELSE 'open' END AS outcome
            FROM main.negotiations WHERE started_at <= {A}""",
        "supplier_scorecard_monthly": f"""
            SELECT s.supplier_id, s.month, s.po_count, s.otif_rate, s.avg_delay_days, s.fill_rate, s.quality_ppm,
                   s.commitments_made,
                   (SELECT COUNT(*) FROM main.commitments c
                     WHERE c.counterparty_id = s.supplier_id AND substr(c.made_at, 1, 7) = substr(s.month, 1, 7)
                       AND c.status = 'fulfilled' AND NULLIF(c.resolved_at, '') <= {A}) AS commitments_kept
            FROM main.supplier_scorecard_monthly s WHERE date(s.month, '+1 month') <= {A}""",
        "rm_purchase_orders": f"""
            SELECT o.rm_purchase_order_id, o.supplier_id, o.plant_id, o.order_type, o.ordered_at, o.promised_at,
                   COALESCE((SELECT r.new_expected_at FROM main.rm_po_revisions r
                             WHERE r.rm_po_id = o.rm_purchase_order_id AND r.revised_at <= {A}
                             ORDER BY r.revised_at DESC, r.revision_id DESC LIMIT 1), o.promised_at) AS expected_at,
                   CASE WHEN NULLIF(o.received_at, '') <= {A} THEN o.received_at END AS received_at,
                   CASE WHEN NULLIF(o.received_at, '') <= {A} THEN o.status
                        WHEN o.status = 'cancelled' THEN 'cancelled' ELSE 'open' END AS status
            FROM main.rm_purchase_orders o WHERE o.ordered_at <= {A}""",
        "rm_purchase_order_lines": f"""
            SELECT l.rm_purchase_order_line_id, l.rm_purchase_order_id, l.rm_id, l.quantity_ordered,
                   CASE WHEN NULLIF(o.received_at, '') <= {A} THEN l.quantity_received END AS quantity_received,
                   l.unit_cost
            FROM main.rm_purchase_order_lines l JOIN main.rm_purchase_orders o USING (rm_purchase_order_id)
            WHERE o.ordered_at <= {A}""",
        "rm_po_revisions": f"SELECT * FROM main.rm_po_revisions WHERE revised_at <= {A}",
        "rm_inventory_movements": f"SELECT * FROM main.rm_inventory_movements WHERE movement_at <= {A}",
        "rm_inventory_snapshots_weekly": f"SELECT * FROM main.rm_inventory_snapshots_weekly WHERE snapshot_date <= {A}",
        "production_runs": f"""
            SELECT production_run_id, plant_id, product_id, purchase_order_line_id, planned_start, planned_qty,
                   {_masked(_RUN_ACTUALS, f"NULLIF(actual_start, '') <= {A}")}
            FROM main.production_runs""",
        "product_demand_weekly": f"SELECT * FROM main.product_demand_weekly WHERE week_start <= {A}",
        "purchase_orders": f"""
            SELECT purchase_order_id, supplier_id, warehouse_id, ordered_at, expected_at,
                   CASE WHEN NULLIF(received_at, '') <= {A} THEN received_at END AS received_at,
                   CASE WHEN NULLIF(received_at, '') <= {A} THEN status
                        WHEN status = 'cancelled' THEN 'cancelled' ELSE 'open' END AS status
            FROM main.purchase_orders WHERE ordered_at <= {A}""",
        "purchase_order_lines": f"""
            SELECT l.purchase_order_line_id, l.purchase_order_id, l.product_id, l.quantity_ordered,
                   CASE WHEN NULLIF(o.received_at, '') <= {A} THEN l.quantity_received END AS quantity_received,
                   l.unit_cost
            FROM main.purchase_order_lines l JOIN main.purchase_orders o USING (purchase_order_id)
            WHERE o.ordered_at <= {A}""",
        "inventory_movements": f"SELECT * FROM main.inventory_movements WHERE movement_at <= {A}",
    }


def connect(db_path: Path, live_path: Path, as_of: str) -> sqlite3.Connection:
    views = asof_views(as_of)  # validates as_of before anything is opened
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} not found - run `python -m agent.setup_data`")
    init_live(live_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("ATTACH DATABASE ? AS live", (f"file:{live_path}?mode=ro",))
    for name, sql in views.items():
        con.execute(f"CREATE TEMP VIEW {name} AS {sql}")
    con.execute("PRAGMA query_only = ON")
    return con
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_db.py -v`
Expected: 8 passed. If `test_scorecard_complete_months_only` fails with an empty list, `month` is not stored as `YYYY-MM-DD`. Check with `SELECT month FROM main.supplier_scorecard_monthly LIMIT 3` and adapt the `date(s.month, '+1 month')` expression to the stored format.

- [ ] **Step 6: Commit**

```bash
git add agent/db.py tests/conftest.py tests/test_db.py
git commit -m "feat: as-of SQLite views and append-only live tables

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: SQL tools - guarded free-form query + curated lookups

**Files:**
- Create: `agent/sql_tools.py`
- Test: `tests/test_sql_tools.py`

**Interfaces:**
- Consumes: `connect(...)` connections from Task 2 (unqualified names = as-of views; `live.decisions_live` is readable).
- Produces:
  - `SQLGuardError(ValueError)`
  - `check_query(query: str) -> str`
  - `run_sql(con, query: str, *, max_rows=200, timeout_s=5.0) -> dict` with keys `columns`, `rows`, `truncated`
  - `open_commitments(con, counterparty_ids: list[str|None], as_of: str) -> list[dict]` with keys `commitment_id, counterparty_type, counterparty_id, made_at, due_date, commitment_text, penalty_or_credit, source, is_volume_commitment`
  - `supplier_scorecard(con, supplier_id: str, months=6) -> list[dict]`
  - `prior_events(con, supplier_id, rm_id, plant_id, limit=12) -> list[dict]`
  - `event_links_for(con, event_ids: list[str]) -> list[dict]`
  - `prior_decisions(con, supplier_id, rm_id, as_of: str, limit=8) -> list[dict]` with keys `decision_id, event_id, decided_at, decision_type, chosen_option, expected_cost, expected_stockout_days, actual_cost, actual_stockout_days, outcome_label, lesson_text, outcome_attribution, source`
  - `record_exists(con, record_id: str) -> bool | None` (None = id format not recognised)
  - `schema_summary(con) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_sql_tools.py`:
```python
import sqlite3

import pytest

from agent import sql_tools as st


@pytest.mark.parametrize("q", [
    "DELETE FROM decisions",
    "SELECT 1; DROP TABLE decisions",
    "PRAGMA table_info(decisions)",
    "SELECT * FROM main.decisions",
    "SELECT * FROM live.decisions_live",
    "select * from  MAIN . commitments",
    "",
])
def test_check_query_rejects(q):
    with pytest.raises(st.SQLGuardError):
        st.check_query(q)


def test_check_query_allows_keywords_inside_string_literals():
    q = "SELECT commitment_id FROM commitments WHERE commitment_text LIKE '%update the delete plan%';"
    assert st.check_query(q).endswith("'%update the delete plan%'")


def test_run_sql_respects_as_of_and_row_cap(db_factory):
    con = db_factory("2023-06-30")
    out = st.run_sql(con, "SELECT event_id, detected_at FROM disruption_events ORDER BY detected_at", max_rows=5)
    assert out["columns"] == ["event_id", "detected_at"]
    assert len(out["rows"]) == 5 and out["truncated"] is True
    latest = st.run_sql(con, "SELECT MAX(detected_at) FROM disruption_events")["rows"][0][0]
    assert latest <= "2023-06-30"


def test_run_sql_timeout(db_factory):
    con = db_factory("2025-12-31")
    heavy = "SELECT COUNT(*) FROM rm_inventory_movements a JOIN rm_inventory_movements b ON a.rm_id = b.rm_id"
    with pytest.raises(sqlite3.OperationalError):
        st.run_sql(con, heavy, timeout_s=0.05)
    assert st.run_sql(con, "SELECT 1")["rows"] == [[1]]  # handler removed after the timeout


def test_open_commitments_match_holdout_gold(db_factory):
    con = db_factory("2025-10-14")  # HS05: SUP0247 / P01
    ids = [c["commitment_id"] for c in st.open_commitments(con, ["SUP0247", "P01"], "2025-10-14")]
    assert sorted(ids) == ["CMT00785", "CMT00800", "CMT00817", "CMT00825", "CMT00828", "CMT00835"]


def test_open_commitments_flags_volume_commitment(db_factory):
    con = db_factory("2025-01-13")
    rows = {c["commitment_id"]: c for c in st.open_commitments(con, ["SUP0169", "P03", None], "2025-01-13")}
    assert rows["CMT00560"]["is_volume_commitment"] == 1


def test_prior_decisions_and_events(db_factory):
    con = db_factory("2025-10-14")
    decs = st.prior_decisions(con, "SUP0247", "RM0083", "2025-10-14")
    assert decs and all(d["decided_at"] <= "2025-10-14" for d in decs)
    # known outcomes rank before pending ones, so the trap precedent DEC00353 (accept_delay, success) is included
    assert "DEC00353" in [d["decision_id"] for d in decs[:6]]
    known = [d["outcome_label"] is not None for d in decs]
    assert known == sorted(known, reverse=True)
    evs = st.prior_events(con, "SUP0247", "RM0083", "P01")
    assert evs and all(e["detected_at"] <= "2025-10-14" for e in evs)
    links = st.event_links_for(con, [e["event_id"] for e in evs])
    assert all({"src_event_id", "dst_event_id", "link_type"} <= set(l) for l in links)


def test_supplier_scorecard(db_factory):
    rows = st.supplier_scorecard(db_factory("2025-10-14"), "SUP0247", months=3)
    assert len(rows) == 3 and rows[0]["month"] >= rows[-1]["month"]


@pytest.mark.parametrize("rid,expected", [
    ("EVT01987", True), ("DEC00021", True), ("CMT00560", True), ("RPO016183", True), ("REV000864", True),
    ("PR0016522", True), ("SUP0169", True), ("RM0100", True), ("P03", True), ("W012", True),
    ("EVT99999", False), ("DECL00001", False), ("RM0100|P03|2025-01-12", None), ("banana", None),
])
def test_record_exists(db_factory, rid, expected):
    assert st.record_exists(db_factory("2025-12-31"), rid) is expected


def test_record_exists_respects_as_of(db_factory):
    assert st.record_exists(db_factory("2024-01-01"), "EVT01987") is False


def test_schema_summary_lists_tables(db_factory):
    s = st.schema_summary(db_factory("2025-12-31"))
    assert "commitments(" in s and "rm_purchase_orders(" in s and "decisions_live" not in s
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_sql_tools.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'agent.sql_tools'`

- [ ] **Step 3: Implement `agent/sql_tools.py`**

```python
"""Read-only SQL for the agent: a guarded free-form `run_sql` plus curated lookups.

All queries run on an as-of connection from agent.db.connect: unqualified table names resolve to the
as-of TEMP views, which is why the LLM may not use schema-qualified names.
"""
from __future__ import annotations

import re
import sqlite3
import time

_LITERAL = re.compile(r"'(?:[^']|'')*'")
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|create|alter|replace|attach|detach|pragma|vacuum|"
                        r"reindex|analyze|begin|commit|rollback|savepoint)\b", re.I)
_QUALIFIED = re.compile(r"\b(main|live|temp)\s*\.", re.I)

_RECORD_TABLES = [  # longest prefix first
    ("DECL", "decisions", "decision_id"), ("CMTL", "commitments", "commitment_id"),
    ("RPOL", "rm_purchase_order_lines", "rm_purchase_order_line_id"),
    ("RPO", "rm_purchase_orders", "rm_purchase_order_id"), ("REV", "rm_po_revisions", "revision_id"),
    ("EVT", "disruption_events", "event_id"), ("DEC", "decisions", "decision_id"),
    ("CMT", "commitments", "commitment_id"), ("NEG", "negotiations", "negotiation_id"),
    ("CTR", "contracts", "contract_id"), ("CAT", "rm_supplier_catalog", "catalog_id"),
    ("POL", "purchase_order_lines", "purchase_order_line_id"), ("PR", "production_runs", "production_run_id"),
    ("PO", "purchase_orders", "purchase_order_id"), ("RT", "rm_inventory_movements", "transfer_id"),
    ("SUP", "suppliers", "supplier_id"), ("RM", "raw_materials", "rm_id"), ("IP", "products", "product_id"),
    ("W", "warehouses", "warehouse_id"), ("P", "plants", "plant_id"),
]


class SQLGuardError(ValueError):
    """The query is not a single read-only statement over the as-of views."""


def check_query(query: str) -> str:
    q = (query or "").strip().rstrip(";").strip()
    if not q:
        raise SQLGuardError("empty query")
    bare = _LITERAL.sub("''", q)
    if ";" in bare:
        raise SQLGuardError("only one statement is allowed")
    if not re.match(r"(?is)^(select|with)\b", bare):
        raise SQLGuardError("only SELECT / WITH queries are allowed")
    if m := _FORBIDDEN.search(bare):
        raise SQLGuardError(f"keyword {m.group(1).upper()} is not allowed (read-only)")
    if _QUALIFIED.search(bare):
        raise SQLGuardError("schema-qualified names (main./live./temp.) are not allowed; use plain table "
                            "names so the as-of filter applies")
    return q


def run_sql(con: sqlite3.Connection, query: str, *, max_rows: int = 200, timeout_s: float = 5.0) -> dict:
    q = check_query(query)
    deadline = time.monotonic() + timeout_s
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    try:
        cur = con.execute(q)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
    finally:
        con.set_progress_handler(None, 0)
    return {"columns": cols, "rows": [list(r) for r in rows[:max_rows]], "truncated": len(rows) > max_rows}


def _rows(con, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def open_commitments(con, counterparty_ids, as_of: str) -> list[dict]:
    ids = [i for i in dict.fromkeys(counterparty_ids) if i]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    return _rows(con, f"""
        SELECT commitment_id, counterparty_type, counterparty_id, made_at, due_date, commitment_text,
               penalty_or_credit, source, commitment_text LIKE 'We order at least%' AS is_volume_commitment
        FROM commitments
        WHERE status = 'open' AND made_at <= ? AND counterparty_id IN ({ph})
        ORDER BY due_date, commitment_id""", (as_of, *ids))


def supplier_scorecard(con, supplier_id: str, months: int = 6) -> list[dict]:
    return _rows(con, """SELECT * FROM supplier_scorecard_monthly WHERE supplier_id = ?
                         ORDER BY month DESC LIMIT ?""", (supplier_id, months))


def prior_events(con, supplier_id, rm_id, plant_id, limit: int = 12) -> list[dict]:
    return _rows(con, """
        SELECT e.event_id, e.event_type, e.title, e.detected_at, e.start_date, e.end_date, e.severity,
               e.root_cause_code, e.origin_entity_type, e.origin_entity_id,
               (e.origin_entity_id = :rm OR EXISTS (SELECT 1 FROM event_impacts i
                                                    WHERE i.event_id = e.event_id AND i.entity_id = :rm)) AS same_rm
        FROM disruption_events e
        WHERE e.origin_entity_id IN (:sup, :rm, :plant)
           OR EXISTS (SELECT 1 FROM event_impacts i WHERE i.event_id = e.event_id AND i.entity_id = :rm)
        ORDER BY same_rm DESC, e.detected_at DESC LIMIT :lim""",
                 {"sup": supplier_id, "rm": rm_id, "plant": plant_id, "lim": limit})


def event_links_for(con, event_ids: list[str]) -> list[dict]:
    if not event_ids:
        return []
    ph = ",".join("?" * len(event_ids))
    return _rows(con, f"""SELECT src_event_id, dst_event_id, link_type FROM event_links
                          WHERE src_event_id IN ({ph}) OR dst_event_id IN ({ph})
                          ORDER BY src_event_id, dst_event_id""", (*event_ids, *event_ids))


def prior_decisions(con, supplier_id, rm_id, as_of: str, limit: int = 8) -> list[dict]:
    """This deployment's own live decisions first, then historical decisions whose outcome is already known
    on as_of (they carry lessons), then decisions still pending an outcome; newest first within each group.
    Pure recency would hide the informative precedents: e.g. on 2025-10-14 the only SUP0247 accept_delay
    with a known (successful) outcome is DEC00353, the 9th most recent decision."""
    return _rows(con, """
        SELECT * FROM (
            SELECT d.decision_id, d.event_id, d.decided_at, d.decision_type, d.chosen_option, d.expected_cost,
                   d.expected_stockout_days, d.actual_cost, d.actual_stockout_days, d.outcome_label, d.lesson_text,
                   d.outcome_attribution, d.source
            FROM decisions d JOIN disruption_events e ON e.event_id = d.event_id
            WHERE d.source = 'historical'
              AND (e.origin_entity_id IN (:sup, :rm)
                   OR EXISTS (SELECT 1 FROM event_impacts i WHERE i.event_id = e.event_id AND i.entity_id = :rm))
            UNION ALL
            SELECT decision_id, event_ref, decided_at, decision_type, chosen_option, expected_cost,
                   expected_stockout_days, NULL, NULL, NULL, NULL, NULL, 'live'
            FROM live.decisions_live
            WHERE decided_at <= :as_of AND (supplier_id = :sup OR rm_id = :rm))
        ORDER BY (source = 'live') DESC, (outcome_label IS NULL), decided_at DESC LIMIT :lim""",
                 {"sup": supplier_id, "rm": rm_id, "as_of": as_of, "lim": limit})


def record_exists(con, record_id: str) -> bool | None:
    rid = (record_id or "").strip()
    if not re.fullmatch(r"[A-Z]+\d+", rid):
        return None
    for prefix, table, col in _RECORD_TABLES:
        if rid.startswith(prefix) and rid[len(prefix):].isdigit():
            return con.execute(f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (rid,)).fetchone() is not None
    return None


def schema_summary(con) -> str:
    names = [r[0] for r in con.execute(
        "SELECT name FROM main.sqlite_master WHERE type = 'table' AND name <> 'dataset_info' ORDER BY name")]
    lines = []
    for n in names:
        cols = [r[1] for r in con.execute(f"PRAGMA main.table_info({n})")]
        lines.append(f"{n}({', '.join(cols)})")
    lines.append("decisions / commitments also contain this deployment's live rows (column source = 'live').")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_sql_tools.py -v`
Expected: all pass. If `test_run_sql_timeout` does not raise, the query is finishing too fast. Swap it for a triple self-join on `rm_inventory_movements`.

- [ ] **Step 5: Commit**

```bash
git add agent/sql_tools.py tests/test_sql_tools.py
git commit -m "feat: guarded read-only SQL tool and curated as-of lookups

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Deterministic simulator (ported from the generator) with parity tests

This is Stage 1's centrepiece. The option logic is a line-by-line port of `dataset/code/gen_eval.py::holdout_scenarios` (lines 202-323). It calls the cost functions in `dataset/code/consequences.py` directly instead of copying them. A throwaway port during planning matched all 14 scenarios' `candidate_actions`.

**Files:**
- Create: `agent/sim_rules.yaml`, `agent/simulate.py`, `agent/scenarios.py`
- Test: `tests/test_simulate.py`

**Interfaces:**
- Consumes: as-of connection (Task 2); `ROOT`.
- Produces:
  - `TAXONOMY: tuple[str, ...]` (9 decision types) and `SIMULATED: tuple[str, ...]` (6)
  - `SimulationError(ValueError)`
  - `ScenarioState` (frozen dataclass, fields below) with `to_dict() -> dict`
  - `load_rules() -> dict`
  - `build_state(con, *, rpo_id: str, run_ids: list[str], day0: str, rm_id: str | None = None, rules=None) -> ScenarioState`
  - `simulate_options(con, state, rules=None) -> list[dict]`. Each simulated option has keys `action, feasible, supported, arrival (ISO str), stockout_days, cost, risk, score, service_impact_units, note, breaches_commitments, evidence_record_ids`, plus `alt_supplier_id` / `donor_plant` / `substitute` where relevant. Infeasible options carry `action, feasible=False, supported=True, note`. Unsupported ones carry `action, feasible=False, supported=False, note`.
  - `simulate_action(con, action_type, *, rpo_id, run_ids, day0, rm_id=None) -> dict`
  - `best_option(options) -> dict | None`: lowest `(score, action)` among feasible, supported options with no breaches.
  - `agent.scenarios.load_holdout(settings) -> dict[str, dict]`; `holdout_context(scenario) -> dict` (keys `rpo_id, run_ids, rm_id`); `compare_to_gold(options, candidate_actions) -> list[str]`.

- [ ] **Step 1: Write `agent/sim_rules.yaml`**

```yaml
# Constants and risk rules copied from the dataset generator so the simulator reproduces its
# consequence model exactly (dataset/code/config.yaml + dataset/code/gen_eval.py::holdout_scenarios).
# The four entity rules mirror planted patterns P01, P02, P07 and P08: the generator's own
# consequence model uses them, so parity requires them. See README "Known limitations".
stockout_cost_rate_per_day: 0.004      # config.yaml memory.stockout_cost_rate_per_day
spot_price_premium: 0.12               # config.yaml rm_ops.spot_price_premium
transfer_cost_per_unit_value: 0.04     # config.yaml rm_ops.transfer_cost_per_unit_value
substitute_qa_days: 6
substitute_fixed_cost: 3000
cancel_fee_rate: 0.02
volume_breach_rate: 0.06
volume_breach_multiplier: 3
q4_slip_supplier_id: SUP0247           # P01: slips a further 7-14 days in Nov-Dec
feb_congestion_country: CN             # P02: February port congestion
risky_substitute_rm_id: RM0016         # P07: substitute with incoming-reject history
depleting_donor_plant_id: P03          # P08: transfers out of this plant leave it short
region_xy: {West: [0, 0], Southwest: [1, -1], Midwest: [2, 0], Northeast: [4, 1], Southeast: [3, -1], Canada: [3, 2]}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_simulate.py`:
```python
import pytest

from agent.scenarios import compare_to_gold, holdout_context, load_holdout
from agent.simulate import (SIMULATED, TAXONOMY, SimulationError, best_option, build_state, simulate_action,
                            simulate_options)


@pytest.fixture(scope="module")
def holdout(base_settings):
    return load_holdout(base_settings)


@pytest.mark.parametrize("sid", [f"HS{i:02d}" for i in range(1, 15)])
def test_options_match_generator(db_factory, holdout, sid):
    s = holdout[sid]
    con = db_factory(s["day0"])
    opts = simulate_options(con, build_state(con, day0=s["day0"], **holdout_context(s)))
    assert compare_to_gold(opts, s["candidate_actions"]) == []
    assert best_option(opts)["action"] == s["gold_best_action"]


def test_state_matches_report_numbers(db_factory, holdout):
    s = holdout["HS01"]  # "...needs 178.0 kg" ; slipped to 2025-10-30
    con = db_factory(s["day0"])
    st = build_state(con, day0=s["day0"], **holdout_context(s))
    assert round(st.req, 1) == 178.0
    assert st.eta.isoformat() == "2025-10-30"
    assert st.lot_rpo_ids == ("RPO023175", "RPO023521")


def test_cancel_po_flags_open_volume_commitment(db_factory):
    con = db_factory("2025-01-13")  # EVT01987: SUP0169 slipped RPO016183 (RM0100 at P03)
    opts = {o["action"]: o for o in simulate_options(
        con, build_state(con, rpo_id="RPO016183", run_ids=["PR0016522"], day0="2025-01-13"))}
    assert opts["cancel_po"]["breaches_commitments"] == ["CMT00560"]
    assert opts["cancel_po"]["risk"] == "high"
    assert best_option(list(opts.values()))["action"] in {"switch_supplier", "reallocate_stock"}


def test_deterministic_and_complete(db_factory, holdout):
    s = holdout["HS05"]
    con = db_factory(s["day0"])
    a = simulate_options(con, build_state(con, day0=s["day0"], **holdout_context(s)))
    b = simulate_options(con, build_state(con, day0=s["day0"], **holdout_context(s)))
    assert a == b
    assert [o["action"] for o in a][:len(SIMULATED)] == list(SIMULATED)
    assert {o["action"] for o in a} == set(TAXONOMY)
    unsupported = {o["action"]: o for o in a if not o["supported"]}
    assert set(unsupported) == {"build_safety_stock", "renegotiate", "reduce_allocation"}
    assert all("cost" not in o for o in unsupported.values())


def test_simulate_action_single(db_factory, holdout):
    s = holdout["HS05"]
    con = db_factory(s["day0"])
    out = simulate_action(con, "switch_supplier", day0=s["day0"], **holdout_context(s))
    assert out["action"] == "switch_supplier" and out["feasible"] is True
    with pytest.raises(SimulationError):
        simulate_action(con, "teleport", day0=s["day0"], **holdout_context(s))


@pytest.mark.parametrize("kwargs,match", [
    (dict(rpo_id="RPO016183", run_ids=["PR0016522"], day0="2025-01-06"), "no date revision"),
    (dict(rpo_id="RPO024268", run_ids=["PR0016522"], day0="2025-01-13"), "not found"),
    (dict(rpo_id="RPO016183", run_ids=[], day0="2025-01-13"), "production_run_id"),
    (dict(rpo_id="RPO016183", run_ids=["PR9999999"], day0="2025-01-13"), "not found"),
    (dict(rpo_id="RPO016183", run_ids=["PR0016522"], day0="2025-01-13", rm_id="RM0001"), "not on"),
])
def test_build_state_errors(db_factory, kwargs, match):
    con = db_factory("2025-01-13")
    with pytest.raises(SimulationError, match=match):
        build_state(con, **kwargs)
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_simulate.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'agent.scenarios'`

- [ ] **Step 4: Implement `agent/simulate.py`**

```python
"""Deterministic what-if simulator for raw-material supply disruptions.

Port of dataset/code/gen_eval.py::holdout_scenarios (the generator's consequence model) over the
as-of SQLite views, using the generator's own cost functions in dataset/code/consequences.py.
Same DB + same inputs -> same numbers. The LLM never produces cost / stockout figures itself.
"""
from __future__ import annotations

import importlib.util
import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from agent.config import ROOT

TAXONOMY = ("expedite", "switch_supplier", "substitute_rm", "reallocate_stock", "cancel_po",
            "build_safety_stock", "renegotiate", "accept_delay", "reduce_allocation")
SIMULATED = ("accept_delay", "expedite", "switch_supplier", "reallocate_stock", "substitute_rm", "cancel_po")
RULES_PATH = Path(__file__).with_name("sim_rules.yaml")


def _load_consequences():
    spec = importlib.util.spec_from_file_location("sc_consequences", ROOT / "dataset" / "code" / "consequences.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cq = _load_consequences()


class SimulationError(ValueError):
    """The inputs cannot be turned into a scenario state (missing PO, revision, run or BOM line)."""


@dataclass(frozen=True)
class ScenarioState:
    day0: date
    rpo_id: str
    supplier_id: str
    plant_id: str
    rm_id: str
    uom: str
    eta: date
    revision_id: str
    primary_run_id: str
    run_ids: tuple[str, ...]
    planned_start: date
    req: float
    daily_block_cost: float
    service_units: int
    lot_value: float
    lot_rpo_ids: tuple[str, ...]
    lead_days: int
    congested: bool
    q4_slip_risk: bool

    def to_dict(self) -> dict:
        return {k: (v.isoformat() if isinstance(v, date) else list(v) if isinstance(v, tuple) else v)
                for k, v in asdict(self).items()}


def load_rules(path: Path = RULES_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def _one(con, sql, params, what):
    row = con.execute(sql, params).fetchone()
    if row is None:
        raise SimulationError(what)
    return row


def _sunday_on_or_before(d: date) -> date:
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _snapshot(con, rm_id, plant_id, d: date) -> tuple[float, float]:
    row = con.execute("""SELECT on_hand_units, safety_stock_units FROM rm_inventory_snapshots_weekly
                         WHERE rm_id = ? AND plant_id = ? AND snapshot_date = ?""",
                      (rm_id, plant_id, _sunday_on_or_before(d).isoformat())).fetchone()
    return (float(row[0]), float(row[1])) if row else (math.nan, math.nan)


def build_state(con, *, rpo_id: str, run_ids, day0: str, rm_id: str | None = None, rules=None) -> ScenarioState:
    rules = rules or load_rules()
    d0 = date.fromisoformat(day0)
    run_ids = list(run_ids or [])
    if not run_ids:
        raise SimulationError("at least one production_run_id is required (the run that needs the material)")
    po = _one(con, "SELECT supplier_id, plant_id FROM rm_purchase_orders WHERE rm_purchase_order_id = ?",
              (rpo_id,), f"{rpo_id} not found (or ordered after the as-of date)")
    sup, pl = po["supplier_id"], po["plant_id"]
    rms = [r[0] for r in con.execute("""SELECT DISTINCT rm_id FROM rm_purchase_order_lines
                                        WHERE rm_purchase_order_id = ? ORDER BY rm_id""", (rpo_id,))]
    if rm_id is None:
        if len(rms) != 1:
            raise SimulationError(f"{rpo_id} has {len(rms)} materials; pass rm_id")
        rm_id = rms[0]
    elif rm_id not in rms:
        raise SimulationError(f"{rm_id} is not on {rpo_id}")
    rev = _one(con, """SELECT revision_id, revised_at, new_expected_at FROM rm_po_revisions
                       WHERE rm_po_id = ? AND revised_at <= ? ORDER BY revised_at DESC, revision_id DESC LIMIT 1""",
               (rpo_id, day0), f"{rpo_id} has no date revision on or before {day0}; nothing to simulate")
    run = _one(con, "SELECT product_id, planned_start, planned_qty FROM production_runs WHERE production_run_id = ?",
               (run_ids[0],), f"run {run_ids[0]} not found")
    ps = date.fromisoformat(run["planned_start"])
    bom = _one(con, """SELECT qty_per_unit, scrap_pct FROM bill_of_materials
                       WHERE product_id = ? AND rm_id = ? AND effective_from <= ?
                         AND (effective_to IS NULL OR effective_to = '' OR effective_to >= ?)
                       ORDER BY bom_version DESC LIMIT 1""",
               (run["product_id"], rm_id, ps.isoformat(), ps.isoformat()), f"run {run_ids[0]} does not consume {rm_id}")
    ph = ",".join("?" * len(run_ids))
    vr = con.execute(f"""SELECT r.production_run_id, r.planned_qty, x.unit_price FROM production_runs r
                         JOIN products_ext x USING (product_id) WHERE r.production_run_id IN ({ph})""",
                     tuple(run_ids)).fetchall()
    missing = set(run_ids) - {r["production_run_id"] for r in vr}
    if missing:
        raise SimulationError(f"runs not found: {sorted(missing)}")
    # the "slipped lot" = every RPO for this RM and plant whose date moved on the same day
    lot = con.execute("""SELECT o.rm_purchase_order_id, SUM(l.quantity_ordered * l.unit_cost) AS v
                         FROM rm_purchase_orders o JOIN rm_purchase_order_lines l USING (rm_purchase_order_id)
                         WHERE l.rm_id = ? AND o.plant_id = ?
                           AND o.rm_purchase_order_id IN (SELECT rm_po_id FROM rm_po_revisions WHERE revised_at = ?)
                         GROUP BY o.rm_purchase_order_id ORDER BY o.rm_purchase_order_id""",
                      (rm_id, pl, rev["revised_at"])).fetchall()
    lead = _one(con, """SELECT lead_time_days FROM rm_supplier_catalog WHERE rm_id = ? AND supplier_id = ?
                        ORDER BY catalog_id LIMIT 1""", (rm_id, sup), f"{sup} has no catalog row for {rm_id}")[0]
    country = con.execute("SELECT country_code FROM suppliers WHERE supplier_id = ?", (sup,)).fetchone()
    uom = con.execute("SELECT uom FROM raw_materials WHERE rm_id = ?", (rm_id,)).fetchone()[0]
    eta = date.fromisoformat(rev["new_expected_at"])
    value = sum(r["planned_qty"] * r["unit_price"] for r in vr)
    return ScenarioState(
        day0=d0, rpo_id=rpo_id, supplier_id=sup, plant_id=pl, rm_id=rm_id, uom=uom, eta=eta,
        revision_id=rev["revision_id"], primary_run_id=run_ids[0], run_ids=tuple(run_ids), planned_start=ps,
        req=run["planned_qty"] * bom["qty_per_unit"] * (1 + bom["scrap_pct"] / 100),
        daily_block_cost=cq.block_cost_per_day(value, {"memory": {"stockout_cost_rate_per_day": rules["stockout_cost_rate_per_day"]}}),
        service_units=int(sum(r["planned_qty"] for r in vr)),
        lot_value=float(sum(r["v"] for r in lot)), lot_rpo_ids=tuple(r[0] for r in lot), lead_days=int(lead),
        congested=d0.month == 2 and country is not None and country[0] == rules["feb_congestion_country"],
        q4_slip_risk=sup == rules["q4_slip_supplier_id"] and eta.month in (11, 12))


def _opt(action, **kw) -> dict:
    return {"action": action, "feasible": True, "supported": True, "breaches_commitments": [], **kw}


def _infeasible(action, note) -> dict:
    return {"action": action, "feasible": False, "supported": True, "note": note}


def _best_transfer(con, st: ScenarioState, rules) -> dict | None:
    xy = rules["region_xy"]
    regions = {r[0]: r[1] for r in con.execute("SELECT plant_id, region FROM plants")}
    donors = [r[0] for r in con.execute("""SELECT DISTINCT plant_id FROM rm_inventory_snapshots_weekly
                                           WHERE rm_id = ? AND plant_id <> ? ORDER BY plant_id""", (st.rm_id, st.plant_id))]
    best = None
    for k in donors:
        oh, ss = _snapshot(con, st.rm_id, k, st.day0)
        if math.isnan(oh) or oh - st.req < 0:
            continue
        td = cq.transfer_days(math.dist(xy.get(regions[k], [0, 0]), xy.get(regions[st.plant_id], [0, 0])))
        arr = st.day0 + timedelta(days=td)
        so = max(0, (arr - st.planned_start).days)
        depleting = k == rules["depleting_donor_plant_id"]
        cand = _opt("reallocate_stock", donor_plant=k, arrival=arr, stockout_days=so,
                    cost=round(rules["transfer_cost_per_unit_value"] * st.lot_value + so * st.daily_block_cost, 0),
                    risk="high" if (depleting or oh - st.req < ss) else "medium",
                    note=f"donor on hand {oh:,.1f} vs SS {ss:,.1f}"
                         + ("; transfers from this plant have repeatedly left it short" if depleting else ""),
                    evidence_record_ids=[f"{st.rm_id}|{k}|{_sunday_on_or_before(st.day0).isoformat()}"])
        # Verbatim from the generator: (risk, cost) tuples compare risk as a string, so "high" < "medium".
        # Kept for parity with holdout_scenarios.json; documented in README "Known limitations".
        if best is None or (cand["risk"], cand["cost"]) < (best["risk"], best["cost"]):
            best = cand
    return best


def _substitute(con, st: ScenarioState, rules) -> dict | None:
    sg = con.execute("SELECT substitute_group_id FROM raw_materials WHERE rm_id = ?", (st.rm_id,)).fetchone()[0]
    if not sg:
        return None
    subs = [r[0] for r in con.execute("""SELECT rm_id FROM raw_materials WHERE substitute_group_id = ? AND rm_id <> ?
                                         ORDER BY rm_id""", (sg, st.rm_id))]
    ok = [x for x in subs if _snapshot(con, x, st.plant_id, st.day0)[0] >= st.req]  # NaN compares False
    if not ok:
        return None
    x = ok[0]
    arr = st.day0 + timedelta(days=rules["substitute_qa_days"])
    dq = max(0, (arr - st.planned_start).days)
    risky = x == rules["risky_substitute_rm_id"]
    return _opt("substitute_rm", substitute=x, arrival=arr, stockout_days=dq,
                cost=round(rules["substitute_fixed_cost"] + dq * st.daily_block_cost, 0),
                risk="high" if risky else "medium",
                note="QA approval ~6 days" + ("; this substitute has a history of incoming rejects" if risky else ""),
                evidence_record_ids=[x])


def _cancel(con, st: ScenarioState, rules, switch: dict, d_acc: int) -> dict:
    d0 = st.day0.isoformat()
    rows = con.execute("""SELECT commitment_id, commitment_text FROM commitments
                          WHERE made_at <= ? AND (resolved_at IS NULL OR resolved_at > ?) AND counterparty_id IN (?, ?)
                          ORDER BY commitment_id""", (d0, d0, st.supplier_id, st.plant_id)).fetchall()
    vol = [r["commitment_id"] for r in rows if "We order at least" in (r["commitment_text"] or "")]
    base = switch["cost"] if switch["feasible"] else d_acc * st.daily_block_cost
    penalty = rules["volume_breach_rate"] * st.lot_value * rules["volume_breach_multiplier"] if vol else 0
    # the generator re-buys via the switch lane; with no alternative source the arrival is unknown (None)
    return _opt("cancel_po", arrival=switch.get("arrival"), stockout_days=switch.get("stockout_days", d_acc),
                cost=round(base + rules["cancel_fee_rate"] * st.lot_value + penalty, 0),
                risk="high" if vol else "medium",
                note=("cancelling would breach open volume commitment " + ", ".join(vol)) if vol
                     else "cancel and re-buy elsewhere",
                breaches_commitments=vol, evidence_record_ids=[st.rpo_id, *vol])


def simulate_options(con, st: ScenarioState, rules=None) -> list[dict]:
    rules = rules or load_rules()
    d0, P, daily, lv = st.day0, st.planned_start, st.daily_block_cost, st.lot_value

    def late(arr: date) -> int:
        return max(0, (arr - P).days)

    acts = []
    d_acc = late(st.eta)
    acts.append(_opt("accept_delay", arrival=st.eta, stockout_days=d_acc, cost=round(d_acc * daily, 0),
                     risk="high" if st.q4_slip_risk else "low",
                     note="supplier historically slips a further 7-14 days in Nov-Dec" if st.q4_slip_risk else "supplier date",
                     evidence_record_ids=[st.rpo_id, st.revision_id, st.primary_run_id]))
    pct = cq.rm_expedite_premium_pct(st.lead_days, st.congested)
    e_arr = d0 + timedelta(days=cq.rm_expedite_days(st.lead_days, st.congested))
    acts.append(_opt("expedite", arrival=e_arr, stockout_days=late(e_arr), cost=round(pct * lv + late(e_arr) * daily, 0),
                     risk="medium", note=f"air premium {pct:.0%} of lot value", evidence_record_ids=list(st.lot_rpo_ids)))
    alt = con.execute("""SELECT catalog_id, supplier_id, lead_time_days, unit_price FROM rm_supplier_catalog
                         WHERE rm_id = ? AND supplier_id <> ? AND qualified_flag = 1
                           AND price_valid_from <= ? AND price_valid_to >= ?
                         ORDER BY allocation_pct DESC, catalog_id LIMIT 1""",
                      (st.rm_id, st.supplier_id, d0.isoformat(), d0.isoformat())).fetchone()
    if alt:
        s_arr = d0 + timedelta(days=cq.spot_lead_days(int(alt["lead_time_days"])))
        acts.append(_opt("switch_supplier", arrival=s_arr, stockout_days=late(s_arr),
                         cost=round(rules["spot_price_premium"] * st.req * float(alt["unit_price"]) + late(s_arr) * daily, 0),
                         risk="medium", note=f"spot lot from {alt['supplier_id']}", alt_supplier_id=alt["supplier_id"],
                         evidence_record_ids=[alt["catalog_id"]]))
    else:
        acts.append(_infeasible("switch_supplier", "no other qualified source in the catalog at day 0"))
    acts.append(_best_transfer(con, st, rules) or _infeasible("reallocate_stock", "no other plant holds enough of this material"))
    acts.append(_substitute(con, st, rules) or _infeasible("substitute_rm", "no qualified substitute in stock at the plant"))
    acts.append(_cancel(con, st, rules, switch=acts[2], d_acc=d_acc))
    for a in acts:
        if a["feasible"]:
            a["score"] = round(cq.total_score(a["cost"], 0, 0, a["risk"]), 0)
            a["service_impact_units"] = st.service_units if a["stockout_days"] > 0 else 0
            a["arrival"] = a["arrival"].isoformat() if a["arrival"] else None
    for action in TAXONOMY:
        if action not in SIMULATED:
            acts.append({"action": action, "feasible": False, "supported": False,
                         "note": "not modelled by the deterministic simulator - no cost or stockout estimate available"})
    return acts


def simulate_action(con, action_type: str, *, rpo_id: str, run_ids, day0: str, rm_id: str | None = None,
                    rules=None) -> dict:
    if action_type not in TAXONOMY:
        raise SimulationError(f"unknown action_type {action_type!r}; expected one of {TAXONOMY}")
    st = build_state(con, rpo_id=rpo_id, run_ids=run_ids, day0=day0, rm_id=rm_id, rules=rules)
    return next(o for o in simulate_options(con, st, rules) if o["action"] == action_type)


def best_option(options: list[dict]) -> dict | None:
    ok = [o for o in options if o.get("feasible") and o.get("supported", True) and not o.get("breaches_commitments")]
    return min(ok, key=lambda o: (o["score"], o["action"])) if ok else None
```

- [ ] **Step 5: Implement `agent/scenarios.py`**

```python
"""Holdout scenario helpers shared by tests, the eval harness, the console and the demo."""
from __future__ import annotations

import json
import re

from agent.config import Settings


def load_holdout(settings: Settings) -> dict[str, dict]:
    data = json.loads((settings.dataset_dir / "holdout_scenarios.json").read_text())
    return {s["scenario_id"]: s for s in data["scenarios"]}


def holdout_context(s: dict) -> dict:
    """MRP context for a scenario: the slipped RPO, the run named in the report first, then the other
    dependent runs. These are inputs a planner's MRP would supply, not gold answers."""
    report = s["day0_report"]
    rpo = re.search(r"\b(RPO\d+)\b", report).group(1)
    primary = re.search(r"\brun (PR\d+)\b", report).group(1)
    runs = [primary] + [r for r in s["entities"]["runs"] if r != primary]
    return {"rpo_id": rpo, "run_ids": runs, "rm_id": s["entities"]["rm_id"]}


def compare_to_gold(options: list[dict], candidate_actions: list[dict]) -> list[str]:
    """Mismatches between simulated options and the generator's candidate_actions (cost within +/-1 USD,
    the generator rounds to whole dollars)."""
    mine = {o["action"]: o for o in options}
    out = []
    for g in candidate_actions:
        o = mine.get(g["action"])
        if o is None:
            out.append(f"{g['action']}: not simulated")
            continue
        if o["feasible"] != g["feasible"]:
            out.append(f"{g['action']}: feasible {o['feasible']} != {g['feasible']}")
            continue
        if not g["feasible"]:
            continue
        for k in ("stockout_days", "risk", "arrival", "donor_plant", "substitute"):
            if o.get(k) != g.get(k):
                out.append(f"{g['action']}: {k} {o.get(k)!r} != {g.get(k)!r}")
        if abs(o["cost"] - g["cost"]) > 1:
            out.append(f"{g['action']}: cost {o['cost']} != {g['cost']}")
    return out
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_simulate.py -v`
Expected: all pass (14 parity cases + 9 others). If a parity case fails, print `compare_to_gold` for it and diff against `gen_eval.py:202-323`. Do not loosen the tolerance.

- [ ] **Step 7: Commit**

```bash
git add agent/sim_rules.yaml agent/simulate.py agent/scenarios.py tests/test_simulate.py
git commit -m "feat: deterministic simulator ported from generator, parity-tested on 14 holdout scenarios

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Corpus index and Hindsight memory tools with client-side as-of enforcement

**Files:**
- Create: `agent/corpus.py`, `agent/hindsight_tools.py`
- Test: `tests/test_hindsight_tools.py`

**Interfaces:**
- Consumes: `Settings`, `agent.db.live_memory_ids` (passed in as a callable).
- Produces:
  - `agent.corpus`:
    - `as_of_end(as_of: str) -> datetime` (23:59:59-05:00)
    - `parse_ts(value) -> datetime | None`
    - `CorpusDoc` (fields `doc_id, timestamp, context, content, doc_type, entity_ids, source_record_ids, split, supersedes`; property `date`)
    - `DocIndex` with `by_id`, `memory_horizon`, `resolve(document_id=None, mentioned_at=None, context=None) -> CorpusDoc | None`, `visible(doc, as_of) -> bool`, `superseding(doc_id, as_of) -> list[CorpusDoc]`
    - `load_index(path) -> DocIndex` (cached)
  - `agent.hindsight_tools`:
    - `MemoryHit(text, fact_type, doc_id, doc_date, doc_type, source, superseded_by)` with `to_dict()`
    - `RecallResult(query, as_of, hits, raw_count, dropped_future, dropped_unresolved)` with `doc_ids()` and `to_payload(limit=25)`
    - `ReflectResult(question, as_of, mode, text, doc_ids, hits, structured=None)`, where `mode` is `"native"` or `"local_filtered"` and `structured` is the `response_schema` output (native only)
    - `RetainLedger(path)` with `entries()`, `append(entry)`, `max_timestamp(bank_id)`, `doc_ids(bank_id)`
  - `HindsightMemory(client, bank_id, index, ledger, live_memory_ids, synthesizer=None, policy=("", []), use_mental_models=True)` with:
    - `recall(query, as_of, *, window_days=None, budget="mid", max_tokens=4096) -> RecallResult`
    - `native_reflect_allowed(as_of) -> bool`
    - `reflect(question, as_of, *, budget="mid", response_schema=None) -> ReflectResult`
    - `use_mental_models=False` passes `exclude_mental_models=True` to native reflect, and raises `ValueError` at construction if the client's `reflect` has no such parameter
    - `get_document(doc_id, as_of) -> dict | None`
    - `retain_experience(*, document_id, content, context, timestamp, metadata) -> str` (returns `"ok"` or `"error: ..."`)
    - attribute `policy_text: str`
  - Other exports: `load_bank_policy() -> tuple[str, list[str]]` (the bank's mission + traits and the 8 directive texts from `agent.bank_setup`); `build_memory(settings, live_memory_ids, synthesizer=None) -> HindsightMemory`.
  - `synthesizer` signature: `(question: str, hits: list[MemoryHit], policy_text: str) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_hindsight_tools.py`:
```python
from types import SimpleNamespace

import pytest

from agent.corpus import load_index
from agent.hindsight_tools import HindsightMemory, RetainLedger, load_bank_policy


class FakeHindsight:
    """Signature deliberately lacks temporal_window/tags to prove kwargs are filtered."""

    def __init__(self, results=(), reflect_text="native answer", based_on=()):
        self.results, self.reflect_text, self.based_on = list(results), reflect_text, list(based_on)
        self.calls = []

    def recall(self, bank_id, query, budget="mid", max_tokens=4096, types=None, query_timestamp=None):
        self.calls.append(("recall", dict(bank_id=bank_id, query=query, types=types, query_timestamp=query_timestamp)))
        return SimpleNamespace(results=self.results)

    def reflect(self, bank_id, query, budget="low", include_facts=False):
        self.calls.append(("reflect", dict(query=query)))
        return SimpleNamespace(text=self.reflect_text, based_on=SimpleNamespace(memories=self.based_on))

    def retain(self, bank_id, content, context=None, timestamp=None, document_id=None, metadata=None):
        self.calls.append(("retain", dict(document_id=document_id, timestamp=timestamp)))


@pytest.fixture(scope="module")
def index(base_settings):
    return load_index(base_settings.corpus_path)


def hit(text, **kw):
    return SimpleNamespace(text=text, type="world", **kw)


def make(index, tmp_path, client, live_ids=frozenset(), synth=None):
    return HindsightMemory(client, "bank", index, RetainLedger(tmp_path / "ledger.jsonl"), lambda: set(live_ids),
                           synthesizer=synth, policy=("mission", ["cite evidence"]))


def test_recall_filters_future_and_unresolved(index, tmp_path):
    holdout_doc = next(d for d in index.by_id.values() if d.split == "holdout")
    client = FakeHindsight([
        hit("slip", document_id="DOC000248"),
        hit("expedite", document_id="DOC000254"),
        hit("future", document_id=holdout_doc.doc_id),
        hit("orphan", document_id=None, mentioned_at=None, context=None),
    ])
    mem = make(index, tmp_path, client)
    r = mem.recall("RPO003179 ETA", "2023-06-03")
    assert [h.doc_id for h in r.hits] == ["DOC000248"]
    assert (r.raw_count, r.dropped_future, r.dropped_unresolved) == (4, 2, 1)
    kw = client.calls[0][1]
    assert kw["types"] == ["world", "experience"] and kw["query_timestamp"].startswith("2023-06-03T23:59:59")


def test_recall_resolves_by_timestamp_and_context(index, tmp_path):
    d = index.by_id["DOC000254"]
    client = FakeHindsight([hit("expedite", document_id=None, mentioned_at=d.timestamp.isoformat(), context=d.context)])
    r = make(index, tmp_path, client).recall("q", "2023-06-11")
    assert r.hits[0].doc_id == "DOC000254"


def test_supersession_annotated_only_when_visible(index, tmp_path):
    client = FakeHindsight([hit("slip", document_id="DOC000248")])
    mem = make(index, tmp_path, client)
    assert mem.recall("q", "2023-06-11").hits[0].superseded_by == [{"doc_id": "DOC000254", "date": "2023-06-04"}]
    assert mem.recall("q", "2023-06-03").hits[0].superseded_by == []


def test_live_hits_only_visible_with_matching_live_db(index, tmp_path):
    live_hit = hit("agent decision", document_id="DECL00001.ab12", mentioned_at="2025-10-14T12:00:00-05:00")
    mine = make(index, tmp_path, FakeHindsight([live_hit]), live_ids={"DECL00001.ab12"})
    other = make(index, tmp_path, FakeHindsight([live_hit]), live_ids={"DECL00001.ffff"})
    r = mine.recall("q", "2025-10-19")
    assert [(h.doc_id, h.source) for h in r.hits] == [("DECL00001", "live")]
    assert mine.recall("q", "2025-10-13").dropped_future == 1
    assert other.recall("q", "2025-10-19").hits == []


def test_native_reflect_guard(index, tmp_path):
    client = FakeHindsight([hit("slip", document_id="DOC000248")], based_on=[hit("x", document_id="DOC000248")])
    calls = []
    mem = make(index, tmp_path, client, synth=lambda q, hits, pol: calls.append((q, len(hits), pol)) or "local answer")
    # before the corpus horizon -> local synthesis over as-of-filtered recall
    r = mem.reflect("what happened?", "2023-06-11")
    assert (r.mode, r.text, r.doc_ids) == ("local_filtered", "local answer", ["DOC000248"])
    assert "mission" in calls[0][2]
    # after the horizon with an empty ledger -> native
    assert mem.reflect("what happened?", "2025-10-14").mode == "native"
    # a retain from another live DB makes native reflect unsafe
    mem.ledger.append({"bank_id": "bank", "document_id": "DECL00001.zz", "timestamp": "2025-10-01T12:00:00-05:00", "status": "ok"})
    assert mem.native_reflect_allowed("2025-10-14") is False


def test_get_document_respects_as_of(index, tmp_path):
    mem = make(index, tmp_path, FakeHindsight())
    assert mem.get_document("DOC000254", "2023-06-03") is None
    doc = mem.get_document("DOC000248", "2023-06-11")
    assert doc["doc_id"] == "DOC000248" and doc["superseded_by"] == [{"doc_id": "DOC000254", "date": "2023-06-04"}]


def test_retain_logs_to_ledger(index, tmp_path):
    client = FakeHindsight()
    mem = make(index, tmp_path, client)
    status = mem.retain_experience(document_id="DECL00001.ab12", content="c", context="agent decision log",
                                   timestamp="2025-10-14T12:00:00-05:00", metadata={"k": "v"})
    assert status == "ok"
    assert client.calls[-1] == ("retain", {"document_id": "DECL00001.ab12", "timestamp": "2025-10-14T12:00:00-05:00"})
    assert mem.ledger.doc_ids("bank") == {"DECL00001.ab12"}


def test_bank_policy_matches_bank_setup():
    mission, directives = load_bank_policy()
    assert "supply-chain memory" in mission and "Proactive" in mission
    assert len(directives) == 8 and any("open commitments" in d for d in directives)


class SchemaReflectClient(FakeHindsight):
    def reflect(self, bank_id, query, budget="low", include_facts=False, response_schema=None,
                exclude_mental_models=False):
        self.calls.append(("reflect", dict(response_schema=response_schema, exclude=exclude_mental_models)))
        return SimpleNamespace(text="t", structured_output={"recommendation": "x"},
                               based_on=SimpleNamespace(memories=[]))


def test_reflect_schema_and_mental_model_exclusion(index, tmp_path):
    client = SchemaReflectClient()
    mem = HindsightMemory(client, "bank", index, RetainLedger(tmp_path / "l.jsonl"), lambda: set(),
                          use_mental_models=False)
    r = mem.reflect("q", "2025-10-14", budget="high", response_schema={"type": "object"})
    assert r.structured == {"recommendation": "x"}
    assert client.calls[-1] == ("reflect", {"response_schema": {"type": "object"}, "exclude": True})


def test_mental_model_exclusion_unsupported_fails_loudly(index, tmp_path):
    with pytest.raises(ValueError, match="exclude_mental_models"):
        HindsightMemory(FakeHindsight(), "bank", index, RetainLedger(tmp_path / "l.jsonl"), lambda: set(),
                        use_mental_models=False)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_hindsight_tools.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'agent.corpus'`

- [ ] **Step 3: Implement `agent/corpus.py`**

```python
"""Local index over memory_corpus.jsonl.

Resolves Hindsight hits back to doc_ids, applies the as-of visibility rule and exposes the corpus
`supersedes` graph so later statements win over earlier ones.
"""
from __future__ import annotations

import functools
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HQ_OFFSET = "-05:00"


def as_of_end(as_of: str) -> datetime:
    return datetime.fromisoformat(f"{as_of}T23:59:59{HQ_OFFSET}")


def parse_ts(value) -> datetime | None:
    if value in (None, ""):
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _minute(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


@dataclass(frozen=True)
class CorpusDoc:
    doc_id: str
    timestamp: datetime
    context: str
    content: str
    doc_type: str
    entity_ids: tuple[str, ...]
    source_record_ids: tuple[str, ...]
    split: str
    supersedes: tuple[str, ...]

    @property
    def date(self) -> str:
        return self.timestamp.date().isoformat()


class DocIndex:
    def __init__(self, docs: list[CorpusDoc]):
        self.by_id = {d.doc_id: d for d in docs}
        self._by_minute_ctx: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._by_minute: dict[str, list[str]] = defaultdict(list)
        self._superseded_by: dict[str, list[str]] = defaultdict(list)
        for d in docs:
            self._by_minute_ctx[(_minute(d.timestamp), d.context)].append(d.doc_id)
            self._by_minute[_minute(d.timestamp)].append(d.doc_id)
            for old in d.supersedes:
                self._superseded_by[old].append(d.doc_id)
        mem = [d.timestamp for d in docs if d.split == "memory"]
        self.memory_horizon = max(mem) if mem else datetime.min.replace(tzinfo=timezone.utc)

    @classmethod
    def load(cls, path: Path) -> "DocIndex":
        docs = []
        with Path(path).open() as fh:
            for line in fh:
                r = json.loads(line)
                docs.append(CorpusDoc(r["doc_id"], parse_ts(r["timestamp"]), r["context"], r["content"], r["doc_type"],
                                      tuple(r["entity_ids"]), tuple(r["source_record_ids"]), r["split"],
                                      tuple(r.get("supersedes") or ())))
        return cls(docs)

    def resolve(self, document_id=None, mentioned_at=None, context=None) -> CorpusDoc | None:
        if document_id and document_id in self.by_id:
            return self.by_id[document_id]
        ts = parse_ts(mentioned_at)
        if ts is None:
            return None
        ids = self._by_minute_ctx.get((_minute(ts), context), []) if context else self._by_minute.get(_minute(ts), [])
        return self.by_id[ids[0]] if len(ids) == 1 else None  # ambiguous -> unresolved

    def visible(self, doc: CorpusDoc, as_of: str) -> bool:
        return doc.split == "memory" and doc.timestamp <= as_of_end(as_of)

    def superseding(self, doc_id: str, as_of: str) -> list[CorpusDoc]:
        later = (self.by_id[i] for i in self._superseded_by.get(doc_id, []))
        return sorted((d for d in later if self.visible(d, as_of)), key=lambda d: d.timestamp)


@functools.lru_cache(maxsize=4)
def load_index(path: Path) -> DocIndex:
    return DocIndex.load(path)
```

- [ ] **Step 4: Implement `agent/hindsight_tools.py`**

```python
"""Hindsight memory tools with the as-of rule enforced client-side.

Hindsight recall has no hard "nothing after date X" filter and reflect has no date filter, so:
* recall hits are resolved to corpus doc_ids (or live decision ids) and anything dated after as_of,
  from the holdout split, or unresolvable is dropped and counted;
* reflect runs natively only when nothing in the bank can be newer than as_of (corpus horizon and the
  bank-wide retain ledger); otherwise it is an as-of-filtered recall + local synthesis guided by the
  bank's own mission, traits and directives (agent/bank_setup.py). Mental Models are only ever consulted
  by native reflect, so they obey the same guard.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from agent.bank_setup import DIRECTIVES, make_client, reflect_mission
from agent.config import Settings
from agent.corpus import DocIndex, as_of_end, load_index, parse_ts

LIVE_PREFIX = "DECL"


def load_bank_policy() -> tuple[str, list[str]]:
    return reflect_mission(), [content for _, content in DIRECTIVES]


def _field(obj, name, default=None):
    if obj is None:
        return default
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _call(fn, **kwargs):
    """Pass only the kwargs the installed client accepts (client versions differ)."""
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**kwargs)
    return fn(**{k: v for k, v in kwargs.items() if k in params})


@dataclass
class MemoryHit:
    text: str
    fact_type: str | None
    doc_id: str | None
    doc_date: str | None
    doc_type: str | None
    source: str  # corpus | live
    superseded_by: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RecallResult:
    query: str
    as_of: str
    hits: list[MemoryHit]
    raw_count: int
    dropped_future: int
    dropped_unresolved: int

    def doc_ids(self) -> list[str]:
        return sorted({h.doc_id for h in self.hits if h.doc_id})

    def to_payload(self, limit: int = 25) -> dict:
        return {"query": self.query, "as_of": self.as_of, "hits": [h.to_dict() for h in self.hits[:limit]],
                "dropped_future": self.dropped_future, "dropped_unresolved": self.dropped_unresolved}


@dataclass
class ReflectResult:
    question: str
    as_of: str
    mode: str  # native | local_filtered
    text: str | None
    doc_ids: list[str]
    hits: list[MemoryHit]
    structured: dict | None = None


class RetainLedger:
    """Bank-wide record of every runtime retain, independent of which live DB a run uses."""

    def __init__(self, path: Path):
        self.path = path

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def _ok(self, bank_id: str) -> list[dict]:
        return [e for e in self.entries() if e.get("status") == "ok" and e.get("bank_id") == bank_id]

    def max_timestamp(self, bank_id: str) -> datetime | None:
        ts = [parse_ts(e["timestamp"]) for e in self._ok(bank_id)]
        return max(ts) if ts else None

    def doc_ids(self, bank_id: str) -> set[str]:
        return {e["document_id"] for e in self._ok(bank_id)}


class HindsightMemory:
    def __init__(self, client, bank_id: str, index: DocIndex, ledger: RetainLedger,
                 live_memory_ids: Callable[[], set[str]],
                 synthesizer: Callable[[str, list[MemoryHit], str], str] | None = None,
                 policy: tuple[str, list[str]] = ("", []), use_mental_models: bool = True):
        if not bank_id:
            raise ValueError("HINDSIGHT_BANK_ID is empty - set it in .env")
        if not use_mental_models and "exclude_mental_models" not in inspect.signature(client.reflect).parameters:
            raise ValueError("MENTAL_MODELS_IN_REFLECT=0 but the installed hindsight-client reflect() has no "
                             "exclude_mental_models parameter; run `python -m agent.mental_models delete` for the "
                             "ablation instead")
        self.client, self.bank_id, self.index, self.ledger = client, bank_id, index, ledger
        self.live_memory_ids, self.synthesizer = live_memory_ids, synthesizer
        self.use_mental_models = use_mental_models
        mission, directives = policy
        self.policy_text = f"Mission: {mission}\nDirectives:\n" + "\n".join(f"- {d}" for d in directives)

    def _resolve(self, raw, as_of: str) -> tuple[MemoryHit | None, str]:
        doc_id, text = _field(raw, "document_id"), _field(raw, "text") or ""
        mentioned, ctx, ftype = _field(raw, "mentioned_at"), _field(raw, "context"), _field(raw, "type")
        if doc_id and str(doc_id).startswith(LIVE_PREFIX):
            if doc_id not in self.live_memory_ids():
                return None, "unresolved"  # retained by a run whose live DB is not in use
            ts = parse_ts(mentioned)
            if ts is None or ts > as_of_end(as_of):
                return None, "future"
            return MemoryHit(text, ftype, str(doc_id).split(".")[0], ts.date().isoformat(), "agent_decision", "live"), "ok"
        doc = self.index.resolve(doc_id, mentioned, ctx)
        if doc is None:
            return None, "unresolved"
        if not self.index.visible(doc, as_of):
            return None, "future"
        sup = [{"doc_id": d.doc_id, "date": d.date} for d in self.index.superseding(doc.doc_id, as_of)]
        return MemoryHit(text, ftype, doc.doc_id, doc.date, doc.doc_type, "corpus", sup), "ok"

    def recall(self, query: str, as_of: str, *, window_days: int | None = None, budget: str = "mid",
               max_tokens: int = 4096) -> RecallResult:
        end = as_of_end(as_of)
        kw = dict(bank_id=self.bank_id, query=query, budget=budget, max_tokens=max_tokens,
                  types=["world", "experience"], query_timestamp=end.isoformat())
        if window_days:
            kw["temporal_window"] = {"start": (end - timedelta(days=window_days)).isoformat(), "end": end.isoformat()}
        raw = list(_field(_call(self.client.recall, **kw), "results") or [])
        hits, future, unresolved = [], 0, 0
        for r in raw:
            h, why = self._resolve(r, as_of)
            if h:
                hits.append(h)
            elif why == "future":
                future += 1
            else:
                unresolved += 1
        return RecallResult(query, as_of, hits, len(raw), future, unresolved)

    def native_reflect_allowed(self, as_of: str) -> bool:
        end = as_of_end(as_of)
        if end < self.index.memory_horizon:
            return False
        last = self.ledger.max_timestamp(self.bank_id)
        if last is not None and last > end:
            return False
        return self.ledger.doc_ids(self.bank_id) <= self.live_memory_ids()

    def reflect(self, question: str, as_of: str, *, budget: str = "mid",
                response_schema: dict | None = None) -> ReflectResult:
        if self.native_reflect_allowed(as_of):
            kw = dict(bank_id=self.bank_id, query=question, budget=budget, include_facts=True)
            if response_schema is not None:
                kw["response_schema"] = response_schema
            if not self.use_mental_models:
                kw["exclude_mental_models"] = True
            resp = _call(self.client.reflect, **kw)
            based = _field(_field(resp, "based_on"), "memories") or []
            hits = [h for h, _ in (self._resolve(m, as_of) for m in based) if h]
            return ReflectResult(question, as_of, "native", _field(resp, "text"),
                                 sorted({h.doc_id for h in hits if h.doc_id}), hits,
                                 structured=_field(resp, "structured_output"))
        rec = self.recall(question, as_of, budget="high")
        text = self.synthesizer(question, rec.hits, self.policy_text) if self.synthesizer else None
        return ReflectResult(question, as_of, "local_filtered", text, rec.doc_ids(), rec.hits)

    def get_document(self, doc_id: str, as_of: str) -> dict | None:
        doc = self.index.by_id.get(doc_id)
        if doc is None or not self.index.visible(doc, as_of):
            return None
        return {"doc_id": doc.doc_id, "date": doc.date, "doc_type": doc.doc_type, "context": doc.context,
                "content": doc.content, "source_record_ids": list(doc.source_record_ids),
                "supersedes": list(doc.supersedes),
                "superseded_by": [{"doc_id": d.doc_id, "date": d.date} for d in self.index.superseding(doc_id, as_of)]}

    def retain_experience(self, *, document_id: str, content: str, context: str, timestamp: str,
                          metadata: dict) -> str:
        try:
            _call(self.client.retain, bank_id=self.bank_id, content=content, context=context, timestamp=timestamp,
                  document_id=document_id, metadata=metadata, tags=["source:live"])
            status = "ok"
        except Exception as ex:  # external service: record the failure, the DB rows stay the source of truth
            status = f"error: {ex}"
        self.ledger.append({"bank_id": self.bank_id, "document_id": document_id, "timestamp": timestamp,
                            "status": status, "logged_at": datetime.now(timezone.utc).isoformat()})
        return status


def build_memory(settings: Settings, live_memory_ids: Callable[[], set[str]],
                 synthesizer: Callable[[str, list[MemoryHit], str], str] | None = None) -> HindsightMemory:
    return HindsightMemory(make_client(settings), settings.hindsight_bank_id, load_index(settings.corpus_path),
                           RetainLedger(settings.retain_ledger_path), live_memory_ids, synthesizer,
                           load_bank_policy(), use_mental_models=settings.mental_models_in_reflect)
```

- [ ] **Step 5: Run the unit tests**

Run: `.venv/bin/python -m pytest tests/test_hindsight_tools.py -v`
Expected: 11 passed

- [ ] **Step 6: Add and run a live recall test (Stage 1 gate on the real bank)**

Append to `tests/test_hindsight_tools.py`:
```python
@pytest.mark.live
def test_live_recall_is_resolvable(base_settings):
    from agent.hindsight_tools import build_memory

    mem = build_memory(base_settings, lambda: set())
    r = mem.recall("Why did RPO003179 slip and what is its latest ETA?", "2023-06-11")
    assert r.hits, f"no visible hits: raw={r.raw_count} future={r.dropped_future} unresolved={r.dropped_unresolved}"
    assert any("RPO003179" in h.text for h in r.hits)
```
Run: `RUN_LIVE=1 .venv/bin/python -m pytest tests/test_hindsight_tools.py -k live -v`
Expected: PASS. If every hit is `unresolved`, the bank stores neither `document_id` nor a `mentioned_at` equal to the doc timestamp. Print one raw result (`client.recall(...).results[0]`) and extend `DocIndex.resolve` to match the field that carries the doc timestamp (e.g. `occurred_start`). Add a unit test for that field before changing the code.

- [ ] **Step 7: Commit**

```bash
git add agent/corpus.py agent/hindsight_tools.py tests/test_hindsight_tools.py
git commit -m "feat: Hindsight memory tools with as-of filtering, supersession and reflect guard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: LLM wrapper with effort, caching, refusal fallback and cost accounting

**Files:**
- Create: `agent/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `Settings`.
- Produces:
  - `PRICES: dict[str, tuple[float, float]]`
  - `Usage` (fields `calls, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd`) with `add(usage, model)` and `to_dict()`
  - `LLMRefusal(RuntimeError)`
  - `LLM(settings, client=None)` with attributes `model`, `usage`, and `create(*, system: str, messages: list, tools: list | None = None, effort: str = "low", temperature: float | None = None) -> response`
  - Response contract: `.content` (blocks with `.type` in `text` / `tool_use` / other), `.stop_reason`, `.usage`.

- [ ] **Step 1: Write the failing tests**

`tests/test_llm.py`:
```python
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.llm import LLM, LLMRefusal, Usage


def _resp(stop="end_turn", i=1000, o=100, cr=0, cw=0):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr,
                                                 cache_creation_input_tokens=cw))


class FakeClient:
    def __init__(self, resp):
        self.kw = None
        outer = self

        class _Msgs:
            def create(self, **kw):
                outer.kw, outer.path = kw, "messages"
                return resp

        class _BetaMsgs:
            def create(self, **kw):
                outer.kw, outer.path = kw, "beta"
                return resp

        self.messages = _Msgs()
        self.beta = SimpleNamespace(messages=_BetaMsgs())


def test_request_shape_with_fallback(settings):
    c = FakeClient(_resp())
    llm = LLM(replace(settings, llm_refusal_fallback="default"), client=c)
    llm.create(system="sys", messages=[{"role": "user", "content": "hi"}], tools=[{"name": "t"}], effort="low")
    assert c.path == "beta"
    assert c.kw["betas"] == ["server-side-fallback-2026-07-01"] and c.kw["fallbacks"] == "default"
    assert c.kw["output_config"] == {"effort": "low"}
    assert c.kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in c.kw and c.kw["tools"] == [{"name": "t"}]


def test_no_fallback_and_temperature_when_configured(settings):
    c = FakeClient(_resp())
    llm = LLM(replace(settings, llm_refusal_fallback=""), client=c)
    llm.create(system="s", messages=[], effort="high", temperature=0.2)
    assert c.path == "messages" and c.kw["temperature"] == 0.2 and "tools" not in c.kw


def test_usage_and_cost(settings):
    llm = LLM(replace(settings, llm_model="claude-opus-5"), client=FakeClient(_resp(i=1000, o=100, cr=2000, cw=400)))
    llm.create(system="s", messages=[])
    u = llm.usage.to_dict()
    assert u["calls"] == 1 and u["input_tokens"] == 1000 and u["cache_read_tokens"] == 2000
    # (1000*5 + 100*25 + 2000*5*0.1 + 400*5*1.25) / 1e6
    assert u["cost_usd"] == pytest.approx(0.0110)


def test_unknown_model_cost_is_none():
    u = Usage()
    u.add(SimpleNamespace(input_tokens=1, output_tokens=1), "some-future-model")
    assert u.cost_usd is None


def test_refusal_raises(settings):
    with pytest.raises(LLMRefusal):
        LLM(settings, client=FakeClient(_resp(stop="refusal"))).create(system="s", messages=[])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_llm.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'agent.llm'`

- [ ] **Step 3: Implement `agent/llm.py`**

```python
"""Thin wrapper over the Anthropic Messages API: model, effort, prompt caching, refusal fallback and
usage / cost accounting in one place."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import anthropic

from agent.config import Settings

# USD per million tokens (input, output), Claude API price table cached 2026-06.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0), "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0), "claude-sonnet-4-6": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_READ_MULT, CACHE_WRITE_MULT = 0.1, 1.25
NO_EFFORT_MODELS = ("claude-haiku-4-5",)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMRefusal(RuntimeError):
    """The model (and any fallback) declined the request."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = 0.0

    def add(self, u, model: str) -> None:
        i = getattr(u, "input_tokens", 0) or 0
        o = getattr(u, "output_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        self.calls += 1
        self.input_tokens += i
        self.output_tokens += o
        self.cache_read_tokens += cr
        self.cache_write_tokens += cw
        price = PRICES.get(model)
        if price is None or self.cost_usd is None:
            self.cost_usd = None
        else:
            pin, pout = price
            self.cost_usd += (i * pin + o * pout + cr * pin * CACHE_READ_MULT + cw * pin * CACHE_WRITE_MULT) / 1e6

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["cost_usd"] is not None:
            d["cost_usd"] = round(d["cost_usd"], 6)
        return d


class LLM:
    def __init__(self, settings: Settings, client=None):
        self.model = settings.llm_model
        self.max_tokens = settings.llm_max_tokens
        self.fallback = settings.llm_refusal_fallback
        self.client = client or anthropic.Anthropic()
        self.usage = Usage()

    def create(self, *, system: str, messages: list, tools: list | None = None, effort: str = "low",
               temperature: float | None = None):
        kw = {"model": self.model, "max_tokens": self.max_tokens, "messages": messages,
              "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]}
        if tools:
            kw["tools"] = tools
        if not self.model.startswith(NO_EFFORT_MODELS):
            kw["output_config"] = {"effort": effort}
        if temperature is not None:
            kw["temperature"] = temperature
        if self.fallback:
            resp = self.client.beta.messages.create(**kw, betas=[FALLBACK_BETA], fallbacks=self.fallback)
        else:
            resp = self.client.messages.create(**kw)
        self.usage.add(resp.usage, self.model)
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMRefusal(f"model declined the request ({getattr(details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("response hit max_tokens; raise LLM_MAX_TOKENS")
        return resp
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_llm.py -v`
Expected: 5 passed

- [ ] **Step 5: Live smoke (one call)**

Append to `tests/test_llm.py`:
```python
@pytest.mark.live
def test_live_round_trip(base_settings):
    llm = LLM(base_settings)
    r = llm.create(system="Reply with the single word OK.", messages=[{"role": "user", "content": "ping"}])
    assert any(b.type == "text" for b in r.content) and llm.usage.calls == 1
```
Run: `RUN_LIVE=1 .venv/bin/python -m pytest tests/test_llm.py -k live -v`
Expected: PASS. If the beta create rejects `fallbacks`, the installed SDK predates the parameter. Upgrade `anthropic`, or set `LLM_REFUSAL_FALLBACK=` and note it in the README limitations.

- [ ] **Step 6: Commit**

```bash
git add agent/llm.py tests/test_llm.py
git commit -m "feat: Anthropic wrapper with effort, caching, refusal fallback and cost accounting

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Report parsing and guardrails

**Files:**
- Create: `agent/parsing.py`, `agent/guardrails.py`
- Test: `tests/test_parsing_guardrails.py`

**Interfaces:**
- Consumes: `TAXONOMY`, `best_option` (Task 4); `record_exists` (Task 3).
- Produces:
  - `agent.parsing.ReportEntities` with fields `report_date, supplier_ids, rm_ids, plant_ids, rpo_ids, run_ids, warehouse_ids, event_ids, proposed_action`, plus `to_dict()`
  - `parse_report(text: str) -> ReportEntities`
  - `agent.guardrails.action_violations(action: str, options: list[dict]) -> list[str]`
  - `split_citations(con, doc_ids, record_ids, seen_doc_ids: set[str]) -> tuple[list[str], list[str], list[str]]` returning (valid docs, valid records, unverifiable)
  - `ungrounded_numbers(text: str, options: list[dict], extra_values=()) -> list[str]`

- [ ] **Step 1: Write the failing tests**

`tests/test_parsing_guardrails.py`:
```python
import pytest

from agent.guardrails import action_violations, split_citations, ungrounded_numbers
from agent.parsing import parse_report

HS01 = ("2025-10-01 - Supplier 192 (SUP0192) just moved RPO023175 (PP Impact Copolymer Grade B, RM0079) for "
        "Riverbend Plant (P04) to 2025-10-30. We have about 111.9 kg on hand vs safety stock 105.2; run PR0021648 "
        "is planned 2025-10-21 and needs 178.0 kg. 1 run(s) and ~598 FG units for W012 depend on it. What should we do?")
CONFLICT = ("2025-01-13 - Supplier 169 (SUP0169) just moved RPO016183 (Elastic Film Laminate, RM0100) for Eastfield "
            "Plant (P03) to 2025-03-10. Run PR0016522 is planned 2025-02-17. The buyer proposes cancelling "
            "RPO016183 and re-buying elsewhere. What should we do?")


def test_parse_holdout_report():
    e = parse_report(HS01)
    assert e.report_date == "2025-10-01"
    assert (e.supplier_ids, e.rm_ids, e.plant_ids) == (["SUP0192"], ["RM0079"], ["P04"])
    assert (e.rpo_ids, e.run_ids, e.warehouse_ids) == (["RPO023175"], ["PR0021648"], ["W012"])
    assert e.proposed_action is None


def test_parse_proposed_action():
    assert parse_report(CONFLICT).proposed_action == "cancel_po"
    assert parse_report("Planner suggests we expedite RPO1 by air.").proposed_action == "expedite"
    assert parse_report("Should we cancel? Nobody proposed anything.").proposed_action is None


OPTS = [
    {"action": "accept_delay", "feasible": True, "supported": True, "cost": 311.0, "score": 311.0, "stockout_days": 21, "breaches_commitments": []},
    {"action": "switch_supplier", "feasible": True, "supported": True, "cost": 1.0, "score": 1.0, "stockout_days": 0, "breaches_commitments": []},
    {"action": "cancel_po", "feasible": True, "supported": True, "cost": 8.0, "score": 11.0, "stockout_days": 0, "breaches_commitments": ["CMT00560"]},
    {"action": "substitute_rm", "feasible": False, "supported": True, "note": "no substitute"},
    {"action": "renegotiate", "feasible": False, "supported": False, "note": "not modelled"},
]


@pytest.mark.parametrize("action,needle", [
    ("cancel_po", "CMT00560"), ("substitute_rm", "infeasible"), ("renegotiate", "not modelled"),
    ("expedite", "not simulated"), ("teleport", "taxonomy"),
])
def test_action_violations(action, needle):
    v = action_violations(action, OPTS)
    assert v and needle in v[0]


def test_no_violation_for_clean_action():
    assert action_violations("switch_supplier", OPTS) == []


def test_split_citations(db_factory):
    con = db_factory("2025-01-13")
    docs, recs, bad = split_citations(con, ["DOC000001", "DOC000002", "DOC000001"],
                                      ["CMT00560", "EVT99999", "RM0100|P03|2025-01-12", "RPO016183"], {"DOC000001"})
    assert docs == ["DOC000001"]
    assert recs == ["CMT00560", "RPO016183"]
    assert bad == ["DOC000002", "EVT99999", "RM0100|P03|2025-01-12"]


def test_ungrounded_numbers():
    ok = "Switch supplier costs $1 with 0 stockout days vs accept delay at $311 and 21 stockout days. QA takes ~6 days."
    assert ungrounded_numbers(ok, OPTS) == []
    bad = "Expedite would cost about $5,000 and cause 3 stockout days."
    assert ungrounded_numbers(bad, OPTS) == ["$5,000", "3 stockout days"]
    assert ungrounded_numbers("DEC00353 cost $11,555 in the end.", OPTS, extra_values=[11555.42]) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_parsing_guardrails.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'agent.guardrails'`

- [ ] **Step 3: Implement `agent/parsing.py`**

```python
"""Pull entity ids and an optional proposed action out of a free-text disruption report."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

_IDS = {
    "supplier_ids": r"\bSUP\d{4}\b", "rm_ids": r"\bRM\d{4}\b", "plant_ids": r"\bP\d{2}\b",
    "rpo_ids": r"\bRPO\d+\b", "run_ids": r"\bPR\d+\b", "warehouse_ids": r"\bW\d{3}\b", "event_ids": r"\bEVT\d+\b",
}
_PROPOSAL_CUE = re.compile(r"\b(propos\w*|suggest\w*|recommend\w*|wants? to|plans? to)\b", re.I)
_ACTION_WORDS = [  # checked in order; first match wins
    ("cancel_po", r"\bcancel"), ("expedite", r"\bexpedit|\bair[- ]freight"),
    ("switch_supplier", r"\bswitch\w* supplier|\balternat\w* supplier|\bspot (?:buy|lot)"),
    ("substitute_rm", r"\bsubstitut"), ("reallocate_stock", r"\btransfer|\breallocat"),
    ("build_safety_stock", r"\bsafety stock"), ("renegotiate", r"\brenegotiat"),
    ("reduce_allocation", r"\breduce\w* allocation"), ("accept_delay", r"\baccept\w* the delay|\bwait for"),
]


@dataclass
class ReportEntities:
    report_date: str | None
    supplier_ids: list[str]
    rm_ids: list[str]
    plant_ids: list[str]
    rpo_ids: list[str]
    run_ids: list[str]
    warehouse_ids: list[str]
    event_ids: list[str]
    proposed_action: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _proposed_action(text: str) -> str | None:
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if not _PROPOSAL_CUE.search(sentence):
            continue
        for action, pattern in _ACTION_WORDS:
            if re.search(pattern, sentence, re.I):
                return action
    return None


def parse_report(text: str) -> ReportEntities:
    date = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    ids = {k: list(dict.fromkeys(re.findall(p, text))) for k, p in _IDS.items()}
    return ReportEntities(report_date=date.group(1) if date else None, proposed_action=_proposed_action(text), **ids)
```

- [ ] **Step 4: Implement `agent/guardrails.py`**

```python
"""Deterministic checks applied to the LLM's recommendation and prose before they reach the card."""
from __future__ import annotations

import re

from agent.simulate import TAXONOMY
from agent.sql_tools import record_exists

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
_STOCKOUT_DAYS = re.compile(r"(\d+(?:\.\d+)?)\s+(?:stockout|stock-out|blocked)\s+days?", re.I)


def action_violations(action: str, options: list[dict]) -> list[str]:
    if action not in TAXONOMY:
        return [f"{action!r} is not in the decision taxonomy {TAXONOMY}"]
    opt = next((o for o in options if o["action"] == action), None)
    if opt is None:
        return [f"{action} was not simulated for this disruption"]
    if not opt.get("supported", True):
        return [f"{action} is not modelled by the simulator, so its consequences cannot be quantified"]
    if not opt["feasible"]:
        return [f"{action} is infeasible: {opt.get('note', '')}"]
    if opt.get("breaches_commitments"):
        return [f"{action} would breach open commitment(s) {', '.join(opt['breaches_commitments'])}"]
    return []


def split_citations(con, doc_ids, record_ids, seen_doc_ids: set[str]):
    docs, recs, bad = [], [], []
    for d in dict.fromkeys(doc_ids):
        (docs if d in seen_doc_ids else bad).append(d)
    for r in dict.fromkeys(record_ids):
        (recs if record_exists(con, r) else bad).append(r)  # None (unrecognised id) is unverifiable
    return docs, recs, bad


def ungrounded_numbers(text: str, options: list[dict], extra_values=()) -> list[str]:
    feasible = [o for o in options if o.get("feasible")]
    money = {float(o[k]) for o in feasible for k in ("cost", "score") if o.get(k) is not None}
    money |= {float(v) for v in extra_values if v is not None}
    days = {float(o["stockout_days"]) for o in feasible}
    out = []
    for m in _MONEY.finditer(text):
        v = float(m.group(1).replace(",", ""))
        if not any(abs(v - x) <= 1 for x in money):
            out.append(m.group(0))
    for m in _STOCKOUT_DAYS.finditer(text):
        if float(m.group(1)) not in days:
            out.append(m.group(0))
    return out
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_parsing_guardrails.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add agent/parsing.py agent/guardrails.py tests/test_parsing_guardrails.py
git commit -m "feat: report parsing and deterministic guardrails

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Agent core - decision loop with guardrail enforcement (Stage 2)

**Files:**
- Create: `agent/card.py`, `agent/agent_core.py`, `tests/fakes.py`
- Test: `tests/test_agent_core.py`

**Interfaces:**
- Consumes: everything from Tasks 2-7.
- Produces:
  - `agent.card.DecisionCard` (dataclass, fields in code) with `to_dict()`; `QAResult` with `to_dict()`
  - `agent.agent_core.AgentError(RuntimeError)`, `MemoryUnavailable(AgentError)`
  - `DecisionAgent(settings, llm, memory, live_db_path)` with `run(report, *, as_of=None, context=None, writeback=False) -> DecisionCard` and `answer_question(question, as_of) -> QAResult`
  - `build_precedents(decisions, options, limit=6) -> list[dict]`; `annotate_commitments(commitments, options) -> list[dict]`
  - `make_synthesizer(llm, settings) -> Callable`; `build_agent(settings, *, live_db_path=None, llm=None, memory=None) -> DecisionAgent`
  - `context` keys: `rpo_id`, `run_ids`, `rm_id` (all optional).
- `tests/fakes.py` produces `FakeLLM(script)`, `FakeMemory(hits=(), fail=False)`, `tool_use(name, input)`, `text(t)`, `response(*blocks, stop="tool_use")`, `submission(action, **overrides)`.

- [ ] **Step 1: Write `agent/card.py`**

```python
"""Output records of the agent."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class DecisionCard:
    run_id: str
    as_of: str
    report: str
    entities: dict
    situation_summary: str = ""
    scenario_state: dict | None = None
    options: list[dict] = field(default_factory=list)
    simulator_best_action: str | None = None
    recommended_action: str | None = None
    deviates_from_simulator_best: bool = False
    confidence: str = "low"
    rationale: str = ""
    key_reasons: list[str] = field(default_factory=list)
    precedents: list[dict] = field(default_factory=list)
    precedent_assessments: list[dict] = field(default_factory=list)
    related_event_ids: list[str] = field(default_factory=list)
    open_commitments: list[dict] = field(default_factory=list)
    cited_doc_ids: list[str] = field(default_factory=list)
    cited_record_ids: list[str] = field(default_factory=list)
    unverifiable_citations: list[str] = field(default_factory=list)
    ungrounded_claims: list[str] = field(default_factory=list)
    guardrail_events: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reflect_mode: str | None = None
    reflection: str | None = None
    memory_hits: int = 0
    dropped_future_hits: int = 0
    writeback: dict | None = None
    mock_actions: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    trace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QAResult:
    question: str
    as_of: str
    answer: str = ""
    cited_doc_ids: list[str] = field(default_factory=list)
    cited_record_ids: list[str] = field(default_factory=list)
    unverifiable_citations: list[str] = field(default_factory=list)
    confidence: str = "low"
    memory_hits: int = 0
    dropped_future_hits: int = 0
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    trace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
```

- [ ] **Step 2: Write `tests/fakes.py`**

```python
"""Deterministic stand-ins for Claude and Hindsight used by agent tests."""
from __future__ import annotations

import itertools
from types import SimpleNamespace

from agent.hindsight_tools import MemoryHit, RecallResult, ReflectResult
from agent.llm import Usage

_ids = itertools.count(1)


def tool_use(name, input):
    return SimpleNamespace(type="tool_use", id=f"tu_{next(_ids)}", name=name, input=input)


def text(t):
    return SimpleNamespace(type="text", text=t)


def response(*blocks, stop="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


def submission(action, **over):
    base = {"situation_summary": "summary", "recommended_action": action, "key_reasons": ["cheapest safe option"],
            "precedent_assessments": [], "commitments_considered": [], "cited_doc_ids": [], "cited_record_ids": [],
            "ungrounded_claims": [], "confidence": "medium"}
    return {**base, **over}


class FakeLLM:
    def __init__(self, script):
        self.script, self.calls, self.usage, self.model = list(script), [], Usage(), "fake-model"

    def create(self, **kw):
        # snapshot messages: the agent mutates the list after the call
        self.calls.append({**kw, "messages": list(kw["messages"])})
        if not self.script:
            raise AssertionError("FakeLLM script exhausted")
        resp = self.script.pop(0)
        self.usage.add(resp.usage, self.model)
        return resp


class FakeMemory:
    def __init__(self, hits=(), fail=False, reflect_text="fake reflection"):
        self.hits, self.fail, self.recalls, self.retained = list(hits), fail, [], []
        self.reflect_text = reflect_text

    def recall(self, query, as_of, *, window_days=None, budget="mid", max_tokens=4096):
        if self.fail:
            raise ConnectionError("connection refused")
        self.recalls.append((query, as_of, window_days))
        return RecallResult(query, as_of, list(self.hits), len(self.hits), 0, 0)

    def reflect(self, question, as_of, *, budget="mid", response_schema=None):
        self.reflects = getattr(self, "reflects", []) + [(question, as_of, budget, response_schema)]
        return ReflectResult(question, as_of, "local_filtered", self.reflect_text,
                             sorted({h.doc_id for h in self.hits if h.doc_id}), list(self.hits),
                             structured={"confidence": "medium"} if response_schema else None)

    def get_document(self, doc_id, as_of):
        return None

    def retain_experience(self, **kw):
        self.retained.append(kw)
        return "ok"


def corpus_hit(doc_id, date, text="memory fact"):
    return MemoryHit(text, "world", doc_id, date, "decision_memo", "corpus")
```

- [ ] **Step 3: Write the failing tests**

`tests/test_agent_core.py`:
```python
import json
from dataclasses import replace

import pytest

from agent.agent_core import DecisionAgent, MemoryUnavailable
from agent.scenarios import holdout_context, load_holdout
from agent.simulate import build_state, simulate_options
from tests.fakes import FakeLLM, FakeMemory, corpus_hit, response, submission, text, tool_use

CONFLICT = ("2025-01-13 - Supplier 169 (SUP0169) just moved RPO016183 (Elastic Film Laminate, RM0100) for Eastfield "
            "Plant (P03) to 2025-03-10 citing weather force majeure. Run PR0016522 is planned 2025-02-17 and needs "
            "this material. The buyer proposes cancelling RPO016183 and re-buying elsewhere. What should we do?")
CONFLICT_CTX = {"rpo_id": "RPO016183", "run_ids": ["PR0016522"], "rm_id": "RM0100"}


@pytest.fixture
def hs05(base_settings):
    return load_holdout(base_settings)["HS05"]


def agent(settings, script, hits=(), fail=False):
    # pin the knobs the assertions depend on, whatever the developer's .env says
    settings = replace(settings, llm_structured_effort="low", llm_rationale_effort="high",
                       llm_temperature_structured=None, llm_temperature_rationale=None)
    llm, mem = FakeLLM(script), FakeMemory(hits, fail)
    return DecisionAgent(settings, llm, mem, settings.live_db_path), llm, mem


def test_hs05_happy_path(settings, db_factory, hs05):
    a, llm, mem = agent(settings, [
        response(tool_use("simulate_action", {"action_type": "switch_supplier"})),
        response(tool_use("submit_recommendation", submission(
            "switch_supplier", cited_doc_ids=["DOC000001", "DOC999999"],
            cited_record_ids=["RPO023030", "DEC00353", "CMT99999"]))),
        response(text("Switch supplier: $2 and 0 stockout days [RPO023030]."), stop="end_turn"),
    ], hits=[corpus_hit("DOC000001", "2023-01-02")])
    card = a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))

    con = db_factory(hs05["day0"])
    expected = simulate_options(con, build_state(con, day0=hs05["day0"], **holdout_context(hs05)))
    assert card.options == expected
    assert card.recommended_action == card.simulator_best_action == "switch_supplier"
    assert not card.deviates_from_simulator_best
    # trap is visible: an accept_delay success precedent that does not apply today
    assert any(p["decision_type"] == "accept_delay" and p["outcome_label"] == "success" and not p["applies_today"]
               for p in card.precedents)
    assert sorted(c["commitment_id"] for c in card.open_commitments) == sorted(
        c["commitment_id"] for c in hs05["open_commitments"])
    assert card.cited_doc_ids == ["DOC000001"] and card.cited_record_ids == ["RPO023030", "DEC00353"]
    assert card.unverifiable_citations == ["DOC999999", "CMT99999"]
    assert card.warnings == []
    assert [c["effort"] for c in llm.calls] == ["low", "low", "high"]
    assert "tools" not in llm.calls[-1]
    assert {q[2] for q in mem.recalls} == {None, 120}  # broad, entity and temporal-window recalls
    assert card.usage["calls"] == 3 and card.trace


def test_commitment_conflict_is_caught_before_recommending(settings):
    a, llm, _ = agent(settings, [
        response(tool_use("submit_recommendation", submission("cancel_po"))),
        response(tool_use("submit_recommendation", submission("switch_supplier", cited_record_ids=["CMT00560"]))),
        response(text("Do not cancel: CMT00560 is an open volume commitment."), stop="end_turn"),
    ])
    card = a.run(CONFLICT, as_of="2025-01-13", context=CONFLICT_CTX)
    assert card.guardrail_events[0].startswith("Proposed action cancel_po blocked")
    assert any(e.startswith("Rejected cancel_po") and "CMT00560" in e for e in card.guardrail_events)
    rejection = llm.calls[1]["messages"][-1]["content"][0]
    assert rejection["is_error"] is True and "CMT00560" in rejection["content"]
    assert card.recommended_action == "switch_supplier"
    flagged = {c["commitment_id"]: c for c in card.open_commitments}
    assert flagged["CMT00560"]["affected_by"] == ["cancel_po"]


def test_persistent_violation_is_overridden(settings):
    a, _, _ = agent(settings, [response(tool_use("submit_recommendation", submission("cancel_po")))] * 3
                    + [response(text("Override applied."), stop="end_turn")])
    card = a.run(CONFLICT, as_of="2025-01-13", context=CONFLICT_CTX)
    assert card.recommended_action == card.simulator_best_action
    assert card.recommended_action != "cancel_po"
    assert any(e.startswith("Override:") for e in card.guardrail_events)


def test_no_rpo_skips_decision_llm(settings):
    a, llm, _ = agent(settings, [])
    card = a.run("Supplier 247 (SUP0247) is late again on Stretch Wrap. What do we do?", as_of="2025-10-14")
    assert card.recommended_action is None and card.options == []
    assert "names no RM purchase order" in card.warnings[0]
    assert llm.calls == []


def test_ungrounded_number_in_rationale_is_flagged(settings, hs05):
    a, _, _ = agent(settings, [
        response(tool_use("submit_recommendation", submission("switch_supplier"))),
        response(text("Switching costs $99,999 and avoids 4 stockout days."), stop="end_turn"),
    ])
    card = a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))
    assert any("$99,999" in w and "4 stockout days" in w for w in card.warnings)


def test_sql_guard_error_goes_back_to_model(settings, hs05):
    a, llm, _ = agent(settings, [
        response(tool_use("sql_query", {"query": "DELETE FROM decisions"})),
        response(tool_use("submit_recommendation", submission("switch_supplier"))),
        response(text("ok"), stop="end_turn"),
    ])
    a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))
    result = llm.calls[1]["messages"][-1]["content"][0]
    assert "only SELECT" in json.loads(result["content"])["error"]


def test_reflect_tool_available_to_model(settings, hs05):
    a, llm, _ = agent(settings, [
        response(tool_use("hindsight_reflect", {"question": "What happened with SUP0247 before?"})),
        response(tool_use("submit_recommendation", submission("switch_supplier", cited_doc_ids=["DOC000001"]))),
        response(text("ok"), stop="end_turn"),
    ], hits=[corpus_hit("DOC000001", "2023-01-02")])
    card = a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))
    out = json.loads(llm.calls[1]["messages"][-1]["content"][0]["content"])
    assert out["mode"] == "local_filtered" and out["doc_ids"] == ["DOC000001"]
    assert card.cited_doc_ids == ["DOC000001"]


def test_memory_failure_is_reported(settings, hs05):
    a, _, _ = agent(settings, [], fail=True)
    with pytest.raises(MemoryUnavailable, match="HINDSIGHT_BASE_URL"):
        a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))
```

- [ ] **Step 4: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_agent_core.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'agent.agent_core'`

- [ ] **Step 5: Implement `agent/agent_core.py`**

```python
"""Agent core.

run(): parse -> deterministic simulation -> parallel memory recalls + SQL lookups -> reflect ->
Claude tool loop at low effort, ending in submit_recommendation, which the guardrails check ->
Claude rationale at high effort, number-checked -> DecisionCard (+ optional write-back).
answer_question(): the same tools in a QA loop, used by the eval harness.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent import sql_tools
from agent.card import DecisionCard, QAResult
from agent.config import Settings
from agent.db import connect, live_memory_ids
from agent.guardrails import action_violations, split_citations, ungrounded_numbers
from agent.hindsight_tools import build_memory
from agent.llm import LLM, Usage
from agent.parsing import parse_report
from agent.simulate import TAXONOMY, SimulationError, best_option, build_state, simulate_options

MAX_STEPS = 10
MAX_REJECTIONS = 2


class AgentError(RuntimeError):
    """The agent could not produce a result."""


class MemoryUnavailable(AgentError):
    """Hindsight could not be reached; the agent refuses to reason as if memory were empty."""


def _j(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def _strict(name, description, properties, required=None):
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required or list(properties), "additionalProperties": False}}


_STR_LIST = {"type": "array", "items": {"type": "string"}}
TOOL_RECALL = _strict("hindsight_recall",
                      "Search institutional memory (Hindsight: semantic + keyword + graph + temporal). Only memories "
                      "dated on or before as_of are returned; each hit has a doc_id to cite and lists later docs that "
                      "supersede it.",
                      {"query": {"type": "string"},
                       "window_days": {"type": ["integer", "null"], "description": "only the last N days before as_of, or null"}})
TOOL_REFLECT = _strict("hindsight_reflect",
                       "Synthesis over memory guided by the bank's mission and directives (what happened, what was "
                       "tried, what worked, what is still open). Respects as_of; returns the doc_ids it drew on.",
                       {"question": {"type": "string"}})
TOOL_DOC = _strict("memory_get_document", "Read the full text of a memory document by doc_id (only if dated on or before as_of).",
                   {"doc_id": {"type": "string"}})
TOOL_SQL = _strict("sql_query",
                   "Read-only SQLite query (one SELECT/WITH statement, max 200 rows) over the supply-chain tables as "
                   "known on as_of. Use plain table names (no main./live. prefixes). Schema is in the evidence pack.",
                   {"query": {"type": "string"}})
TOOL_SIM = _strict("simulate_action",
                   "Deterministic consequence simulation of one candidate action for this disruption: cost (USD), "
                   "stockout days, service impact, risk, open commitments breached. The only permitted source of "
                   "consequence numbers.",
                   {"action_type": {"type": "string", "enum": list(TAXONOMY)}})
TOOL_SUBMIT = _strict("submit_recommendation", "Submit the final structured recommendation (call exactly once).", {
    "situation_summary": {"type": "string"},
    "recommended_action": {"type": "string", "enum": list(TAXONOMY)},
    "key_reasons": _STR_LIST,
    "precedent_assessments": {"type": "array", "items": {
        "type": "object", "properties": {"ref": {"type": "string"}, "applies_today": {"type": "boolean"},
                                         "why": {"type": "string"}},
        "required": ["ref", "applies_today", "why"], "additionalProperties": False}},
    "commitments_considered": _STR_LIST,
    "cited_doc_ids": _STR_LIST,
    "cited_record_ids": _STR_LIST,
    "ungrounded_claims": _STR_LIST,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
})
TOOL_ANSWER = _strict("submit_answer", "Submit the final answer (call exactly once).", {
    "answer": {"type": "string"}, "cited_doc_ids": _STR_LIST, "cited_record_ids": _STR_LIST,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]}})
DECIDE_TOOLS = [TOOL_RECALL, TOOL_REFLECT, TOOL_DOC, TOOL_SQL, TOOL_SIM, TOOL_SUBMIT]
QA_TOOLS = [TOOL_RECALL, TOOL_REFLECT, TOOL_DOC, TOOL_SQL, TOOL_ANSWER]

SYSTEM_DECIDE = """You are the Supply Chain Memory & Decision Agent for a multi-plant consumer-goods manufacturer.
You get a disruption report and an evidence pack: memory hits from the Hindsight bank (each with a doc_id), structured
records (with record ids), open commitments, prior decisions scored against today's options, and a deterministic
simulation of the candidate actions.

Rules:
- Every cost, stockout-day and service number must come from the simulation (options / simulate_action). Never estimate one.
- Cite doc_ids (DOC...) and record ids (EVT, DEC, DECL, CMT, CMTL, RPO, REV, PR, ...) for each claim. List anything you
  cannot ground under ungrounded_claims instead of asserting it.
- Check open commitments before choosing. Never recommend an action whose simulation lists breached commitments.
- A precedent is evidence, not an answer. For each precedent you rely on or reject, say whether today's conditions match
  (compare today's simulated score of that action with the best option) in precedent_assessments.
- When an older and a newer memory disagree, use the newer one and name the superseded doc.
- Only information dated on or before as_of exists.
Use tools only when the evidence pack is missing something, then call submit_recommendation."""

SYSTEM_RATIONALE = """Write the rationale section of a supply-chain decision card for planners, 120-220 words, plain prose.
Use only the facts in the JSON you are given. Quote costs and stockout days exactly as they appear in the options table
(USD with a $ sign). Cite doc_ids and record ids inline in square brackets. For any precedent you mention, say how today
differs. Name every open commitment the action touches. If ungrounded_claims is non-empty, say those points are not
backed by evidence. Mention any guardrail event."""

SYSTEM_QA = """You answer questions about a manufacturer's supply-chain history using institutional memory (Hindsight)
and the database, as known on as_of. Nothing dated after as_of exists. When sources conflict, use the most recent and
name the superseded doc. Quote ids, dates and numbers exactly as the sources state them. Cite every doc_id and record id
you used. If you cannot find the answer, say so. Answer in 1-3 sentences, then call submit_answer."""


def build_precedents(decisions: list[dict], options: list[dict], limit: int = 6) -> list[dict]:
    feasible = sorted((o for o in options if o.get("feasible")), key=lambda o: (o["score"], o["action"]))
    rank = {o["action"]: i + 1 for i, o in enumerate(feasible)}
    score = {o["action"]: o["score"] for o in feasible}
    best = feasible[0]["action"] if feasible else None
    out = []
    for d in decisions[:limit]:
        a = d["decision_type"]
        if best is None:
            note = "no simulation available today"
        elif a == best:
            note = "same action is the best option today"
        elif a in rank:
            note = (f"today {a} ranks {rank[a]} of {len(feasible)} "
                    f"(score {score[a]:,.0f} vs {score[best]:,.0f} for {best})")
        else:
            note = f"{a} is not a feasible simulated option today"
        out.append({**{k: d.get(k) for k in ("decision_id", "event_id", "decided_at", "decision_type", "outcome_label",
                                             "expected_cost", "actual_cost", "actual_stockout_days", "lesson_text", "source")},
                    "today_score": score.get(a), "today_rank": rank.get(a), "best_today": best,
                    "applies_today": a == best, "note": note})
    return out


def annotate_commitments(commitments: list[dict], options: list[dict]) -> list[dict]:
    return [{**c, "affected_by": [o["action"] for o in options if c["commitment_id"] in o.get("breaches_commitments", [])]}
            for c in commitments]


def _short(obj, n: int = 600) -> str:
    s = obj if isinstance(obj, str) else _j(obj)
    return s if len(s) <= n else s[:n] + "..."


class DecisionAgent:
    def __init__(self, settings: Settings, llm, memory, live_db_path: Path):
        self.settings, self.llm, self.memory, self.live_db_path = settings, llm, memory, live_db_path

    # ---------------------------------------------------------------- helpers
    def _connect(self, as_of: str) -> sqlite3.Connection:
        return connect(self.settings.db_path, self.live_db_path, as_of)

    @staticmethod
    def _trace(trace: list, kind: str, name: str, inp, out, t0: float) -> None:
        trace.append({"kind": kind, "name": name, "input": _short(inp), "output": _short(out),
                      "ms": round((time.monotonic() - t0) * 1000)})

    @staticmethod
    def _tool_result(block, content: str, error: bool = False) -> dict:
        r = {"type": "tool_result", "tool_use_id": block.id, "content": content}
        if error:
            r["is_error"] = True
        return r

    def _run_tool(self, con, block, as_of: str, state, seen_docs: set, trace: list) -> str:
        t0 = time.monotonic()
        name, inp = block.name, block.input
        try:
            if name == "hindsight_recall":
                try:
                    r = self.memory.recall(inp["query"], as_of, window_days=inp.get("window_days"))
                except Exception as ex:  # external service error goes back to the model as a tool error
                    raise AgentError(f"Hindsight recall failed: {ex}") from ex
                seen_docs.update(h.doc_id for h in r.hits if h.doc_id)
                out = r.to_payload()
            elif name == "hindsight_reflect":
                try:
                    refl = self.memory.reflect(inp["question"], as_of)
                except Exception as ex:  # external service error goes back to the model as a tool error
                    raise AgentError(f"Hindsight reflect failed: {ex}") from ex
                seen_docs.update(h.doc_id for h in refl.hits if h.doc_id)
                out = {"mode": refl.mode, "text": refl.text, "doc_ids": refl.doc_ids,
                       "memories": [h.to_dict() for h in refl.hits[:15]]}
            elif name == "memory_get_document":
                out = self.memory.get_document(inp["doc_id"], as_of) or {
                    "error": f"{inp['doc_id']} does not exist or is dated after {as_of}"}
                if "doc_id" in out:
                    seen_docs.add(out["doc_id"])
            elif name == "sql_query":
                out = sql_tools.run_sql(con, inp["query"])
            elif name == "simulate_action":
                if state is None:
                    out = {"error": "no scenario state for this report - simulation unavailable"}
                else:
                    out = next(o for o in simulate_options(con, state) if o["action"] == inp["action_type"])
            else:
                out = {"error": f"unknown tool {name}"}
        except (sql_tools.SQLGuardError, sqlite3.Error, SimulationError, AgentError) as ex:
            out = {"error": str(ex)}
        self._trace(trace, "tool", name, inp, out, t0)
        return _j(out)

    def _gather(self, report: str, ent_str: str, as_of: str) -> dict:
        queries = {"broad": (report, None),
                   "entity": (f"History of disruptions, decisions, negotiations and commitments involving {ent_str}", None),
                   "recent": (f"Recent problems, promises and changes for {ent_str}", 120)}
        try:
            with ThreadPoolExecutor(max_workers=3) as ex:
                futs = {k: ex.submit(self.memory.recall, q, as_of, window_days=w) for k, (q, w) in queries.items()}
                return {k: f.result() for k, f in futs.items()}
        except Exception as ex:  # any client/network error: refuse to continue on empty memory
            raise MemoryUnavailable(f"Hindsight recall failed ({ex}); check HINDSIGHT_BASE_URL / "
                                    f"HINDSIGHT_BANK_ID") from ex

    # ---------------------------------------------------------------- decision mode
    def run(self, report: str, *, as_of: str | None = None, context: dict | None = None,
            writeback: bool = False) -> DecisionCard:
        t0 = time.monotonic()
        self.llm.usage = Usage()
        ctx = context or {}
        ents = parse_report(report)
        as_of = as_of or ents.report_date or self.settings.default_as_of
        card = DecisionCard(run_id=f"run-{uuid.uuid4().hex[:8]}", as_of=as_of, report=report, entities=ents.to_dict())
        con = self._connect(as_of)
        try:
            sup = (ents.supplier_ids or [None])[0]
            rm = ctx.get("rm_id") or (ents.rm_ids or [None])[0]
            plant = (ents.plant_ids or [None])[0]
            rpo = ctx.get("rpo_id") or (ents.rpo_ids or [None])[0]
            runs = list(ctx.get("run_ids") or ents.run_ids)

            # 1. deterministic simulation (also canonicalises supplier / plant / rm from the PO)
            state, ts = None, time.monotonic()
            try:
                if not rpo:
                    raise SimulationError("the report names no RM purchase order (RPO...)")
                state = build_state(con, rpo_id=rpo, run_ids=runs, day0=as_of, rm_id=rm)
                card.options = simulate_options(con, state)
                card.scenario_state = state.to_dict()
                sup, rm, plant = state.supplier_id, state.rm_id, state.plant_id
                best = best_option(card.options)
                card.simulator_best_action = best["action"] if best else None
                self._trace(card.trace, "step", "simulate_options", card.scenario_state,
                            {o["action"]: o.get("score") for o in card.options}, ts)
            except SimulationError as ex:
                card.warnings.append(f"Cannot simulate actions: {ex}. No consequence numbers are shown.")

            # 2. evidence fan-out: Hindsight in parallel, SQL on this thread
            ts = time.monotonic()
            ent_str = ", ".join(x for x in (sup, rm, plant, rpo, *runs) if x) or report[:200]
            recalls = self._gather(report, ent_str, as_of)
            commitments = sql_tools.open_commitments(con, [sup, plant], as_of)
            scorecard = sql_tools.supplier_scorecard(con, sup) if sup else []
            events = sql_tools.prior_events(con, sup, rm, plant)
            links = sql_tools.event_links_for(con, [e["event_id"] for e in events])
            decisions = sql_tools.prior_decisions(con, sup, rm, as_of)
            card.related_event_ids = [e["event_id"] for e in events]
            card.precedents = build_precedents(decisions, card.options)
            card.open_commitments = annotate_commitments(commitments, card.options)
            self._trace(card.trace, "step", "gather", ent_str,
                        {k: len(r.hits) for k, r in recalls.items()} | {"commitments": len(commitments),
                                                                          "events": len(events), "decisions": len(decisions)}, ts)

            # 3. reflect
            ts = time.monotonic()
            refl = self.memory.reflect(f"Regarding {ent_str}: what happened before, what was tried, what worked or failed "
                                       f"and why, and what is still open or promised?", as_of)
            card.reflect_mode, card.reflection = refl.mode, refl.text
            self._trace(card.trace, "step", "reflect", refl.mode, refl.text or "", ts)
            hits, seen = [], set()
            for h in [*(h for r in recalls.values() for h in r.hits), *refl.hits]:
                key = (h.doc_id, h.text)
                if key not in seen:
                    seen.add(key)
                    hits.append(h)
            card.memory_hits = len(hits)
            card.dropped_future_hits = sum(r.dropped_future for r in recalls.values())
            seen_docs = {h.doc_id for h in hits if h.doc_id}

            if ents.proposed_action and card.options:
                v = action_violations(ents.proposed_action, card.options)
                if v:
                    card.guardrail_events.append(
                        f"Proposed action {ents.proposed_action} blocked before recommendation: {v[0]}")

            if not card.options:
                card.rationale = ("No recommendation: consequences could not be simulated, and the agent does not "
                                  "estimate them. Provide the RPO id and the affected production run ids.")
                return self._finish(card, t0)

            # 4-6. decide (low effort, guarded)
            evidence = {
                "as_of": as_of, "report": report, "entities": card.entities, "scenario_state": card.scenario_state,
                "options": card.options, "simulator_best_action": card.simulator_best_action,
                "precedents": card.precedents, "open_commitments": card.open_commitments,
                "supplier_scorecard_recent": scorecard, "prior_events": events, "event_links": links,
                "memory_hits": [h.to_dict() for h in hits[:30]],
                "reflection": {"mode": refl.mode, "text": refl.text},
                "guardrail_prechecks": card.guardrail_events, "decision_taxonomy": list(TAXONOMY),
                "schema": sql_tools.schema_summary(con),
            }
            sub = self._decide(con, evidence, card, seen_docs, as_of, state)
            card.recommended_action = sub["recommended_action"]
            card.situation_summary = sub["situation_summary"]
            card.key_reasons = sub["key_reasons"]
            card.precedent_assessments = sub["precedent_assessments"]
            card.ungrounded_claims = sub["ungrounded_claims"]
            card.confidence = sub["confidence"]
            card.cited_doc_ids, card.cited_record_ids, card.unverifiable_citations = split_citations(
                con, sub["cited_doc_ids"], sub["cited_record_ids"], seen_docs)
            if not card.cited_doc_ids and not card.cited_record_ids:
                card.warnings.append("The recommendation cites no traceable evidence.")
            card.deviates_from_simulator_best = card.recommended_action != card.simulator_best_action

            # rationale (high effort), then number check
            card.rationale = self._rationale(card)
            extra = [v for p in card.precedents for v in (p.get("expected_cost"), p.get("actual_cost"))]
            bad = ungrounded_numbers(card.rationale, card.options, extra)
            if bad:
                card.warnings.append("Rationale quotes numbers not produced by the simulator or the database: "
                                     + ", ".join(bad))
        finally:
            con.close()

        # 7. optional write-back (own connection to the live DB)
        if writeback and card.recommended_action:
            from agent.log_decision import log_decision
            card.writeback, card.mock_actions = log_decision(self.settings, self.live_db_path, self.memory, card, state)
        return self._finish(card, t0)

    def _decide(self, con, evidence: dict, card: DecisionCard, seen_docs: set, as_of: str, state) -> dict:
        messages = [{"role": "user", "content": "Evidence pack (JSON):\n" + _j(evidence)
                     + "\n\nDecide. Use tools only if something is missing, then call submit_recommendation."}]
        rejections = 0
        for _ in range(MAX_STEPS):
            ts = time.monotonic()
            resp = self.llm.create(system=SYSTEM_DECIDE, messages=messages, tools=DECIDE_TOOLS,
                                   effort=self.settings.llm_structured_effort,
                                   temperature=self.settings.llm_temperature_structured)
            messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if b.type == "tool_use"]
            self._trace(card.trace, "llm", "decide", f"turn {len(messages) // 2}", [u.name for u in uses], ts)
            if not uses:
                messages.append({"role": "user", "content": "Call submit_recommendation now."})
                continue
            results, accepted = [], None
            for u in uses:
                if u.name != "submit_recommendation":
                    results.append(self._tool_result(u, self._run_tool(con, u, as_of, state, seen_docs, card.trace)))
                    continue
                action = u.input["recommended_action"]
                violations = action_violations(action, card.options)
                if violations and rejections < MAX_REJECTIONS:
                    rejections += 1
                    card.guardrail_events.append(f"Rejected {action}: {violations[0]}")
                    results.append(self._tool_result(
                        u, "REJECTED by guardrail: " + "; ".join(violations) + ". Choose another action.", error=True))
                elif violations:
                    safe = best_option(card.options)
                    safe_action = safe["action"] if safe else None
                    card.guardrail_events.append(f"Override: {action} still violates ({violations[0]}); using "
                                                 f"{safe_action} (lowest risk-adjusted score without violations)")
                    accepted = {**u.input, "recommended_action": safe_action}
                    results.append(self._tool_result(u, "accepted with guardrail override"))
                else:
                    accepted = u.input
                    results.append(self._tool_result(u, "accepted"))
            messages.append({"role": "user", "content": results})
            if accepted:
                return accepted
        raise AgentError(f"no recommendation after {MAX_STEPS} model turns")

    def _rationale(self, card: DecisionCard) -> str:
        facts = {"as_of": card.as_of, "recommended_action": card.recommended_action,
                 "simulator_best_action": card.simulator_best_action, "situation_summary": card.situation_summary,
                 "key_reasons": card.key_reasons, "options": [o for o in card.options if o.get("feasible")],
                 "precedents": card.precedents, "precedent_assessments": card.precedent_assessments,
                 "open_commitments": card.open_commitments, "cited_doc_ids": card.cited_doc_ids,
                 "cited_record_ids": card.cited_record_ids, "ungrounded_claims": card.ungrounded_claims,
                 "guardrail_events": card.guardrail_events}
        ts = time.monotonic()
        resp = self.llm.create(system=SYSTEM_RATIONALE, messages=[{"role": "user", "content": _j(facts)}],
                               effort=self.settings.llm_rationale_effort,
                               temperature=self.settings.llm_temperature_rationale)
        out = "".join(b.text for b in resp.content if b.type == "text").strip()
        self._trace(card.trace, "llm", "rationale", "", out, ts)
        return out

    def _finish(self, card: DecisionCard, t0: float) -> DecisionCard:
        card.usage = self.llm.usage.to_dict()
        card.latency_s = round(time.monotonic() - t0, 2)
        return card

    # ---------------------------------------------------------------- QA mode (eval harness)
    def answer_question(self, question: str, as_of: str) -> QAResult:
        t0 = time.monotonic()
        self.llm.usage = Usage()
        res = QAResult(question=question, as_of=as_of)
        con = self._connect(as_of)
        try:
            try:
                rec = self.memory.recall(question, as_of)
            except Exception as ex:  # any client/network error
                raise MemoryUnavailable(f"Hindsight recall failed ({ex}); check HINDSIGHT_BASE_URL") from ex
            res.memory_hits, res.dropped_future_hits = len(rec.hits), rec.dropped_future
            seen = set(rec.doc_ids())
            messages = [{"role": "user", "content": f"as_of: {as_of}\nQuestion: {question}\n\nInitial memory recall "
                         f"(JSON):\n{_j(rec.to_payload())}\n\nSchema:\n{sql_tools.schema_summary(con)}"}]
            for _ in range(MAX_STEPS):
                ts = time.monotonic()
                resp = self.llm.create(system=SYSTEM_QA, messages=messages, tools=QA_TOOLS,
                                       effort=self.settings.llm_structured_effort,
                                       temperature=self.settings.llm_temperature_structured)
                messages.append({"role": "assistant", "content": resp.content})
                uses = [b for b in resp.content if b.type == "tool_use"]
                self._trace(res.trace, "llm", "qa", "", [u.name for u in uses], ts)
                if not uses:
                    messages.append({"role": "user", "content": "Call submit_answer now."})
                    continue
                results, final = [], None
                for u in uses:
                    if u.name == "submit_answer":
                        final = u.input
                        results.append(self._tool_result(u, "accepted"))
                    else:
                        results.append(self._tool_result(u, self._run_tool(con, u, as_of, None, seen, res.trace)))
                messages.append({"role": "user", "content": results})
                if final:
                    res.answer, res.confidence = final["answer"], final["confidence"]
                    res.cited_doc_ids, res.cited_record_ids, res.unverifiable_citations = split_citations(
                        con, final["cited_doc_ids"], final["cited_record_ids"], seen)
                    res.usage = self.llm.usage.to_dict()
                    res.latency_s = round(time.monotonic() - t0, 2)
                    return res
            raise AgentError(f"no answer after {MAX_STEPS} model turns")
        finally:
            con.close()


def make_synthesizer(llm, settings: Settings):
    """Local stand-in for Hindsight reflect when native reflect could see post-as_of memory."""
    def synthesize(question: str, hits, policy_text: str) -> str:
        resp = llm.create(system=policy_text + "\nSynthesize only from the memories given. Cite doc_ids in "
                                               "brackets. Say what is uncertain or missing.",
                          messages=[{"role": "user", "content": _j({"question": question,
                                                                    "memories": [h.to_dict() for h in hits[:40]]})}],
                          effort=settings.llm_structured_effort, temperature=settings.llm_temperature_structured)
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    return synthesize


def build_agent(settings: Settings, *, live_db_path: Path | None = None, llm=None, memory=None) -> DecisionAgent:
    live = live_db_path or settings.live_db_path
    llm = llm or LLM(settings)
    if memory is None:
        memory = build_memory(settings, lambda: live_memory_ids(live), synthesizer=make_synthesizer(llm, settings))
    return DecisionAgent(settings, llm, memory, live)


if __name__ == "__main__":  # Stage 2 smoke: one hardcoded scenario end to end with the full trace
    from agent.config import load_settings
    from agent.scenarios import holdout_context, load_holdout
    from agent.setup_data import ensure_db

    s = load_settings()
    ensure_db(s)
    sc = load_holdout(s)["HS05"]
    card = build_agent(s, live_db_path=s.live_db_path.with_name("smoke_live.sqlite")).run(
        sc["day0_report"], as_of=sc["day0"], context=holdout_context(sc))
    for step in card.trace:
        print(f"[{step['kind']:>4}] {step['name']:<22} {step['ms']:>6} ms  in={step['input']}\n       out={step['output']}")
    print(json.dumps({k: card.to_dict()[k] for k in ("recommended_action", "simulator_best_action", "guardrail_events",
                                                     "cited_doc_ids", "cited_record_ids", "warnings", "usage",
                                                     "latency_s")}, indent=2))
    print("\nRATIONALE:\n" + card.rationale)
```

Note: the synthesizer shares the agent's `LLM`, so its tokens are counted in the card's `usage`.

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_agent_core.py -v`
Expected: 8 passed

- [ ] **Step 7: Stage 2 gate - run HS05 end to end against the real bank and Claude**

Run: `.venv/bin/python -m agent.agent_core`
Expected:
- A printed trace: `simulate_options` → `gather` (hit counts per recall > 0) → `reflect` (mode `native`, since HS05 is after the corpus horizon) → one or more `decide` turns → `rationale`.
- JSON with `recommended_action` = `simulator_best_action` = `switch_supplier` and non-empty citations.
- A rationale that names the accept_delay precedent and explains why it does not apply in Nov-Dec.

If the model recommends `accept_delay`, the trap worked on it. Keep the result (the eval measures this) and read the trace to see whether the precedents block reached it.

- [ ] **Step 8: Commit**

```bash
git add agent/card.py agent/agent_core.py tests/fakes.py tests/test_agent_core.py
git commit -m "feat: agent core decision loop with guardrails, precedents and QA mode

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Write-back - `log_decision` with mock external actions

**Files:**
- Create: `agent/log_decision.py`
- Test: `tests/test_log_decision.py`

**Interfaces:**
- Consumes: `DecisionCard`, `ScenarioState`, `open_live_writer`, `live_token`, `HindsightMemory.retain_experience` (or `FakeMemory`).
- Produces:
  - `MOCK: str`
  - `commitment_for(option: dict, state) -> dict` with keys `counterparty_type, counterparty_id, made_by_role, commitment_text, quantity, due_date`
  - `mock_actions_for(option, state) -> list[str]`
  - `log_decision(settings, live_db_path, memory, card, state) -> tuple[dict, list[str]]`. The dict has keys `decision_id, commitment_id, commitment_text, retain_status, live_db`.

- [ ] **Step 1: Write the failing tests**

`tests/test_log_decision.py`:
```python
import sqlite3
from dataclasses import replace

import pytest

from agent import sql_tools
from agent.agent_core import DecisionAgent
from agent.db import live_token, open_live_writer
from agent.log_decision import MOCK, log_decision
from agent.scenarios import holdout_context, load_holdout
from tests.fakes import FakeLLM, FakeMemory, response, submission, text, tool_use


def _run(settings, sid, writeback=True):
    s = load_holdout(settings)[sid]
    llm = FakeLLM([response(tool_use("submit_recommendation", submission("switch_supplier", cited_record_ids=[s["event_id"]]))),
                   response(text("Switch."), stop="end_turn")])
    mem = FakeMemory()
    card = DecisionAgent(settings, llm, mem, settings.live_db_path).run(
        s["day0_report"], as_of=s["day0"], context=holdout_context(s), writeback=writeback)
    return card, mem


def test_writeback_rows_retain_and_mocks(settings, db_factory):
    card, mem = _run(settings, "HS05")
    wb = card.writeback
    assert (wb["decision_id"], wb["commitment_id"]) == ("DECL00001", "CMTL00001")
    token = live_token(settings.live_db_path)
    assert mem.retained[0]["document_id"] == f"DECL00001.{token}"
    assert mem.retained[0]["timestamp"] == "2025-10-14T12:00:00-05:00"
    assert "switch_supplier" in mem.retained[0]["content"]
    assert card.mock_actions and all(m.startswith(MOCK) for m in card.mock_actions)
    # the next disruption (HS08, same supplier, 2025-10-19) sees the live decision as a precedent
    decs = sql_tools.prior_decisions(db_factory("2025-10-19"), "SUP0247", "RM0083", "2025-10-19")
    assert decs[0]["decision_id"] == "DECL00001" and decs[0]["source"] == "live"


def test_second_decision_gets_next_id_and_rows_are_append_only(settings):
    _run(settings, "HS05")
    card, _ = _run(settings, "HS08")
    assert card.writeback["decision_id"] == "DECL00002"
    con = open_live_writer(settings.live_db_path)
    with pytest.raises(sqlite3.DatabaseError):
        with con:
            con.execute("DELETE FROM commitments_live")
    con.close()


def test_retain_can_be_disabled(settings):
    card, mem = _run(replace(settings, writeback_retain=False), "HS05")
    assert mem.retained == [] and card.writeback["retain_status"].startswith("skipped")


def test_log_decision_requires_recommendation(settings):
    card, _ = _run(settings, "HS05", writeback=False)
    card.recommended_action = None
    with pytest.raises(ValueError):
        log_decision(settings, settings.live_db_path, FakeMemory(), card, None)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_log_decision.py -v`
Expected: FAIL. `agent_core` does `from agent.log_decision import log_decision`, which raises `ModuleNotFoundError`.

- [ ] **Step 3: Implement `agent/log_decision.py`**

```python
"""Close the loop.

Persist the chosen action as append-only live rows (decision + commitment) and retain an experience summary
in Hindsight with the decision's business timestamp. External systems (ERP, supplier portal, email, MES, QMS)
are never called; their would-be actions are returned as clearly labelled mock no-ops.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent.card import DecisionCard
from agent.config import Settings
from agent.db import live_token, open_live_writer
from agent.simulate import ScenarioState

MOCK = "[MOCK - no external call made]"


def commitment_for(option: dict, st: ScenarioState) -> dict:
    a, qty, arr = option["action"], round(st.req, 1), option.get("arrival")
    if a == "switch_supplier":
        return dict(counterparty_type="supplier", counterparty_id=option["alt_supplier_id"], made_by_role="Buyer",
                    commitment_text=f"We place a spot order for {qty:,} {st.uom} of {st.rm_id} with "
                                    f"{option['alt_supplier_id']} for delivery to {st.plant_id} by {arr}",
                    quantity=qty, due_date=arr)
    if a == "expedite":
        return dict(counterparty_type="supplier", counterparty_id=st.supplier_id, made_by_role="Buyer",
                    commitment_text=f"{st.supplier_id} air-expedites {', '.join(st.lot_rpo_ids)} to {st.plant_id}, "
                                    f"arrival by {arr}", quantity=None, due_date=arr)
    if a == "reallocate_stock":
        return dict(counterparty_type="plant", counterparty_id=option["donor_plant"], made_by_role="Materials Manager",
                    commitment_text=f"{option['donor_plant']} transfers {qty:,} {st.uom} of {st.rm_id} to "
                                    f"{st.plant_id} by {arr}", quantity=qty, due_date=arr)
    if a == "substitute_rm":
        return dict(counterparty_type="internal", counterparty_id=st.plant_id, made_by_role="Quality Lead",
                    commitment_text=f"QA approves {option['substitute']} as substitute for {st.rm_id} at "
                                    f"{st.plant_id} by {arr}", quantity=None, due_date=arr)
    if a == "cancel_po":
        return dict(counterparty_type="supplier", counterparty_id=st.supplier_id, made_by_role="Buyer",
                    commitment_text=f"We cancel {st.rpo_id} with {st.supplier_id} and re-buy elsewhere by {arr}",
                    quantity=None, due_date=arr)
    if a == "accept_delay":
        return dict(counterparty_type="plant", counterparty_id=st.plant_id, made_by_role="Plant Scheduler",
                    commitment_text=f"{st.plant_id} reschedules {st.primary_run_id} to start once {st.rpo_id} "
                                    f"arrives ({st.eta.isoformat()})", quantity=None, due_date=st.eta.isoformat())
    raise ValueError(f"no commitment template for {a}")


def mock_actions_for(option: dict, st: ScenarioState) -> list[str]:
    a, qty = option["action"], f"{round(st.req, 1):,} {st.uom}"
    return {
        "switch_supplier": [f"{MOCK} ERP: would create a spot PO with {option.get('alt_supplier_id')} for {qty} of {st.rm_id}",
                            f"{MOCK} Supplier portal: would request delivery confirmation from {option.get('alt_supplier_id')}"],
        "expedite": [f"{MOCK} Forwarder portal: would book air freight for {', '.join(st.lot_rpo_ids)}"],
        "reallocate_stock": [f"{MOCK} ERP: would create transfer order {option.get('donor_plant')} -> {st.plant_id} for {qty}"],
        "substitute_rm": [f"{MOCK} QMS: would open QA approval of {option.get('substitute')} for {st.rm_id}"],
        "cancel_po": [f"{MOCK} ERP: would cancel {st.rpo_id}",
                      f"{MOCK} Email: would notify {st.supplier_id} of the cancellation"],
        "accept_delay": [f"{MOCK} MES: would move {st.primary_run_id} start to {st.eta.isoformat()}"],
    }[a]


def _next_id(con, table: str, prefix: str) -> str:
    return f"{prefix}{con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] + 1:05d}"


def log_decision(settings: Settings, live_db_path: Path, memory, card: DecisionCard,
                 state: ScenarioState | None) -> tuple[dict, list[str]]:
    if card.recommended_action is None or state is None:
        raise ValueError("nothing to log: the card has no recommendation or no scenario state")
    option = next(o for o in card.options if o["action"] == card.recommended_action)
    c = commitment_for(option, state)
    now = datetime.now(timezone.utc).isoformat()
    con = open_live_writer(live_db_path)
    try:
        with con:
            dec_id = _next_id(con, "decisions_live", "DECL")
            cmt_id = _next_id(con, "commitments_live", "CMTL")
            con.execute("""INSERT INTO decisions_live (decision_id, event_ref, supplier_id, rm_id, plant_id, rpo_id,
                           decided_at, decided_by_role, decision_type, options_considered_json, chosen_option,
                           rationale_text, expected_cost, expected_stockout_days, evidence_doc_ids,
                           evidence_record_ids, run_id, logged_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (dec_id, f"LIVE:{state.rpo_id}", state.supplier_id, state.rm_id, state.plant_id, state.rpo_id,
                         card.as_of, "Decision Agent (prototype)", card.recommended_action,
                         json.dumps([o for o in card.options if o.get("feasible")], sort_keys=True),
                         card.recommended_action, card.rationale, option["cost"], option["stockout_days"],
                         json.dumps(card.cited_doc_ids), json.dumps(card.cited_record_ids), card.run_id, now))
            con.execute("""INSERT INTO commitments_live (commitment_id, decision_id, counterparty_type, counterparty_id,
                           made_by_role, made_at, commitment_text, quantity, due_date, penalty_or_credit, logged_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (cmt_id, dec_id, c["counterparty_type"], c["counterparty_id"], c["made_by_role"], card.as_of,
                         c["commitment_text"], c["quantity"], c["due_date"], None, now))
    finally:
        con.close()
    retain_status = "skipped (WRITEBACK_RETAIN=0)"
    if settings.writeback_retain:
        alts = "; ".join(f"{o['action']} score {o['score']:,.0f}" for o in card.options
                         if o.get("feasible") and o["action"] != card.recommended_action)
        summary = (f"Agent decision {dec_id} on {card.as_of}: {state.supplier_id} moved {state.rpo_id} ({state.rm_id}) "
                   f"for {state.plant_id} to {state.eta.isoformat()}; run {state.primary_run_id} was planned "
                   f"{state.planned_start.isoformat()}. Chose {card.recommended_action}: simulated cost "
                   f"${option['cost']:,.0f}, {option['stockout_days']} stockout days, risk {option['risk']}. "
                   f"Alternatives: {alts}. Commitment {cmt_id}: {c['commitment_text']} (due {c['due_date']}). "
                   f"Evidence: docs {', '.join(card.cited_doc_ids) or 'none'}; records "
                   f"{', '.join(card.cited_record_ids) or 'none'}. Outcome not yet known.")
        retain_status = memory.retain_experience(
            document_id=f"{dec_id}.{live_token(live_db_path)}", content=summary, context="agent decision log",
            timestamp=f"{card.as_of}T12:00:00-05:00",
            metadata={"decision_id": dec_id, "commitment_id": cmt_id, "run_id": card.run_id, "logged_at": now})
    return ({"decision_id": dec_id, "commitment_id": cmt_id, "commitment_text": c["commitment_text"],
             "retain_status": retain_status, "live_db": str(live_db_path)}, mock_actions_for(option, state))
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_log_decision.py tests/test_agent_core.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add agent/log_decision.py tests/test_log_decision.py
git commit -m "feat: append-only write-back with Hindsight retain and mocked external actions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Decision console (Stage 3)

**Files:**
- Create: `interface/decision_console.py`
- Test: `tests/test_console.py`

**Interfaces:**
- Consumes: `build_agent`, `DecisionCard`, `QAResult`, `load_holdout`, `holdout_context`, `load_settings`, `ensure_db`.
- Produces: `render_card(card, console, show_trace=False) -> None`; `render_answer(res, console) -> None`; `main(argv: list[str] | None = None) -> int`. CLI: `python -m interface.decision_console (--report TEXT | --report-file PATH | --scenario HSxx | --question TEXT) [--as-of] [--rpo] [--runs] [--rm] [--writeback] [--live-db] [--trace] [--json-out]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_console.py`:
```python
import pytest
from rich.console import Console

from agent.card import DecisionCard, QAResult
from agent.scenarios import holdout_context, load_holdout
from agent.simulate import best_option, build_state, simulate_options
from interface.decision_console import main, render_answer, render_card


def _card(db_factory, base_settings):
    s = load_holdout(base_settings)["HS05"]
    con = db_factory(s["day0"])
    opts = simulate_options(con, build_state(con, day0=s["day0"], **holdout_context(s)))
    return DecisionCard(
        run_id="run-test", as_of=s["day0"], report=s["day0_report"], entities={}, situation_summary="SUP0247 slipped.",
        options=opts, simulator_best_action=best_option(opts)["action"], recommended_action="switch_supplier",
        confidence="high", rationale="Switch [DOC000001].",
        precedents=[{"decision_id": "DEC00353", "event_id": "EVT02501", "decided_at": "2025-08-28",
                     "decision_type": "accept_delay", "outcome_label": "success", "today_rank": 5,
                     "applies_today": False, "note": "today accept_delay ranks 5 of 6"}],
        open_commitments=[{"commitment_id": "CMT00835", "counterparty_id": "SUP0247", "due_date": "2025-12-07",
                           "commitment_text": "Supplier 247 confirmed RPO023030", "affected_by": []}],
        cited_doc_ids=["DOC000001"], cited_record_ids=["DEC00353"], unverifiable_citations=["DOC999999"],
        guardrail_events=["Rejected cancel_po: would breach CMT00560"], warnings=["example warning"],
        mock_actions=["[MOCK - no external call made] ERP: would create a spot PO"],
        usage={"calls": 2, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}, latency_s=1.5)


def test_render_card_sections(db_factory, base_settings):
    console = Console(record=True, width=180)
    render_card(_card(db_factory, base_settings), console)
    out = console.export_text()
    for needle in ("Candidate options", "switch_supplier", "★", "Precedents", "DEC00353", "Open commitments",
                   "CMT00835", "Guardrails", "CMT00560", "Unverifiable", "DOC999999", "MOCK", "example warning",
                   "not modelled"):
        assert needle in out, needle


def test_render_answer():
    console = Console(record=True, width=120)
    render_answer(QAResult(question="q?", as_of="2023-06-11", answer="2023-06-08", cited_doc_ids=["DOC000254"]), console)
    out = console.export_text()
    assert "2023-06-08" in out and "DOC000254" in out


def test_question_requires_as_of():
    with pytest.raises(SystemExit):
        main(["--question", "What is the ETA?"])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_console.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'interface.decision_console'`

- [ ] **Step 3: Implement `interface/decision_console.py`**

```python
"""CLI decision console: a free-text disruption report in, a decision card out."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent.card import DecisionCard, QAResult


def _money(v):
    return "-" if v is None else f"${v:,.0f}"


def render_card(card: DecisionCard, console: Console, show_trace: bool = False) -> None:
    console.print(Panel(f"{card.report}\n\n[dim]as_of {card.as_of} | run {card.run_id}[/]", title="Disruption report"))
    if card.situation_summary:
        console.print(Panel(card.situation_summary, title="Situation"))

    t = Table(title="Candidate options (deterministic simulator)  ★ recommended  ▲ simulator best")
    for col in ("", "Action", "Arrival", "Stockout days", "Cost", "Risk", "Score", "Service impact (units)",
                "Breaches", "Note"):
        t.add_column(col)
    for o in card.options:
        mark = ("★" if o["action"] == card.recommended_action else "") + ("▲" if o["action"] == card.simulator_best_action else "")
        if o.get("feasible"):
            t.add_row(mark, o["action"], o["arrival"] or "-", str(o["stockout_days"]), _money(o["cost"]), o["risk"],
                      _money(o["score"]), str(o["service_impact_units"]), ", ".join(o["breaches_commitments"]) or "-",
                      o.get("note", ""))
        else:
            t.add_row(mark, f"[dim]{o['action']}[/]", "-", "-", "-", "-", "-", "-", "-", f"[dim]{o.get('note', '')}[/]")
    console.print(t)

    if card.precedents:
        p = Table(title="Precedents (structured decisions, scored against today's options)")
        for col in ("Decision", "Event", "Decided", "Action", "Outcome", "Today rank", "Applies today?", "Note"):
            p.add_column(col)
        for x in card.precedents:
            applies = "[green]yes[/]" if x["applies_today"] else "[red]no - misleading if copied[/]"
            p.add_row(x["decision_id"], x.get("event_id") or "-", x["decided_at"], x["decision_type"],
                      x.get("outcome_label") or "unknown yet", str(x.get("today_rank") or "-"), applies, x["note"])
        console.print(p)

    if card.open_commitments:
        c = Table(title="Open commitments")
        for col in ("Commitment", "Counterparty", "Due", "Text", "Affected by"):
            c.add_column(col)
        for x in card.open_commitments:
            hit = ", ".join(x.get("affected_by") or [])
            c.add_row(x["commitment_id"], x["counterparty_id"], x["due_date"], x["commitment_text"],
                      f"[red]{hit}[/]" if hit else "-")
        console.print(c)

    if card.guardrail_events:
        console.print(Panel("\n".join(card.guardrail_events), title="Guardrails", border_style="red"))

    rec = card.recommended_action or "[yellow]no recommendation[/]"
    extra = ("\n[yellow]Deviates from the simulator's lowest risk-adjusted option "
             f"({card.simulator_best_action}); see rationale.[/]") if card.deviates_from_simulator_best and card.recommended_action else ""
    console.print(Panel(f"[bold]{rec}[/]  (confidence: {card.confidence}){extra}\n\n{card.rationale}",
                        title="Recommendation", border_style="green"))

    cites = f"Docs: {', '.join(card.cited_doc_ids) or '-'}\nRecords: {', '.join(card.cited_record_ids) or '-'}"
    if card.unverifiable_citations:
        cites += f"\n[yellow]Unverifiable (dropped): {', '.join(card.unverifiable_citations)}[/]"
    if card.ungrounded_claims:
        cites += "\n[yellow]Ungrounded claims: " + "; ".join(card.ungrounded_claims) + "[/]"
    console.print(Panel(cites, title="Evidence"))

    if card.warnings:
        console.print(Panel("\n".join(card.warnings), title="Warnings", border_style="yellow"))
    if card.writeback or card.mock_actions:
        body = json.dumps(card.writeback, indent=1) if card.writeback else ""
        console.print(Panel(body + "\n" + "\n".join(card.mock_actions), title="Write-back / initiated response"))
    u = card.usage or {}
    console.print(f"[dim]memory hits {card.memory_hits} (dropped as future: {card.dropped_future_hits}) | reflect "
                  f"{card.reflect_mode} | LLM calls {u.get('calls')} | tokens in/out {u.get('input_tokens')}/"
                  f"{u.get('output_tokens')} | cost ${u.get('cost_usd')} | {card.latency_s}s[/]")
    if show_trace:
        tr = Table(title="Trace")
        for col in ("Kind", "Name", "ms", "Input", "Output"):
            tr.add_column(col)
        for s in card.trace:
            tr.add_row(s["kind"], s["name"], str(s["ms"]), s["input"], s["output"])
        console.print(tr)


def render_answer(res: QAResult, console: Console) -> None:
    body = (f"{res.answer}\n\nDocs: {', '.join(res.cited_doc_ids) or '-'}\nRecords: "
            f"{', '.join(res.cited_record_ids) or '-'}\n[dim]confidence {res.confidence} | as_of {res.as_of}[/]")
    if res.unverifiable_citations:
        body += f"\n[yellow]Unverifiable (dropped): {', '.join(res.unverifiable_citations)}[/]"
    console.print(Panel(body, title=res.question))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Supply Chain Memory & Decision Agent console")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--report", help="free-text disruption report")
    src.add_argument("--report-file", help="file containing the report")
    src.add_argument("--scenario", help="holdout scenario id, e.g. HS05")
    src.add_argument("--question", help="ask a history question (QA mode; needs --as-of)")
    ap.add_argument("--as-of", help="YYYY-MM-DD; defaults to the report date, then DEFAULT_AS_OF")
    ap.add_argument("--rpo", help="RM purchase order id if not in the report")
    ap.add_argument("--runs", help="comma-separated affected production run ids (first = the run that needs the RM)")
    ap.add_argument("--rm", help="raw material id if the PO has several")
    ap.add_argument("--writeback", action="store_true", help="log decision + commitment and retain a summary")
    ap.add_argument("--live-db", help="live DB path (default LIVE_DB_PATH)")
    ap.add_argument("--trace", action="store_true", help="print the full tool / LLM trace")
    ap.add_argument("--json-out", help="also write the card as JSON")
    a = ap.parse_args(argv)
    if a.question and not a.as_of:
        ap.error("--question needs --as-of")

    from agent.agent_core import AgentError, build_agent
    from agent.config import load_settings
    from agent.scenarios import holdout_context, load_holdout
    from agent.setup_data import ensure_db

    settings = load_settings()
    ensure_db(settings)
    agent = build_agent(settings, live_db_path=Path(a.live_db) if a.live_db else None)
    console = Console()
    try:
        if a.question:
            res = agent.answer_question(a.question, a.as_of)
            render_answer(res, console)
            out = res.to_dict()
        else:
            if a.scenario:
                s = load_holdout(settings)[a.scenario]
                report, as_of, ctx = s["day0_report"], a.as_of or s["day0"], holdout_context(s)
            else:
                report = a.report or Path(a.report_file).read_text()
                as_of = a.as_of
                ctx = {k: v for k, v in {"rpo_id": a.rpo, "rm_id": a.rm,
                                         "run_ids": a.runs.split(",") if a.runs else None}.items() if v}
            card = agent.run(report, as_of=as_of, context=ctx, writeback=a.writeback)
            render_card(card, console, show_trace=a.trace)
            out = card.to_dict()
    except AgentError as ex:
        console.print(f"[red]Agent error:[/] {ex}")
        return 1
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_console.py -v`
Expected: 3 passed

- [ ] **Step 5: Stage 3 gate - run the console on a trap scenario**

Run: `.venv/bin/python -m interface.decision_console --scenario HS05 --trace`
Expected: a full card. The options table shows ★ and ▲ on `switch_supplier`. The precedents table lists accept_delay successes as "no - misleading if copied". Open commitments CMT00785…CMT00835 appear. The footer shows token cost and latency.

- [ ] **Step 6: Commit**

```bash
git add interface/decision_console.py tests/test_console.py
git commit -m "feat: rich CLI decision console

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10A: Seven-capability router, handlers and `--ask` (merged build prompt)

**Files:**
- Create: `agent/capabilities.py`
- Modify: `interface/decision_console.py` (add `--ask` + `render_capability`)
- Test: `tests/test_capabilities.py`

**Interfaces:**
- Consumes: `DecisionAgent` (`.memory`, `.settings`, `._connect(as_of)`), `sql_tools` curated lookups, `record_exists`, `parse_report`, `HindsightMemory.reflect(question, as_of, budget=, response_schema=)`, `get_document`.
- Produces:
  - `CAPABILITIES`, `route(question) -> str`, `BUDGET: dict[str, str]`, `PRECEDENT_SCHEMA`, `OUTCOME_SCHEMA`
  - `CapabilityAnswer` (dataclass: `question, capability, as_of, budget, reflect_mode, answer, structured, ground_truth, cited_doc_ids, cited_record_ids, unverifiable_citations, commitments_checked, warnings, confidence, latency_s`; `to_dict()`)
  - `ask(agent, question: str, as_of: str | None = None) -> CapabilityAnswer`
  - `interface.decision_console.render_capability(ans, console) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_capabilities.py`:
```python
import pytest
from rich.console import Console

from agent.agent_core import DecisionAgent
from agent.capabilities import OUTCOME_SCHEMA, PRECEDENT_SCHEMA, ask, route
from tests.fakes import FakeLLM, FakeMemory, corpus_hit

DEMO = [
    ("What is SUP0247's delivery track record in November and December? Should we trust their current promise "
     "for a November delivery?", "supplier_reliability"),
    ("What open commitments do we have with SUP0091? If we switch suppliers, what obligations would we breach?",
     "commitments"),
    ("Our primary RM supplier just had a 35% supply reduction. We're deciding between prioritizing high-margin "
     "products vs. switching to the backup supplier. What happened last time we faced this?", "decision_precedent"),
    ("Have we ever accepted partial shipments from suppliers? How many times has this exception been granted this "
     "quarter, and should we escalate for a policy review?", "exceptions"),
    ("We're seeing corrosion on steel components from a supplier that had humidity issues before. Are their "
     "corrective controls still in place, or has the problem recurred?", "quality_root_cause"),
    ("We need to renegotiate with SUP0179 whose lead times keep getting longer. What negotiation strategies have "
     "worked with them before?", "negotiation"),
    ("How accurate have our past 'switch to cheaper supplier' decisions been? Should we trust our cost-savings "
     "projections?", "decision_outcome_learning"),
]


@pytest.mark.parametrize("question,expected", DEMO + [("Tell me something useful.", "general")])
def test_route(question, expected):
    assert route(question) == expected


def _agent(settings, text="fake reflection", hits=()):
    mem = FakeMemory(hits, reflect_text=text)
    return DecisionAgent(settings, FakeLLM([]), mem, settings.live_db_path), mem


def test_supplier_reliability_grounded(settings):
    agent, mem = _agent(settings, "SUP0247 slips in Nov-Dec [DOC000001, 2023-01-02]; see RPO000412 and EVT99999.",
                        [corpus_hit("DOC000001", "2023-01-02")])
    ans = ask(agent, DEMO[0][0], as_of="2025-12-31")
    assert ans.capability == "supplier_reliability" and ans.budget == "high"
    months = {r["month"] for r in ans.ground_truth["delay_by_promised_month"]}
    assert {"11", "12"} <= months
    assert ans.cited_doc_ids == ["DOC000001"] and "RPO000412" in ans.cited_record_ids
    assert "EVT99999" in ans.unverifiable_citations
    question_sent = mem.reflects[-1][0]
    assert "delay_by_promised_month" in question_sent and mem.reflects[-1][2] == "high"


def test_commitments_checked_before_switch(settings):
    agent, _ = _agent(settings, "See CMT00560.")
    ans = ask(agent, DEMO[1][0], as_of="2025-06-30")
    assert ans.capability == "commitments" and ans.commitments_checked is True
    assert "open_commitments" in ans.ground_truth and "contracts_in_force" in ans.ground_truth


def test_switch_without_named_supplier_warns_and_uses_schema(settings):
    # one verifiable citation, so the structured confidence ("medium" from the fake) is used
    agent, mem = _agent(settings, "No record found of a 35% supply reduction; closest precedent DEC00006.")
    ans = ask(agent, DEMO[2][0], as_of="2025-12-31")
    assert ans.commitments_checked is False
    assert any("no supplier" in w for w in ans.warnings)
    assert mem.reflects[-1][3] == PRECEDENT_SCHEMA and ans.confidence == "medium"


def test_outcome_learning_accuracy_table(settings):
    agent, mem = _agent(settings, "DEC00006 ...")
    ans = ask(agent, DEMO[6][0], as_of="2025-12-31")
    rows = ans.ground_truth["decision_accuracy"]
    assert [r["decision_type"] for r in rows] == ["switch_supplier"] and rows[0]["n"] > 0
    assert mem.reflects[-1][3] == OUTCOME_SCHEMA


def test_no_citations_means_low_confidence(settings):
    agent, _ = _agent(settings, "No record found.")
    ans = ask(agent, DEMO[4][0], as_of="2025-12-31")
    assert ans.confidence == "low" and any("no evidence citations" in w for w in ans.warnings)


def test_render_capability(settings):
    from interface.decision_console import render_capability

    agent, _ = _agent(settings, "SUP0179 conceded price [RPO000412].")
    console = Console(record=True, width=160)
    render_capability(ask(agent, DEMO[5][0], as_of="2025-12-31"), console)
    out = console.export_text()
    assert "negotiation" in out and "RPO000412" in out and "negotiations" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_capabilities.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'agent.capabilities'`

- [ ] **Step 3: Implement `agent/capabilities.py`**

```python
"""The seven capabilities from the build prompt, routed from a natural-language question.

Each handler makes one Hindsight reflect call (budget / response_schema as the prompt specifies). The query carries
deterministic ground truth from the as-of SQL views (delays by month, scorecards, commitments, contracts,
negotiations, decision accuracy). Afterwards the answer is checked:
- record ids in it must exist;
- doc ids must be visible memory docs;
- any question about switching suppliers or cancelling a PO gets the open commitments attached.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field

from agent import sql_tools
from agent.parsing import parse_report

CAPABILITIES = ("supplier_reliability", "commitments", "decision_precedent", "exceptions", "quality_root_cause",
                "negotiation", "decision_outcome_learning", "general")
ROUTES = [  # first match wins
    ("commitments", r"\bcommitments?\b|\bobligations?\b|\bowe[sd]?\b"),
    ("negotiation", r"negotiat|\blevers?\b|\bconce(?:ssion|ded)"),
    ("decision_outcome_learning", r"\baccura(?:te|cy)\b|\bpredicted\b|\bprojections?\b|\bunderestimat|\boverestimat"),
    ("quality_root_cause", r"\bdefects?\b|\bcorrosion\b|\bquality\b|\broot cause\b|\breject|\bcorrective\b"),
    ("exceptions", r"\bexceptions?\b|\bpartial shipments?\b|\bpolicy review\b"),
    ("decision_precedent", r"\blast time\b|\bprecedents?\b|\bsimilar situation|\bdeciding between\b"),
    ("supplier_reliability", r"\btrack record\b|\breliab|\bon[- ]time\b|\blate\b|\bdelay|\btrust their\b"),
]
BUDGET = {"supplier_reliability": "high", "commitments": "mid", "decision_precedent": "high", "exceptions": "mid",
          "quality_root_cause": "high", "negotiation": "high", "decision_outcome_learning": "high", "general": "mid"}
INSTRUCTIONS = {
    "supplier_reliability": "Give the complete delivery and failure history: what went wrong, when, what action we "
                            "took, whether it worked, and what to do now. Flag any recurring pattern (seasonal, "
                            "size-dependent, lead-time trend).",
    "commitments": "List every open commitment with this counterparty: who promised what, by when, the penalty or "
                   "credit for breach, and current status. Say which ones a supplier switch or PO cancellation would "
                   "breach.",
    "decision_precedent": "Find the most similar past situations. For each give situation, decision, reasoning, "
                          "outcome, lesson and how today's conditions differ. Then recommend, with the predicted "
                          "outcome and a confidence level.",
    "exceptions": "Find past exceptions to this policy: how many, when, the justification, who approved them and the "
                  "outcome. Say whether the frequency warrants a policy review.",
    "quality_root_cause": "Has this issue occurred before? Give the root cause, the corrective action promised, "
                          "whether it was verified, and whether the problem recurred.",
    "negotiation": "Summarize every negotiation with this supplier: our ask, their offer, concessions each way and "
                   "the outcome. Then say which levers worked, what the supplier values and what to lead with next.",
    "decision_outcome_learning": "Compare predicted and actual outcomes for these past decisions: prediction "
                                 "accuracy, systematic biases (cost, stockout days) and an updated confidence level.",
    "general": "Answer from memory.",
}
COMMON = ("Cite document dates and record ids for every claim. Distinguish facts (documents) from observations "
          "(consolidated patterns). If memory holds no evidence for something, say 'No record found' instead of "
          "guessing.")
_STR = {"type": "string"}
PRECEDENT_SCHEMA = {"type": "object", "properties": {
    "precedents": {"type": "array", "items": {"type": "object", "properties": {
        "decision_id": _STR, "situation": _STR, "decision": _STR, "outcome": _STR, "how_today_differs": _STR},
        "required": ["decision_id", "situation", "decision", "outcome", "how_today_differs"]}},
    "recommendation": _STR, "predicted_outcome": _STR,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
    "required": ["precedents", "recommendation", "predicted_outcome", "confidence"]}
OUTCOME_SCHEMA = {"type": "object", "properties": {
    "decision_type": _STR, "decisions_reviewed": {"type": "array", "items": _STR}, "prediction_accuracy": _STR,
    "systematic_biases": {"type": "array", "items": _STR}, "updated_confidence": _STR,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
    "required": ["decision_type", "decisions_reviewed", "prediction_accuracy", "systematic_biases",
                 "updated_confidence", "confidence"]}
SCHEMAS = {"decision_precedent": PRECEDENT_SCHEMA, "decision_outcome_learning": OUTCOME_SCHEMA}
_SWITCH_OR_CANCEL = re.compile(r"\bswitch\w*|\bcancel\w*|\bbackup supplier|\balternat\w* supplier", re.I)
_ACTION_MENTIONS = {
    "switch_supplier": r"switch\w*(?: to)?(?: a| the)?(?: cheaper| backup| alternat\w*)? supplier",
    "expedite": r"\bexpedit", "cancel_po": r"\bcancel", "reallocate_stock": r"\btransfer|\breallocat",
    "substitute_rm": r"\bsubstitut", "accept_delay": r"\baccept\w* (?:the )?delay",
    "build_safety_stock": r"\bsafety stock", "renegotiate": r"\brenegotiat", "reduce_allocation": r"\breduc\w* allocation",
}
_RECORD_ID = re.compile(r"\b(?:DECL|CMTL|RPOL|RPO|REV|EVT|DEC|CMT|NEG|CTR|CAT|POL|PR|PO|RT|SUP|RM|IP)\d+\b|\b[PW]\d{2,3}\b")
_DOC_ID = re.compile(r"\bDOC\d{6}\b")
MAX_GROUND_TRUTH_CHARS = 6000


@dataclass
class CapabilityAnswer:
    question: str
    capability: str
    as_of: str
    budget: str
    reflect_mode: str | None = None
    answer: str = ""
    structured: dict | None = None
    ground_truth: dict = field(default_factory=dict)
    cited_doc_ids: list[str] = field(default_factory=list)
    cited_record_ids: list[str] = field(default_factory=list)
    unverifiable_citations: list[str] = field(default_factory=list)
    commitments_checked: bool | None = None
    warnings: list[str] = field(default_factory=list)
    confidence: str = "low"
    latency_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def route(question: str) -> str:
    for cap, pattern in ROUTES:
        if re.search(pattern, question, re.I):
            return cap
    return "general"


def _q(con, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _delay_by_promised_month(con, supplier_id: str) -> list[dict]:
    return _q(con, """
        SELECT strftime('%m', promised_at) AS month, COUNT(*) AS n_received,
               ROUND(AVG(MAX(0, julianday(received_at) - julianday(promised_at))), 1) AS avg_delay_days,
               ROUND(AVG(julianday(received_at) > julianday(promised_at)), 2) AS late_share
        FROM rm_purchase_orders WHERE supplier_id = ? AND received_at IS NOT NULL
        GROUP BY month ORDER BY month""", (supplier_id,))


def _lead_time_by_month(con, supplier_id: str) -> list[dict]:
    return _q(con, """
        SELECT substr(ordered_at, 1, 7) AS ordered_month, COUNT(*) AS n,
               ROUND(AVG(julianday(received_at) - julianday(ordered_at)), 1) AS avg_lead_days
        FROM rm_purchase_orders WHERE supplier_id = ? AND received_at IS NOT NULL
        GROUP BY ordered_month ORDER BY ordered_month DESC LIMIT 12""", (supplier_id,))


def _decision_accuracy(con, types: list[str]) -> tuple[list[dict], list[dict]]:
    where = "outcome_label IS NOT NULL AND source = 'historical'"
    params: tuple = ()
    if types:
        where += f" AND decision_type IN ({','.join('?' * len(types))})"
        params = tuple(types)
    summary = _q(con, f"""
        SELECT decision_type, COUNT(*) AS n,
               ROUND(SUM(actual_cost) / NULLIF(SUM(expected_cost), 0), 2) AS actual_to_expected_cost,
               ROUND(AVG(actual_stockout_days - expected_stockout_days), 2) AS extra_stockout_days,
               SUM(outcome_label = 'success') AS success, SUM(outcome_label = 'partial') AS partial,
               SUM(outcome_label = 'failed') AS failed
        FROM decisions WHERE {where} GROUP BY decision_type ORDER BY decision_type""", params)
    examples = _q(con, f"""
        SELECT decision_id, decision_type, decided_at, expected_cost, actual_cost, expected_stockout_days,
               actual_stockout_days, outcome_label, outcome_attribution
        FROM decisions WHERE {where} ORDER BY decided_at DESC LIMIT 10""", params)
    return summary, examples


def _ground_truth(con, cap: str, question: str, as_of: str) -> tuple[dict, bool | None, list[str]]:
    ents = parse_report(question)
    sup = ents.supplier_ids[0] if ents.supplier_ids else None
    rm = ents.rm_ids[0] if ents.rm_ids else None
    parties = ents.supplier_ids + ents.plant_ids
    gt: dict = {}
    warnings: list[str] = []
    if cap == "supplier_reliability" and sup:
        gt["delay_by_promised_month"] = _delay_by_promised_month(con, sup)
        gt["scorecard_recent"] = sql_tools.supplier_scorecard(con, sup, 6)
        gt["prior_decisions"] = sql_tools.prior_decisions(con, sup, rm, as_of)
    elif cap == "commitments" and parties:
        gt["open_commitments"] = sql_tools.open_commitments(con, parties, as_of)
        gt["contracts_in_force"] = _q(con, """SELECT contract_id, scope, valid_from, valid_to, price_terms, min_volume,
                                              penalty_clause, force_majeure_flag FROM contracts
                                              WHERE supplier_id = ? AND valid_from <= ? AND valid_to >= ?""",
                                      (sup, as_of, as_of)) if sup else []
    elif cap == "decision_precedent":
        gt["prior_decisions"] = (sql_tools.prior_decisions(con, sup, rm, as_of) if (sup or rm)
                                 else _decision_accuracy(con, [])[1])
    elif cap == "negotiation" and sup:
        gt["negotiations"] = _q(con, "SELECT * FROM negotiations WHERE supplier_id = ? ORDER BY started_at", (sup,))
        gt["lead_time_by_month"] = _lead_time_by_month(con, sup)
    elif cap == "decision_outcome_learning":
        types = [a for a, p in _ACTION_MENTIONS.items() if re.search(p, question, re.I)]
        gt["decision_accuracy"], gt["decision_examples"] = _decision_accuracy(con, types)
    checked = None
    if _SWITCH_OR_CANCEL.search(question):
        if parties:
            gt.setdefault("open_commitments", sql_tools.open_commitments(con, parties, as_of))
            checked = True
        else:
            checked = False
            warnings.append("The question involves a supplier switch or cancellation but names no supplier id; open "
                            "commitments could not be checked - name the supplier (SUPxxxx) to check them.")
    elif cap == "commitments":
        checked = bool(parties)
    return gt, checked, warnings


def _verify(con, memory, text: str, seen_docs: set[str], as_of: str) -> tuple[list[str], list[str], list[str]]:
    docs, recs, bad = [], [], []
    for d in dict.fromkeys(_DOC_ID.findall(text)):
        (docs if d in seen_docs or memory.get_document(d, as_of) else bad).append(d)
    for r in dict.fromkeys(_RECORD_ID.findall(text)):
        (recs if sql_tools.record_exists(con, r) else bad).append(r)
    return docs, recs, bad


def ask(agent, question: str, as_of: str | None = None) -> CapabilityAnswer:
    t0 = time.monotonic()
    as_of = as_of or agent.settings.default_as_of
    cap = route(question)
    ans = CapabilityAnswer(question=question, capability=cap, as_of=as_of, budget=BUDGET[cap])
    con = agent._connect(as_of)
    try:
        gt, ans.commitments_checked, ans.warnings = _ground_truth(con, cap, question, as_of)
        ans.ground_truth = gt
        facts = json.dumps(gt, default=str)[:MAX_GROUND_TRUTH_CHARS] if gt else "(none for this question)"
        query = (f"{question}\n\nTask: {INSTRUCTIONS[cap]} {COMMON}\nAs of: {as_of}.\n"
                 f"Authoritative database records (quote their ids):\n{facts}")
        refl = agent.memory.reflect(query, as_of, budget=ans.budget, response_schema=SCHEMAS.get(cap))
        ans.reflect_mode, ans.structured = refl.mode, refl.structured
        ans.answer = refl.text or ""
        text = ans.answer + (" " + json.dumps(refl.structured) if refl.structured else "")
        ans.cited_doc_ids, ans.cited_record_ids, ans.unverifiable_citations = _verify(
            con, agent.memory, text, set(refl.doc_ids), as_of)
    finally:
        con.close()
    n_cites = len(ans.cited_doc_ids) + len(ans.cited_record_ids)
    stated = (ans.structured or {}).get("confidence")
    if n_cites == 0:
        ans.confidence = "low"
        ans.warnings.append("The answer contains no evidence citations (document ids or record ids).")
    elif stated in ("low", "medium", "high"):
        ans.confidence = stated
    else:
        ans.confidence = "high" if n_cites >= 3 else "medium"
    ans.latency_s = round(time.monotonic() - t0, 2)
    return ans
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_capabilities.py -v`
Expected: 13 passed (8 routing + 5 handler tests), 1 failed. `test_render_capability` fails with `ImportError: cannot import name 'render_capability'` until Step 5.

- [ ] **Step 5: Add `--ask` and `render_capability` to `interface/decision_console.py`**

Add after `render_answer`:
```python
def render_capability(ans, console: Console) -> None:
    head = (f"[bold]{ans.capability}[/] | budget {ans.budget} | reflect {ans.reflect_mode} | as_of {ans.as_of} | "
            f"confidence {ans.confidence}")
    console.print(Panel(f"{head}\n\n{ans.answer}", title=ans.question))
    if ans.structured:
        console.print(Panel(json.dumps(ans.structured, indent=1), title="Structured output"))
    for name, rows in ans.ground_truth.items():
        t = Table(title=f"{name} (database, as of {ans.as_of})")
        cols = list(rows[0]) if rows else ["(no rows)"]
        for c in cols:
            t.add_column(c)
        for r in rows[:15]:
            t.add_row(*[str(r.get(c)) for c in cols])
        console.print(t)
    checked = {True: "[green]checked[/]", False: "[red]NOT checked[/]", None: "n/a"}[ans.commitments_checked]
    cites = (f"Docs: {', '.join(ans.cited_doc_ids) or '-'}\nRecords: {', '.join(ans.cited_record_ids) or '-'}\n"
             f"Open commitments: {checked}")
    if ans.unverifiable_citations:
        cites += f"\n[yellow]Unverifiable: {', '.join(ans.unverifiable_citations)}[/]"
    console.print(Panel(cites, title="Evidence"))
    if ans.warnings:
        console.print(Panel("\n".join(ans.warnings), title="Warnings", border_style="yellow"))
```
In `main()`, add `src.add_argument("--ask", help="ask any supply-chain question (routed to one of 7 capabilities)")` to the mutually exclusive group. Then add this branch before `if a.question:` inside the `try`:
```python
        if a.ask:
            from agent.capabilities import ask
            from agent.parsing import parse_report

            if parse_report(a.ask).rpo_ids:  # a disruption report: run the simulated decision loop instead
                card = agent.run(a.ask, as_of=a.as_of)
                render_card(card, console, show_trace=a.trace)
                out = card.to_dict()
            else:
                ans = ask(agent, a.ask, a.as_of)
                render_capability(ans, console)
                out = ans.to_dict()
        elif a.question:
```
(Change the existing `if a.question:` to that `elif`.)

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_capabilities.py tests/test_console.py -v`
Expected: all pass

- [ ] **Step 7: Try one live capability (live)**

Run: `.venv/bin/python -m interface.decision_console --ask "What is SUP0247's delivery track record in November and December? Should we trust their current promise for a November delivery?"`
Expected:
- capability `supplier_reliability`, reflect `native` (as_of 2025-12-31 is after the corpus horizon);
- a `delay_by_promised_month` table whose months 11 and 12 show much higher `avg_delay_days`;
- an answer that flags the Nov-Dec pattern and cites docs and records.

- [ ] **Step 8: Commit**

```bash
git add agent/capabilities.py interface/decision_console.py tests/test_capabilities.py
git commit -m "feat: seven-capability router with grounded reflect handlers and --ask

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Eval scoring and `run_eval_questions.py`

**Files:**
- Create: `eval/scoring.py`, `eval/scorecard.py`, `eval/run_eval_questions.py`
- Test: `tests/test_eval_scoring.py`

**Interfaces:**
- Consumes: `build_agent`, `QAResult`, `load_index`, `as_of_end`.
- Produces:
  - `eval.scoring`:
    - `CORRECT_THRESHOLD = 0.75`
    - `key_facts(gold: str) -> set[str]`
    - `answer_score(gold: str, answer: str) -> float`
    - `citation_pr(pred, gold) -> tuple[float|None, float|None]`
    - `select_questions(questions, per_type: int, seed: int = 7, types=None) -> list[dict]`
    - `llm_judge(llm, question, gold, answer) -> bool`
  - `eval.scorecard`: `summarize_questions(rows) -> dict` (incl. `by_hop_count`, `single_hop_fact_recall_accuracy`), `summarize_holdout(rows) -> dict`, `summarize_patterns(rows) -> dict`, `write_scorecard(out_dir: Path) -> Path` (merges whichever of `questions_results.jsonl`, `questions_results_hindsight.jsonl`, `holdout_results.jsonl`, `pattern_results.jsonl` exist)
  - `eval.run_eval_questions.hindsight_answer(agent, q) -> QAResult`; `main(argv=None) -> int` with `--mode agent|hindsight`. It writes `eval/out/questions_results.jsonl` (or `questions_results_hindsight.jsonl`) and then calls `write_scorecard`.

- [ ] **Step 1: Write the failing tests**

`tests/test_eval_scoring.py`:
```python
import json

import pytest

from eval.scorecard import summarize_holdout, summarize_questions, write_scorecard
from eval.scoring import answer_score, citation_pr, key_facts, select_questions


def test_key_facts_extracts_ids_dates_numbers_and_words():
    f = key_facts("Expected 2 stockout days / $438; actual 3 days / $656 - partial. DEC00021 on 2023-04-14.")
    assert {"DEC00021", "2023-04-14", "#2", "#438", "#3", "#656", "@partial"} <= f


def test_answer_score_full_and_partial():
    gold = "2023-06-08 after the expedite (supersedes 2023-06-22)."
    assert answer_score(gold, "Latest ETA is 2023-06-08 (the 2023-06-22 date was superseded by the expedite).") == 1.0
    # key facts: 2023-06-08, 2023-06-22, @expedite -> only the stale date matches
    assert answer_score(gold, "The ETA is 2023-06-22.") == pytest.approx(1 / 3)
    assert answer_score("switch supplier + cancel original PO (DEC00006)", "They chose switch_supplier (DEC00006) and cancel_po.") == 1.0


def test_citation_pr():
    assert citation_pr(["A", "B"], ["B", "C"]) == (0.5, 0.5)
    assert citation_pr([], ["B"]) == (None, 0.0)
    assert citation_pr(["A"], []) == (0.0, None)


def test_select_questions_is_stratified_and_deterministic():
    qs = [{"question_id": f"Q{i:03d}", "type": t} for i, t in enumerate(["a", "b"] * 10)]
    s1, s2 = select_questions(qs, 3, seed=1), select_questions(qs, 3, seed=1)
    assert s1 == s2 and len(s1) == 6 and {q["type"] for q in s1} == {"a", "b"}


def test_scorecard_files(tmp_path):
    q = [{"question_id": "Q1", "type": "temporal", "hop_count": 1, "score": 1.0, "correct": True, "doc_precision": 1.0, "doc_recall": 0.5,
          "record_precision": None, "record_recall": 1.0, "future_doc_citations": [], "latency_s": 2.0,
          "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01}, "error": None}]
    h = [{"scenario_id": "HS05", "correct": True, "trap": True, "trap_pass": True, "precedent_recall": 1 / 3,
          "commitments_recall": 1.0, "parity_mismatches": [], "latency_s": 9.0,
          "usage": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.2}, "error": None}]
    (tmp_path / "questions_results.jsonl").write_text("\n".join(json.dumps(r) for r in q))
    (tmp_path / "holdout_results.jsonl").write_text("\n".join(json.dumps(r) for r in h))
    p = [{"pattern_id": "P01", "mental_models": True, "detected": True},
         {"pattern_id": "P01", "mental_models": False, "detected": False}]
    (tmp_path / "pattern_results.jsonl").write_text("\n".join(json.dumps(r) for r in p))
    s = summarize_questions(q)
    assert s["by_type"]["temporal"]["accuracy"] == 1.0 and s["by_hop_count"]["1"]["n"] == 1
    assert s["single_hop_fact_recall_accuracy"] is None  # no fact_recall rows
    assert summarize_holdout(h)["trap_accuracy"] == 1.0
    md = write_scorecard(tmp_path).read_text()
    assert "temporal" in md and "HS05" in md and "| hop_count |" in md and "1/12 with Mental Models" in md
    assert (tmp_path / "scorecard.json").exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_eval_scoring.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'eval.scoring'`

- [ ] **Step 3: Implement `eval/scoring.py`**

```python
"""Deterministic scoring of eval answers against gold answers and citations."""
from __future__ import annotations

import random
import re
from collections import defaultdict

CORRECT_THRESHOLD = 0.75
ACTIONS = ("accept_delay", "expedite", "switch_supplier", "reallocate_stock", "substitute_rm", "cancel_po",
           "build_safety_stock", "renegotiate", "reduce_allocation")
STATUS_WORDS = ("fulfilled", "breached", "renegotiated", "success", "partial", "failed")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_ID = re.compile(r"\b[A-Z]{1,4}\d{2,}\b")
_NUM = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")


def _num(s: str) -> str:
    v = float(s.replace("$", "").replace(",", ""))
    return "#" + (str(int(v)) if v == int(v) else f"{v:g}")


def _split(text: str) -> tuple[set, set, set]:
    dates = set(_DATE.findall(text))
    rest = _DATE.sub(" ", text)
    ids = set(_ID.findall(rest))
    rest = _ID.sub(" ", rest)
    nums = {_num(m) for m in _NUM.findall(rest) if re.search(r"\d", m)}
    return dates, ids, nums


def _words(text: str) -> set[str]:
    low = text.lower()
    out = {"@" + a for a in ACTIONS if a in low or a.replace("_", " ") in low}
    return out | {"@" + w for w in STATUS_WORDS if re.search(rf"\b{w}\b", low)}


def key_facts(gold: str) -> set[str]:
    dates, ids, nums = _split(gold)
    return dates | ids | nums | _words(gold)


def answer_score(gold: str, answer: str) -> float:
    facts = key_facts(gold)
    if not facts:
        g, a = set(re.findall(r"\w+", gold.lower())), set(re.findall(r"\w+", answer.lower()))
        return len(g & a) / len(g) if g else 0.0
    dates, ids, nums = _split(answer)
    found = (dates | ids | nums | _words(answer)) & facts
    return len(found) / len(facts)


def citation_pr(pred, gold) -> tuple[float | None, float | None]:
    p, g = set(pred), set(gold)
    hit = len(p & g)
    return (hit / len(p) if p else None, hit / len(g) if g else None)


def select_questions(questions: list[dict], per_type: int, seed: int = 7, types=None) -> list[dict]:
    by_type = defaultdict(list)
    for q in sorted(questions, key=lambda q: q["question_id"]):
        if types is None or q["type"] in types:
            by_type[q["type"]].append(q)
    rng = random.Random(seed)
    out = []
    for t in sorted(by_type):
        pool = by_type[t]
        out += sorted(rng.sample(pool, min(per_type, len(pool))), key=lambda q: q["question_id"])
    return out


def llm_judge(llm, question: str, gold: str, answer: str) -> bool:
    """Optional semantic check (--judge). First word of the reply must be CORRECT or INCORRECT."""
    resp = llm.create(system="You grade answers against a gold answer. Reply with CORRECT or INCORRECT as the first "
                             "word, then one short reason. Paraphrase is fine; missing or wrong ids, dates, numbers "
                             "or statuses are INCORRECT.",
                      messages=[{"role": "user", "content": f"Question: {question}\nGold: {gold}\nAnswer: {answer}"}],
                      effort="low")
    reply = "".join(b.text for b in resp.content if b.type == "text").strip().upper()
    return reply.startswith("CORRECT")
```

- [ ] **Step 4: Implement `eval/scorecard.py`**

```python
"""Aggregate eval results into scorecard.json + scorecard.md."""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def _cost(rows):
    costs = [(r.get("usage") or {}).get("cost_usd") for r in rows]
    known = [c for c in costs if c is not None]
    return {"mean_usd": _mean(known), "total_usd": round(sum(known), 4) if known else None}


def _latency(rows):
    lat = [r["latency_s"] for r in rows if r.get("latency_s") is not None]
    return {"mean_s": _mean(lat), "p50_s": round(statistics.median(lat), 2) if lat else None,
            "max_s": max(lat) if lat else None}


def _tokens(rows):
    return {"mean_input": _mean([(r.get("usage") or {}).get("input_tokens") for r in rows]),
            "mean_output": _mean([(r.get("usage") or {}).get("output_tokens") for r in rows])}


def summarize_questions(rows: list[dict]) -> dict:
    by = defaultdict(list)
    for r in rows:
        by[r["type"]].append(r)
    hops = defaultdict(list)
    for r in rows:
        hops[str(r.get("hop_count"))].append(r)
    return {
        "n": len(rows), "accuracy": _mean([float(r["correct"]) for r in rows]),
        "by_type": {t: {"n": len(rs), "accuracy": _mean([float(r["correct"]) for r in rs]),
                        "mean_score": _mean([r["score"] for r in rs])} for t, rs in sorted(by.items())},
        "by_hop_count": {h: {"n": len(rs), "accuracy": _mean([float(r["correct"]) for r in rs])}
                         for h, rs in sorted(hops.items())},
        "single_hop_fact_recall_accuracy": _mean([float(r["correct"]) for r in rows
                                                  if r["type"] == "fact_recall" and r.get("hop_count") == 1]),
        "citations": {k: _mean([r.get(k) for r in rows]) for k in
                      ("doc_precision", "doc_recall", "record_precision", "record_recall")},
        "future_doc_citations": sum(len(r.get("future_doc_citations") or []) for r in rows),
        "errors": sum(1 for r in rows if r.get("error")),
        "latency": _latency(rows), "tokens": _tokens(rows), "cost": _cost(rows),
    }


def summarize_holdout(rows: list[dict]) -> dict:
    traps = [r for r in rows if r.get("trap")]
    return {
        "n": len(rows), "accuracy": _mean([float(r["correct"]) for r in rows]),
        "trap_n": len(traps), "trap_accuracy": _mean([float(r["trap_pass"]) for r in traps]),
        "trap_results": {r["scenario_id"]: "PASS" if r["trap_pass"] else "FAIL" for r in traps},
        "precedent_recall": _mean([r.get("precedent_recall") for r in rows]),
        "commitments_recall": _mean([r.get("commitments_recall") for r in rows]),
        "simulator_parity_ok": sum(1 for r in rows if not r.get("parity_mismatches")),
        "errors": sum(1 for r in rows if r.get("error")),
        "latency": _latency(rows), "tokens": _tokens(rows), "cost": _cost(rows),
        "per_scenario": [{k: r.get(k) for k in ("scenario_id", "gold", "chosen", "correct", "trap", "trap_pass",
                                                "precedent_recall", "commitments_recall", "latency_s")} for r in rows],
    }


def summarize_patterns(rows: list[dict]) -> dict:
    per = defaultdict(dict)
    for r in rows:
        per[r["pattern_id"]]["on" if r["mental_models"] else "off"] = r["detected"]
    return {"per_pattern": dict(sorted(per.items())),
            "detected_with_mental_models": sum(1 for v in per.values() if v.get("on")),
            "detected_without_mental_models": sum(1 for v in per.values() if v.get("off"))}


def _read(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def _question_section(title: str, s: dict) -> list[str]:
    lines = [f"## {title} (n={s['n']}, accuracy {s['accuracy']}, single-hop fact recall "
             f"{s['single_hop_fact_recall_accuracy']}, errors {s['errors']}, future-dated doc citations "
             f"{s['future_doc_citations']})", "",
             "| type | n | accuracy | mean key-fact score |", "|---|---|---|---|"]
    lines += [f"| {t} | {v['n']} | {v['accuracy']} | {v['mean_score']} |" for t, v in s["by_type"].items()]
    lines += ["", "| hop_count | n | accuracy |", "|---|---|---|"]
    lines += [f"| {h} | {v['n']} | {v['accuracy']} |" for h, v in s["by_hop_count"].items()]
    c = s["citations"]
    lines += ["", f"Citation precision/recall - docs {c['doc_precision']}/{c['doc_recall']}, records "
                  f"{c['record_precision']}/{c['record_recall']}",
              f"Latency {s['latency']} | tokens {s['tokens']} | cost {s['cost']}", ""]
    return lines


def write_scorecard(out_dir: Path) -> Path:
    q, h = _read(out_dir / "questions_results.jsonl"), _read(out_dir / "holdout_results.jsonl")
    qh, p = _read(out_dir / "questions_results_hindsight.jsonl"), _read(out_dir / "pattern_results.jsonl")
    data = {"questions": summarize_questions(q) if q else None,
            "questions_hindsight_mode": summarize_questions(qh) if qh else None,
            "holdout": summarize_holdout(h) if h else None,
            "patterns": summarize_patterns(p) if p else None}
    (out_dir / "scorecard.json").write_text(json.dumps(data, indent=2))
    lines = ["# Scorecard", ""]
    if data["questions"]:
        lines += _question_section("Eval questions - agent mode", data["questions"])
    if data["questions_hindsight_mode"]:
        lines += _question_section("Eval questions - hindsight mode (recall/reflect only)",
                                   data["questions_hindsight_mode"])
    if data["patterns"]:
        s = data["patterns"]
        lines += [f"## Pattern detection ({s['detected_with_mental_models']}/12 with Mental Models, "
                  f"{s['detected_without_mental_models']}/12 without)", "",
                  "| pattern | with Mental Models | without |", "|---|---|---|"]
        lines += [f"| {pid} | {v.get('on')} | {v.get('off')} |" for pid, v in s["per_pattern"].items()]
        lines.append("")
    if data["holdout"]:
        s = data["holdout"]
        lines += [f"## Holdout scenarios (n={s['n']}, accuracy {s['accuracy']}, trap accuracy {s['trap_accuracy']} "
                  f"on {s['trap_n']}, simulator parity {s['simulator_parity_ok']}/{s['n']}, errors {s['errors']})", "",
                  "| scenario | gold | chosen | correct | trap | trap pass | precedent recall | commitments recall | latency s |",
                  "|---|---|---|---|---|---|---|---|---|"]
        lines += [f"| {r['scenario_id']} | {r['gold']} | {r['chosen']} | {r['correct']} | {r['trap']} | {r['trap_pass']} | "
                  f"{r['precedent_recall']} | {r['commitments_recall']} | {r['latency_s']} |" for r in s["per_scenario"]]
        lines += ["", f"Latency {s['latency']} | tokens {s['tokens']} | cost {s['cost']}"]
    md = out_dir / "scorecard.md"
    md.write_text("\n".join(lines) + "\n")
    return md
```

- [ ] **Step 5: Run the scoring tests**

Run: `.venv/bin/python -m pytest tests/test_eval_scoring.py -v`
Expected: 5 passed

- [ ] **Step 6: Implement `eval/run_eval_questions.py`**

```python
"""Run the QA agent on eval_questions.jsonl, respecting each question's as_of_date, and score it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.agent_core import build_agent
from agent.config import ROOT, load_settings
from agent.corpus import as_of_end, load_index
from agent.setup_data import ensure_db
import re
import time

from agent.card import QAResult
from agent.sql_tools import record_exists
from eval.scorecard import write_scorecard
from eval.scoring import CORRECT_THRESHOLD, answer_score, citation_pr, llm_judge, select_questions

_RECORD_ID = re.compile(r"\b(?:DECL|CMTL|RPOL|RPO|REV|EVT|DEC|CMT|NEG|CTR|CAT|POL|PR|PO|RT|SUP|RM|IP)\d+\b|\b[PW]\d{2,3}\b")


def hindsight_answer(agent, q: dict) -> QAResult:
    """The build prompt's eval strategy, as-of safe: recall(mid) for single-hop fact recall, reflect(high) otherwise
    (reflect falls back to local synthesis over as-of-filtered recall when native reflect could see later memory)."""
    t0 = time.monotonic()
    agent.llm.usage = type(agent.llm.usage)()
    if q["type"] == "fact_recall" and q["hop_count"] == 1:
        r = agent.memory.recall(q["question"], q["as_of_date"], budget="mid")
        answer, docs = "\n".join(h.text for h in r.hits[:10]), r.doc_ids()
    else:
        r = agent.memory.reflect(q["question"], q["as_of_date"], budget="high")
        answer, docs = r.text or "", r.doc_ids
    con = agent._connect(q["as_of_date"])
    try:
        recs = [x for x in dict.fromkeys(_RECORD_ID.findall(answer)) if record_exists(con, x)]
    finally:
        con.close()
    return QAResult(question=q["question"], as_of=q["as_of_date"], answer=answer, cited_doc_ids=docs,
                    cited_record_ids=recs, usage=agent.llm.usage.to_dict(), latency_s=round(time.monotonic() - t0, 2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--per-type", type=int, default=5, help="questions sampled per type (default 5 -> 40 total)")
    ap.add_argument("--all", action="store_true", help="run all 241 questions")
    ap.add_argument("--types", nargs="*", help="restrict to these question types")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--judge", action="store_true", help="also grade with an LLM judge; correct = judge verdict")
    ap.add_argument("--mode", choices=["agent", "hindsight"], default="agent",
                    help="agent = QA tool loop; hindsight = the build prompt's strategy (recall budget=mid for "
                         "single-hop fact_recall, reflect budget=high otherwise), still as-of filtered")
    ap.add_argument("--out", default=str(ROOT / "eval" / "out"))
    a = ap.parse_args(argv)

    settings = load_settings()
    ensure_db(settings)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    live = out / "eval_live.sqlite"  # fresh and empty: runtime decisions from demos are invisible
    live.unlink(missing_ok=True)
    agent = build_agent(settings, live_db_path=live)
    index = load_index(settings.corpus_path)
    qs = [json.loads(l) for l in (settings.dataset_dir / "eval_questions.jsonl").open()]
    selected = qs if a.all else select_questions(qs, a.per_type, a.seed, a.types)
    results_path = out / ("questions_results.jsonl" if a.mode == "agent" else "questions_results_hindsight.jsonl")
    results_path.write_text("")
    for i, q in enumerate(selected, 1):
        row = {"question_id": q["question_id"], "type": q["type"], "hop_count": q["hop_count"], "mode": a.mode,
               "as_of": q["as_of_date"], "gold": q["gold_answer"]}
        try:
            res = (agent.answer_question(q["question"], q["as_of_date"]) if a.mode == "agent"
                   else hindsight_answer(agent, q))
        except Exception as ex:  # harness: one failed question must not abort the run; it is recorded and scored wrong
            row.update(error=f"{type(ex).__name__}: {ex}", score=0.0, correct=False, answer="", latency_s=None, usage={},
                       doc_precision=None, doc_recall=0.0, record_precision=None, record_recall=0.0,
                       future_doc_citations=[])
        else:
            score = answer_score(q["gold_answer"], res.answer)
            judged = llm_judge(agent.llm, q["question"], q["gold_answer"], res.answer) if a.judge else None
            dp, dr = citation_pr(res.cited_doc_ids, q["gold_doc_ids"])
            rp, rr = citation_pr(res.cited_record_ids, q["gold_record_ids"])
            end = as_of_end(q["as_of_date"])
            leaks = [d for d in res.cited_doc_ids if d in index.by_id and index.by_id[d].timestamp > end]
            row.update(error=None, answer=res.answer, score=round(score, 3), judged=judged,
                       correct=judged if a.judge else score >= CORRECT_THRESHOLD,
                       doc_precision=dp, doc_recall=dr, record_precision=rp, record_recall=rr,
                       cited_doc_ids=res.cited_doc_ids, cited_record_ids=res.cited_record_ids,
                       future_doc_citations=leaks, latency_s=res.latency_s, usage=res.usage)
        with results_path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"[{i}/{len(selected)}] {q['question_id']} {q['type']:<27} score={row['score']} "
              f"correct={row['correct']} {row['error'] or ''}")
    print(f"scorecard: {write_scorecard(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Smoke-run on a tiny sample (live)**

Run: `.venv/bin/python -m eval.run_eval_questions --per-type 1` and then `.venv/bin/python -m eval.run_eval_questions --per-type 1 --mode hindsight`
Expected: 8 lines, one per type. `eval/out/questions_results.jsonl` has 8 rows and `eval/out/scorecard.md` is written. `future_doc_citations` must be 0. Any non-zero value is a leakage bug: stop and fix it before continuing.

- [ ] **Step 8: Commit**

```bash
git add eval/scoring.py eval/scorecard.py eval/run_eval_questions.py tests/test_eval_scoring.py
git commit -m "feat: eval-question harness with as-of scoring and scorecard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Holdout harness and full Stage 4 run

**Files:**
- Create: `eval/run_holdout_scenarios.py`
- Test: `tests/test_holdout_harness.py`

**Interfaces:**
- Consumes: `build_agent`, `load_holdout`, `holdout_context`, `compare_to_gold`, `write_scorecard`.
- Produces: `score_card(card: DecisionCard, scenario: dict) -> dict`, with keys `scenario_id, gold, chosen, correct, trap, trap_pass, precedent_recall, commitments_recall, parity_mismatches, latency_s, usage, error`. Also `main(argv=None) -> int`, which writes `eval/out/holdout_results.jsonl` and `eval/out/cards/HSxx.json`.

- [ ] **Step 1: Write the failing test**

`tests/test_holdout_harness.py`:
```python
from agent.card import DecisionCard
from agent.scenarios import load_holdout
from eval.run_holdout_scenarios import score_card


def test_score_card_trap_and_recalls(base_settings):
    s = load_holdout(base_settings)["HS05"]
    card = DecisionCard(run_id="r", as_of=s["day0"], report="", entities={}, recommended_action="switch_supplier",
                        options=[{**a, "breaches_commitments": []} for a in s["candidate_actions"]],
                        precedents=[{"event_id": "EVT02501"}], related_event_ids=["EVT02515"],
                        open_commitments=[{"commitment_id": c["commitment_id"]} for c in s["open_commitments"][:3]])
    r = score_card(card, s)
    assert r["correct"] is True and r["trap"] is True and r["trap_pass"] is True
    assert r["precedent_recall"] == round(2 / 3, 3)
    assert r["commitments_recall"] == 0.5
    assert r["parity_mismatches"] == []
    card.recommended_action = "accept_delay"
    assert score_card(card, s)["trap_pass"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_holdout_harness.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'eval.run_holdout_scenarios'`

- [ ] **Step 3: Implement `eval/run_holdout_scenarios.py`**

```python
"""Run the full decision loop on every holdout scenario and compare with the gold best action."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.agent_core import build_agent
from agent.card import DecisionCard
from agent.config import ROOT, load_settings
from agent.scenarios import compare_to_gold, holdout_context, load_holdout
from agent.setup_data import ensure_db
from eval.scorecard import write_scorecard


def score_card(card: DecisionCard, s: dict) -> dict:
    chosen, gold, trap = card.recommended_action, s["gold_best_action"], s["trap"]
    surfaced = ({p.get("event_id") for p in card.precedents} | set(card.related_event_ids)
                | {r for r in card.cited_record_ids if r.startswith("EVT")})
    gold_prec = s["gold_precedent_event_ids"]
    gold_c = {c["commitment_id"] for c in s["open_commitments"]}
    flagged = {c["commitment_id"] for c in card.open_commitments}
    return {"scenario_id": s["scenario_id"], "gold": gold, "chosen": chosen, "correct": chosen == gold,
            "trap": bool(trap), "trap_pass": (chosen == gold and chosen != trap["its_action"]) if trap else None,
            "precedent_recall": round(len(set(gold_prec) & surfaced) / len(gold_prec), 3) if gold_prec else None,
            "commitments_recall": round(len(gold_c & flagged) / len(gold_c), 3) if gold_c else None,
            "parity_mismatches": compare_to_gold(card.options, s["candidate_actions"]),
            "latency_s": card.latency_s, "usage": card.usage, "error": None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenarios", nargs="*", help="subset, e.g. HS02 HS05")
    ap.add_argument("--out", default=str(ROOT / "eval" / "out"))
    a = ap.parse_args(argv)
    settings = load_settings()
    ensure_db(settings)
    out = Path(a.out)
    (out / "cards").mkdir(parents=True, exist_ok=True)
    live = out / "holdout_live.sqlite"  # fresh and empty; scenarios never write back
    live.unlink(missing_ok=True)
    agent = build_agent(settings, live_db_path=live)
    scenarios = load_holdout(settings)
    ids = a.scenarios or sorted(scenarios)
    results = out / "holdout_results.jsonl"
    results.write_text("")
    for sid in ids:
        s = scenarios[sid]
        try:
            card = agent.run(s["day0_report"], as_of=s["day0"], context=holdout_context(s), writeback=False)
        except Exception as ex:  # harness: record and continue
            row = {"scenario_id": sid, "gold": s["gold_best_action"], "chosen": None, "correct": False,
                   "trap": bool(s["trap"]), "trap_pass": False if s["trap"] else None, "precedent_recall": None,
                   "commitments_recall": None, "parity_mismatches": [], "latency_s": None, "usage": {},
                   "error": f"{type(ex).__name__}: {ex}"}
        else:
            row = score_card(card, s)
            (out / "cards" / f"{sid}.json").write_text(json.dumps(card.to_dict(), indent=2, default=str))
        with results.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"{sid} gold={row['gold']:<17} chosen={str(row['chosen']):<17} correct={row['correct']} "
              f"trap={row['trap']} trap_pass={row['trap_pass']} {row['error'] or ''}")
    print(f"scorecard: {write_scorecard(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the unit test**

Run: `.venv/bin/python -m pytest tests/test_holdout_harness.py -v`
Expected: 1 passed

- [ ] **Step 5: Stage 4 gate - full eval runs (live, costs money)**

Before running, tell the user the expected spend. Take one HS05 card's `usage.cost_usd` from Task 10 Step 5 and multiply by 14, then add about 40 QA questions at the Task 11 Step 7 per-question cost.

```bash
.venv/bin/python -m eval.run_holdout_scenarios
.venv/bin/python -m eval.run_eval_questions --per-type 5
```
Expected:
- 14 holdout lines, then 40 question lines.
- `eval/out/scorecard.md` contains both sections and `simulator parity 14/14`.
- `future-dated doc citations 0`.
- Trap results listed for HS02, HS05, HS08, HS10.

Record the headline numbers exactly as produced; do not tune prompts inside this task. Commit the scorecard as a deliverable:

```bash
git add eval/run_holdout_scenarios.py tests/test_holdout_harness.py eval/out/scorecard.md eval/out/scorecard.json eval/out/holdout_results.jsonl eval/out/questions_results.jsonl
git commit -m "feat: holdout harness and first scorecard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12A: Pattern-detection probe (with and without Mental Models) and the build prompt's eval mode

**Files:**
- Create: `eval/pattern_probe.yaml`, `eval/run_pattern_probe.py`
- Test: `tests/test_pattern_probe.py`

**Interfaces:**
- Consumes: `build_agent`, `capabilities.ask`, `write_scorecard` / `summarize_patterns` (Task 11), `Settings.mental_models_in_reflect`.
- Produces: `load_probes() -> list[dict]` (keys `id, question, rules`), `detected(text: str, rules: list[str]) -> bool`, `main(argv=None) -> int`. `main` writes `eval/out/pattern_results.jsonl` with keys `pattern_id, mental_models, question, capability, reflect_mode, detected, answer, cited_doc_ids, cited_record_ids, latency_s, error`.

- [ ] **Step 1: Write `eval/pattern_probe.yaml`**

Each question names the entity but not the behaviour. A pattern counts as detected only when every rule (a case-insensitive regex) matches the answer.
```yaml
- id: P01
  question: How reliable are SUP0247's raw-material deliveries, and is there anything planners should watch for?
  rules: ["nov|dec|q4|year[- ]end", "late|delay|slip"]
- id: P02
  question: What delivery risks should we plan for with raw materials from China-based suppliers?
  rules: ["feb|lunar|chinese new year", "late|delay|congest"]
- id: P03
  question: What supply risks should we know about for RM0046 from SUP0223?
  rules: ["price", "short|stockout|stock-out|ran out"]
- id: P04
  question: How reliable is SUP0091 on raw-material orders?
  rules: ["large|big|size|small|volume", "late|on[- ]time|reliab|miss"]
- id: P05
  question: What should we know before expediting finished goods into warehouse W005?
  rules: ["cost|premium|expens", "2x|twice|double|higher|more"]
- id: P06
  question: What scheduling risks exist for production runs at plant P02?
  rules: ["quarter|first (?:two )?weeks|weeks? 1", "maint"]
- id: P07
  question: Are there any quality concerns with RM0016 at plant P02?
  rules: ["reject|qa\\b|quality", "RM0022|substitut|switch"]
- id: P08
  question: Is plant P03 a good donor plant for raw-material transfers?
  rules: ["safety stock|below|short|deplet", "\\bnot\\b|avoid|risk|caution"]
- id: P09
  question: Can we rely on SUP0237's revised delivery dates when a lot is late?
  rules: ["miss|broken|slip(?:s|ped)? again|unreliab|not reliable|cannot rely|can't rely"]
- id: P10
  question: How do our demand forecasts compare with actual demand across product categories?
  rules: ["consumables", "over|above|bias|too high|higher"]
- id: P11
  question: How are SUP0179's lead times trending, and what does that mean for us?
  rules: ["lengthen|increas|creep|longer|grow|rising|worsen"]
- id: P12
  question: What should we know about finished-goods deliveries from SUP0005?
  rules: ["april|\\bapr\\b", "late|delay"]
```

- [ ] **Step 2: Write the failing tests**

`tests/test_pattern_probe.py`:
```python
import json
import re

from eval.run_pattern_probe import detected, load_probes


def test_probes_cover_all_patterns_neutrally(base_settings):
    patterns = json.loads((base_settings.dataset_dir / "planted_patterns.json").read_text())["patterns"]
    probes = load_probes()
    assert [p["id"] for p in probes] == [p["pattern_id"] for p in patterns]
    for p in probes:
        assert not re.search(r"\d+\s*%|\bdays?\b.*\blate\b|november|february|april|quarter", p["question"], re.I), p["id"]
        for r in p["rules"]:
            re.compile(r)


def test_detected_requires_every_rule():
    rules = ["nov|dec", "late|delay"]
    assert detected("SUP0247 deliveries promised in November arrive ~13 days late.", rules)
    assert not detected("SUP0247 is usually late.", rules)
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_pattern_probe.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'eval.run_pattern_probe'`

- [ ] **Step 4: Implement `eval/run_pattern_probe.py`**

```python
"""Pattern-detection probe: for each planted pattern, ask a neutral question about its entity and check whether the
answer surfaces the pattern. Runs with the Mental Models and without them (MENTAL_MODELS_IN_REFLECT=0), so the score
is not just the answer key being read back."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import replace
from pathlib import Path

import yaml

from agent.agent_core import build_agent
from agent.capabilities import ask
from agent.config import ROOT, load_settings
from agent.setup_data import ensure_db
from eval.scorecard import write_scorecard

PROBES = Path(__file__).with_name("pattern_probe.yaml")


def load_probes() -> list[dict]:
    return yaml.safe_load(PROBES.read_text())


def detected(text: str, rules: list[str]) -> bool:
    return all(re.search(r, text, re.I) for r in rules)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mental-models", choices=["on", "off", "both"], default="both")
    ap.add_argument("--out", default=str(ROOT / "eval" / "out"))
    a = ap.parse_args(argv)
    base = load_settings()
    ensure_db(base)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    results = out / "pattern_results.jsonl"
    results.write_text("")
    modes = {"on": [True], "off": [False], "both": [True, False]}[a.mental_models]
    for flag in modes:
        live = out / "pattern_live.sqlite"
        live.unlink(missing_ok=True)
        try:
            agent = build_agent(replace(base, mental_models_in_reflect=flag), live_db_path=live)
        except ValueError as ex:  # client cannot exclude mental models: say so instead of mislabelling results
            print(f"mental_models={flag}: skipped - {ex}")
            continue
        for p in load_probes():
            row = {"pattern_id": p["id"], "mental_models": flag, "question": p["question"]}
            try:
                ans = ask(agent, p["question"], base.default_as_of)
            except Exception as ex:  # harness: record and continue
                row.update(detected=False, error=f"{type(ex).__name__}: {ex}")
            else:
                text = ans.answer + " " + json.dumps(ans.structured or {})
                row.update(capability=ans.capability, reflect_mode=ans.reflect_mode, detected=detected(text, p["rules"]),
                           answer=ans.answer, cited_doc_ids=ans.cited_doc_ids, cited_record_ids=ans.cited_record_ids,
                           latency_s=ans.latency_s,
                           error=None if ans.reflect_mode == "native" else
                           "native reflect unavailable (runtime retains in the bank) - Mental Models not consulted")
            with results.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            print(f"{p['id']} mental_models={flag} detected={row['detected']} {row.get('error') or ''}")
    print(f"scorecard: {write_scorecard(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_pattern_probe.py tests/test_eval_scoring.py -v`
Expected: all pass

- [ ] **Step 6: Stage 4 gate, part 2 (live, costs money; run BEFORE the demo's write-back step)**

Tell the user the expected spend first: 24 probe calls, plus 40 questions in hindsight mode, at the per-call cost measured in Task 11 Step 7.
```bash
.venv/bin/python -m eval.run_pattern_probe
.venv/bin/python -m eval.run_eval_questions --per-type 5 --mode hindsight
```
Expected:
- `eval/out/scorecard.md` gains a "Pattern detection (x/12 with Mental Models, y/12 without)" section and a "hindsight mode" question section with hop_count rows.
- Every probe row has `reflect_mode` = `native`. If not, runtime retains exist in the bank and the Mental Model comparison is invalid, so fix that first.

Compare with the build prompt's targets: pattern detection at least 8/12 with Mental Models, and single-hop fact recall at least 60%. Report the numbers as produced, whether or not they meet the targets.

```bash
git add eval/pattern_probe.yaml eval/run_pattern_probe.py tests/test_pattern_probe.py eval/out/scorecard.md eval/out/scorecard.json eval/out/pattern_results.jsonl eval/out/questions_results_hindsight.jsonl
git commit -m "feat: pattern-detection probe with/without mental models and hindsight-mode eval

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Scripted demo with expected vs actual

**Files:**
- Create: `demo/scenarios.yaml`, `demo/run_demo.py`, `demo/EXPECTED.md`
- Test: `tests/test_demo_checks.py`

**Interfaces:**
- Consumes: `build_agent`, `load_holdout`, `holdout_context`, `render_card`, `render_answer`.
- Produces: `check_expectations(spec: dict, result: dict) -> list[dict]`, where each item has keys `check, expected, actual, ok`. Also `main(argv=None) -> int`, which writes `demo/out/demo_report.md` and `demo/out/<id>.json`.

- [ ] **Step 1: Write `demo/scenarios.yaml`**

```yaml
# Scripted walkthrough. kind: holdout | report | question | chain
- id: D1
  title: Routine slip - memory, ground truth and simulator agree
  kind: holdout
  scenario: HS01
  expect:
    recommended_action: switch_supplier
    commitments_flagged: [CMT00786, CMT00802]
- id: D2
  title: Trap - the most similar precedent (accept_delay worked in Aug-Oct) is misleading in Nov-Dec
  kind: holdout
  scenario: HS05
  expect:
    recommended_action: switch_supplier
    not_action: accept_delay
    precedent_not_applicable: accept_delay
- id: D3
  title: Open-commitment conflict - the naive cancel would breach volume commitment CMT00560
  kind: report
  as_of: "2025-01-13"
  report: >-
    2025-01-13 - Supplier 169 (SUP0169) just moved RPO016183 (Elastic Film Laminate, RM0100) for Eastfield Plant (P03)
    to 2025-03-10 citing weather force majeure. Run PR0016522 is planned 2025-02-17 and needs this material.
    The buyer proposes cancelling RPO016183 and re-buying elsewhere. What should we do?
  context: {rpo_id: RPO016183, run_ids: [PR0016522], rm_id: RM0100}
  expect:
    recommended_action_in: [switch_supplier, reallocate_stock]
    not_action: cancel_po
    guardrail_mentions: CMT00560
- id: D4
  title: Contradiction / supersession - the later ETA wins
  kind: question
  as_of: "2023-06-11"
  question: What is the latest ETA for RPO003179?
  expect:
    answer_contains: ["2023-06-08"]
    cites_doc: DOC000254
- id: D5
  title: Closing the loop - today's logged decision is a precedent for the next disruption
  kind: chain
  steps: [HS05, HS08]
  expect:
    second_has_live_precedent: true
```

- [ ] **Step 2: Write the failing tests**

`tests/test_demo_checks.py`:
```python
from demo.run_demo import check_expectations


def test_checks_pass_and_fail():
    card = {"recommended_action": "switch_supplier", "guardrail_events": ["Rejected cancel_po: would breach CMT00560"],
            "open_commitments": [{"commitment_id": "CMT00786"}],
            "precedents": [{"decision_type": "accept_delay", "applies_today": False, "source": "historical"}]}
    spec = {"recommended_action": "switch_supplier", "not_action": "cancel_po", "guardrail_mentions": "CMT00560",
            "commitments_flagged": ["CMT00786", "CMT00802"], "precedent_not_applicable": "accept_delay"}
    res = {c["check"]: c["ok"] for c in check_expectations(spec, {"card": card})}
    assert res == {"recommended_action": True, "not_action": True, "guardrail_mentions": True,
                   "commitments_flagged": False, "precedent_not_applicable": True}


def test_question_and_chain_checks():
    ans = {"answer": "Latest ETA 2023-06-08 (2023-06-22 superseded).", "cited_doc_ids": ["DOC000254"]}
    res = check_expectations({"answer_contains": ["2023-06-08"], "cites_doc": "DOC000254"}, {"answer": ans})
    assert all(c["ok"] for c in res)
    chain = {"cards": [{}, {"precedents": [{"source": "live", "decision_id": "DECL00001"}]}]}
    assert check_expectations({"second_has_live_precedent": True}, chain)[0]["ok"] is True
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_demo_checks.py -v`
Expected: ERROR `ModuleNotFoundError: No module named 'demo.run_demo'`

- [ ] **Step 4: Implement `demo/run_demo.py`**

```python
"""Scripted 5-scenario walkthrough: runs each scenario, renders it, checks expected vs actual."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import yaml
from rich.console import Console

DEMO_DIR = Path(__file__).resolve().parent


def check_expectations(spec: dict, result: dict) -> list[dict]:
    card, ans = result.get("card") or {}, result.get("answer") or {}
    rec = card.get("recommended_action")
    out = []

    def add(name, expected, actual, ok):
        out.append({"check": name, "expected": expected, "actual": actual, "ok": bool(ok)})

    for k, v in spec.items():
        if k == "recommended_action":
            add(k, v, rec, rec == v)
        elif k == "recommended_action_in":
            add(k, v, rec, rec in v)
        elif k == "not_action":
            add(k, f"not {v}", rec, rec is not None and rec != v)
        elif k == "guardrail_mentions":
            ev = card.get("guardrail_events") or []
            add(k, v, ev, any(v in e for e in ev))
        elif k == "commitments_flagged":
            got = sorted(c["commitment_id"] for c in card.get("open_commitments") or [])
            add(k, v, got, set(v) <= set(got))
        elif k == "precedent_not_applicable":
            ps = card.get("precedents") or []
            add(k, f"{v} precedent marked not applicable", [(p["decision_type"], p["applies_today"]) for p in ps],
                any(p["decision_type"] == v and not p["applies_today"] for p in ps))
        elif k == "answer_contains":
            add(k, v, ans.get("answer"), all(x in (ans.get("answer") or "") for x in v))
        elif k == "cites_doc":
            add(k, v, ans.get("cited_doc_ids"), v in (ans.get("cited_doc_ids") or []))
        elif k == "second_has_live_precedent":
            ps = (result.get("cards") or [{}, {}])[1].get("precedents") or []
            add(k, v, [p.get("decision_id") for p in ps if p.get("source") == "live"],
                any(p.get("source") == "live" for p in ps) == v)
        else:
            add(k, v, "unknown check", False)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", nargs="*", help="demo ids to run, e.g. D2 D3")
    ap.add_argument("--no-retain", action="store_true", help="write live rows but do not retain into Hindsight")
    a = ap.parse_args(argv)

    from agent.agent_core import build_agent
    from agent.config import load_settings
    from agent.scenarios import holdout_context, load_holdout
    from agent.setup_data import ensure_db
    from interface.decision_console import render_answer, render_card

    settings = load_settings()
    if a.no_retain:
        settings = replace(settings, writeback_retain=False)
    ensure_db(settings)
    out = DEMO_DIR / "out"
    out.mkdir(exist_ok=True)
    live = out / "live_demo.sqlite"
    live.unlink(missing_ok=True)  # every demo run starts from an empty live DB
    agent = build_agent(settings, live_db_path=live)
    holdout = load_holdout(settings)
    console = Console(record=True, width=170)
    specs = [s for s in yaml.safe_load((DEMO_DIR / "scenarios.yaml").read_text()) if not a.only or s["id"] in a.only]
    report = ["# Demo walkthrough - expected vs actual", ""]
    all_ok = True
    for spec in specs:
        console.rule(f"{spec['id']} - {spec['title']}")
        if spec["kind"] == "holdout":
            s = holdout[spec["scenario"]]
            card = agent.run(s["day0_report"], as_of=s["day0"], context=holdout_context(s))
            render_card(card, console)
            result = {"card": card.to_dict()}
        elif spec["kind"] == "report":
            card = agent.run(spec["report"], as_of=spec["as_of"], context=spec.get("context"))
            render_card(card, console)
            result = {"card": card.to_dict()}
        elif spec["kind"] == "question":
            res = agent.answer_question(spec["question"], spec["as_of"])
            render_answer(res, console)
            result = {"answer": res.to_dict()}
        else:  # chain: first step writes back, the next one should see it
            cards = []
            for i, sid in enumerate(spec["steps"]):
                s = holdout[sid]
                card = agent.run(s["day0_report"], as_of=s["day0"], context=holdout_context(s), writeback=(i == 0))
                render_card(card, console)
                cards.append(card.to_dict())
            result = {"cards": cards}
        (out / f"{spec['id']}.json").write_text(json.dumps(result, indent=2, default=str))
        checks = check_expectations(spec["expect"], result)
        all_ok &= all(c["ok"] for c in checks)
        report += [f"## {spec['id']} - {spec['title']}", "", "| check | expected | actual | result |", "|---|---|---|---|"]
        report += [f"| {c['check']} | {c['expected']} | {c['actual']} | {'PASS' if c['ok'] else 'FAIL'} |" for c in checks]
        report.append("")
    (out / "demo_report.md").write_text("\n".join(report))
    (out / "demo_console.txt").write_text(console.export_text())
    print(f"demo report: {out / 'demo_report.md'} ({'all checks passed' if all_ok else 'some checks FAILED'})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Write `demo/EXPECTED.md`**

```markdown
# Demo - what each scenario should show

Run: `.venv/bin/python -m demo.run_demo`. The actual results land in `demo/out/demo_report.md` (a PASS/FAIL table per
check) and `demo/out/demo_console.txt` (the rendered cards).

| id | scenario | expected behaviour | where to look on the card |
|---|---|---|---|
| D1 | HS01 - SUP0192 moves RPO023175 (RM0079, P04) to 2025-10-30 | `switch_supplier` (spot lot from SUP0177, risk-adjusted score 36, 0 stockout days). accept_delay would cost $25,386 (9 stockout days). Open plant commitments CMT00786 / CMT00802 are flagged. | options table ★▲, Open commitments |
| D2 | HS05 - SUP0247 moves RPO023030 (RM0083, P01) to 2025-12-07 | **Trap.** The most similar precedent, DEC00353 (accept_delay, 2025-08-28), succeeded, and three later SUP0247 accept_delay decisions (DEC00364/373/377) are still awaiting an outcome on 2025-10-14. But the new ETA falls in Nov-Dec, when SUP0247 historically slips another 7-14 days. accept_delay is high risk (score 31,826) against 2 for `switch_supplier`. The card marks DEC00353 "no - misleading if copied" and recommends switch_supplier. | Precedents table, rationale |
| D3 | EVT01987 - SUP0169 moves RPO016183 (RM0100, P03) to 2025-03-10; the buyer proposes cancelling | **Commitment conflict.** CMT00560 ("We order at least 44,060 m of Elastic Film Laminate from SUP0169…", open until 2025-12-30) is caught twice: in the pre-check on the proposed action, and in the guardrail if the model submits cancel_po. Recommendation is `switch_supplier` or `reallocate_stock` (both score 1). | Guardrails panel, Open commitments "Affected by: cancel_po" |
| D4 | Q0020 - "What is the latest ETA for RPO003179?" as of 2023-06-11 | **Supersession.** DOC000248 (2023-06-02) says 2023-06-22; DOC000254 (2023-06-04) supersedes it with an expedite ETA of 2023-06-08. The answer is 2023-06-08, cites DOC000254 and names the superseded date. | Answer panel |
| D5 | HS05 with write-back, then HS08 (same supplier, 2025-10-19) | **Closing the loop.** HS05 writes DECL00001 + CMTL00001 to `demo/out/live_demo.sqlite` and retains a summary in Hindsight. The ERP/portal steps print as `[MOCK - no external call made]`. HS08's precedents table then lists DECL00001 with source `live`. | Write-back panel, second card's Precedents |

D1-D4 are read-only. D5 writes only to `demo/out/live_demo.sqlite`, plus one Hindsight retain per run (skip it with
`--no-retain`). Eval runs use their own empty live DB, so demo decisions never reach eval.
```

- [ ] **Step 6: Run the tests, then the demo (live)**

Run: `.venv/bin/python -m pytest tests/test_demo_checks.py -v`
Expected: 2 passed

Run: `.venv/bin/python -m demo.run_demo`
Expected: five rendered sections, and `demo/out/demo_report.md` with PASS rows. For any FAIL, keep the actual output (it is the "actual" column) and look at the card JSON. Fix a code bug if there is one. Do not edit expectations to match the output.

- [ ] **Step 7: Commit**

```bash
git add demo/scenarios.yaml demo/run_demo.py demo/EXPECTED.md tests/test_demo_checks.py demo/out/demo_report.md demo/out/demo_console.txt
git commit -m "feat: scripted demo walkthrough with expected vs actual checks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: README and final verification (Stage 5)

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: every CLI entry point above; `eval/out/scorecard.md`; `demo/out/demo_report.md`.

- [ ] **Step 1: Write `README.md`**

The README must contain these sections, filled with the real commands and numbers from this branch:

````markdown
# Supply Chain Memory & Decision Agent (prototype)

Given a free-text disruption report, the agent:
- recalls history from a Hindsight memory bank;
- connects it to structured ground truth (decisions, commitments, event links, scorecards);
- simulates candidate actions deterministically;
- recommends one with cited evidence, and can log it back so the next disruption benefits.

## Architecture

```mermaid
flowchart LR
  R[Disruption report] --> P[parse_report]
  P --> S[simulate.py\nport of generator\nconsequence model]
  P --> F{parallel evidence fan-out}
  F --> H1[Hindsight recall: broad]
  F --> H2[Hindsight recall: entity]
  F --> H3[Hindsight recall: 120-day window]
  F --> Q[sql_tools: commitments, scorecard,\nevents, links, prior decisions]
  H1 & H2 & H3 --> AF[as-of filter +\nsupersession via DocIndex]
  AF --> RF[reflect: native if safe,\nelse local synthesis]
  S & AF & RF & Q --> L[Claude tool loop - effort low\nrecall / doc / sql / simulate]
  L --> G{guardrails:\ncommitment breach, feasibility,\ncitations}
  G -- reject --> L
  G -- accept --> W[Claude rationale - effort high]
  W --> N[number check vs simulator/DB]
  N --> C[Decision card]
  C -- --writeback --> LD[log_decision: decisions_live /\ncommitments_live + Hindsight retain\n+ MOCK external actions]
  subgraph Data
    DB[(dataset SQLite - read-only\nas-of TEMP views)]
    LV[(live.sqlite - append-only)]
  end
  Q --- DB
  Q --- LV
  S --- DB
```

## Setup
(uv venv, requirements, .env from .env.example, `python -m agent.setup_data`, `python -m agent.check_connectivity`)

## Run
- Console: `python -m interface.decision_console --scenario HS05`, `--report "..." --as-of ... --runs ...`, `--question ... --as-of ...`, `--writeback`, `--trace`
- Demo: `python -m demo.run_demo` (see demo/EXPECTED.md, results in demo/out/demo_report.md)
- Eval: `python -m eval.run_holdout_scenarios`, `python -m eval.run_eval_questions [--per-type N | --all] [--judge]` → eval/out/scorecard.md
- Tests: `python -m pytest` (offline); `RUN_LIVE=1 python -m pytest -m live`

## How the hard rules are enforced
(one short paragraph each: numbers only from simulate.py, plus the prose number check; citations verified against seen docs and existing records;
as-of rule = TEMP views + DocIndex filter + reflect guard + future-citation counter in eval; external calls = MOCK strings;
config in .env)

## Results
(paste the headline table from eval/out/scorecard.md and the PASS/FAIL summary of demo/out/demo_report.md, with the date and model)

## Known limitations
- The simulator reproduces the generator's consequence model, including four risk rules copied from planted patterns
  P01/P02/P07/P08 (agent/sim_rules.yaml). That is by design for parity, but it means the simulator "knows" those
  patterns rather than learning them from memory.
- Parity quirk kept on purpose: when choosing a donor plant the generator compares risk labels as strings, so a
  "high"-risk donor can win over a "medium" one (agent/simulate.py `_best_transfer`).
- Only 6 of the 9 decision types are simulated. build_safety_stock, renegotiate and reduce_allocation show "not
  modelled", and the guardrail refuses to recommend them because no numbers can back them.
- The holdout harness passes each scenario's dependent run ids as MRP context. For free-text reports, the agent uses
  the run ids in the report (or --runs); if only the first run is known, the service impact is understated.
- As-of views mask receipts, outcomes, resolutions and negotiation results. Some aggregates are still computed with
  end-of-window knowledge: the scorecard's otif/fill for completed months, and cancelled-PO status.
- Native Hindsight reflect runs only when nothing in the bank can post-date as_of. Otherwise reflect is recall plus a
  local synthesis using the bank's mission/directives (reflect_mode on the card says which ran).
- Eval answer scoring is a deterministic key-fact match (ids, dates, numbers, status/action words, threshold 0.75).
  Use --judge for a semantic LLM check.
- Current Claude models reject `temperature`; "low vs normal temperature" is implemented as effort low/high.
- Write-back retains into the shared bank. Other runs ignore those facts unless they use the same live DB.
````

- [ ] **Step 2: Full offline test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests pass, and the `live` tests are skipped.

- [ ] **Step 3: Live test suite**

Run: `RUN_LIVE=1 .venv/bin/python -m pytest -m live -q`
Expected: 2 passed (`test_live_recall_is_resolvable`, `test_live_round_trip`). Report any failure verbatim.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: README with architecture, setup, run instructions, results and limitations

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Hand off**

Use superpowers:finishing-a-development-branch.
