"""Stage 0: prove the DB, the ingestion and the Hindsight bank are in place before building on them."""
from __future__ import annotations

import json
import sqlite3
import sys

from agent.config import Settings, load_settings
from agent.setup_data import ensure_db

KNOWN_QUERY = "Why did RPO003179 slip and what is its latest ETA?"
KNOWN_TOKEN = "RPO003179"  # DOC000248 / DOC000254 in the memory split
EXPECTED_MEMORY_DOCS = 2201  # split=memory rows in dataset/memory_corpus.jsonl


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


def check_ingestion(settings: Settings) -> str:
    path = settings.retention_log_path
    if not path.exists():
        raise RuntimeError(f"{path} not found - run the loader (Task 1B)")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    ok = {r["doc_id"] for r in rows if r.get("status") == "ok"}
    if len(ok) != EXPECTED_MEMORY_DOCS:
        raise RuntimeError(f"{len(ok)}/{EXPECTED_MEMORY_DOCS} memory docs retained ok - re-run the loader (it resumes)")
    return f"Ingestion ok: {len(ok)}/{EXPECTED_MEMORY_DOCS} memory docs retained"


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
