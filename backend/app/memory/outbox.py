"""Transactional memory outbox: the database and Hindsight stay in step.

1. Every audited change is written to `memory_outbox` inside the same SQLite transaction as the change itself
   (app.db.audit calls enqueue_event), so a committed change can never be missing from the queue.
2. A background worker sends queued documents to Hindsight with async retain_batch, stores the operation id, and polls
   the operations API until each one completes. Failures are retried with exponential backoff.
3. Dataset memories that never reached the bank (the original ingest hit gateway timeouts) can be backfilled through
   the same pipeline with their original timestamps, so the bank converges on the full corpus.
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.memory import service as mem

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_outbox (
  outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                 -- event (application change) | corpus (dataset memory backfill) | note
  source_id TEXT,                     -- audit_id or corpus doc id
  document_id TEXT UNIQUE NOT NULL,
  content TEXT NOT NULL,
  context TEXT,
  timestamp TEXT,
  tags TEXT,
  entities TEXT,
  metadata TEXT,
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | submitted | synced | failed | dead
  operation_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  submitted_at TEXT,
  synced_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_status ON memory_outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_outbox_op ON memory_outbox(operation_id);
CREATE TABLE IF NOT EXISTS memory_cache (key TEXT PRIMARY KEY, created_at TEXT, payload TEXT);
"""

ID_RE = re.compile(r"\b((?:IP|SUP|CUS|SO|RPO|PO|SH|RMA|PR|RM|CR|EVT|DEC|DECL|CMT|CMTL|NEG|CON)\d{2,}|W\d{3}|P0\d)\b")
KIND = {"IP": "product", "SUP": "supplier", "CUS": "customer", "SO": "sales_order", "RPO": "material_po", "PO": "purchase_order",
        "SH": "shipment", "RMA": "return", "PR": "production_run", "RM": "raw_material", "CR": "carrier", "EVT": "disruption",
        "DEC": "decision", "DECL": "decision", "CMT": "commitment", "CMTL": "commitment", "NEG": "negotiation", "CON": "contract",
        "W": "warehouse", "P": "plant"}
MAX_ATTEMPTS = 6

state = {"running": False, "last_cycle": None, "last_error": None, "submitted": 0, "synced": 0, "failed": 0, "cycles": 0}
_wake = threading.Event()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate(con) -> None:
    con.executescript(SCHEMA)
    con.commit()


def entity_kind(eid: str) -> str:
    m = re.match(r"([A-Z]+)\d", eid)
    return KIND.get(m.group(1), "record") if m else "record"


def ids_in(*texts) -> list[str]:
    seen: list[str] = []
    for t in texts:
        for m in ID_RE.findall(str(t or "")):
            if m not in seen:
                seen.append(m)
    return seen[:25]


def _details(payload) -> str:
    """Readable key facts from an action payload (lines, quantities, reasons) for the memory text."""
    if not payload:
        return ""
    try:
        p = json.loads(payload) if isinstance(payload, str) else payload
    except ValueError:
        return ""
    parts = []
    if isinstance(p, dict):
        for k, v in p.items():
            if k == "lines" and isinstance(v, list):
                parts.append("lines: " + "; ".join(", ".join(f"{a}={b}" for a, b in ln.items()) for ln in v[:12] if isinstance(ln, dict)))
            elif isinstance(v, (str, int, float)) and v not in ("", None):
                parts.append(f"{k.replace('_', ' ')}: {v}")
    elif isinstance(p, list):
        parts.append("; ".join(", ".join(f"{a}={b}" for a, b in x.items()) for x in p[:12] if isinstance(x, dict)))
    return " ".join(parts)[:1500]


def enqueue_event(con, *, audit_id: int, at: str, business_date: str, actor: str, module: str, action: str,
                  entity_type: str, entity_id: str, summary: str, payload) -> None:
    ids = ids_in(entity_id, summary, payload)
    details = _details(payload)
    content = (f"{business_date}: {summary}. " + (f"Details - {details}. " if details else "") +
               f"Recorded in Meridian by {actor} ({module} / {action}) on business date {business_date}.")
    tags = ["source:meridian", f"module:{module}", f"action:{action}", f"type:{entity_type}"] + [f"entity:{i}" for i in ids]
    con.execute("""INSERT OR IGNORE INTO memory_outbox (kind, source_id, document_id, content, context, timestamp, tags, entities,
                   metadata, created_at) VALUES ('event', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(audit_id), f"MER-{audit_id:07d}", content, f"Meridian {module}: {action.replace('_', ' ')}",
                 f"{business_date}T12:00:00+00:00", json.dumps(tags),
                 json.dumps([{"text": i, "type": entity_kind(i)} for i in ids]),
                 json.dumps({"audit_id": str(audit_id), "module": module, "action": action, "actor": actor,
                             "entity_type": entity_type, "entity_id": str(entity_id), "recorded_at": at}), _now()))
    _wake.set()


def enqueue_note(con, *, document_id: str, content: str, context: str, business_date: str, tags: list[str],
                 metadata: dict | None = None) -> None:
    ids = ids_in(content)
    con.execute("""INSERT OR IGNORE INTO memory_outbox (kind, source_id, document_id, content, context, timestamp, tags, entities,
                   metadata, created_at) VALUES ('note', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (document_id, document_id, content, context, f"{business_date}T12:00:00+00:00",
                 json.dumps(["source:meridian"] + tags + [f"entity:{i}" for i in ids]),
                 json.dumps([{"text": i, "type": entity_kind(i)} for i in ids]), json.dumps(metadata or {}), _now()))
    _wake.set()


def enqueue_corpus(con, docs: list[dict]) -> int:
    n = 0
    for d in docs:
        ids = list(dict.fromkeys((d.get("entity_ids") or []) + (d.get("source_record_ids") or [])))[:25]
        meta = {"doc_type": d.get("doc_type") or "", "source": "dataset corpus",
                "source_record_ids": ",".join(d.get("source_record_ids") or [])[:900]}
        cur = con.execute("""INSERT OR IGNORE INTO memory_outbox (kind, source_id, document_id, content, context, timestamp, tags,
                             entities, metadata, created_at) VALUES ('corpus', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (d["doc_id"], d["doc_id"], d["content"], d.get("context") or d.get("doc_type"), d.get("timestamp"),
                           json.dumps(["source:dataset", f"doc_type:{d.get('doc_type')}"] + [f"entity:{i}" for i in ids]),
                           json.dumps([{"text": i, "type": entity_kind(i)} for i in ids]), json.dumps(meta), _now()))
        n += cur.rowcount
    con.commit()
    _wake.set()
    return n


# ------------------------------------------------------------------ worker
def _item(r) -> dict:
    ts = r["timestamp"]
    item = {"content": r["content"], "context": r["context"], "document_id": r["document_id"],
            "tags": json.loads(r["tags"] or "[]"), "entities": json.loads(r["entities"] or "[]"),
            "metadata": {k: str(v) for k, v in json.loads(r["metadata"] or "{}").items()}}
    if ts:
        item["timestamp"] = ts
    return item


def _submit(con) -> int:
    rows = con.execute("""SELECT * FROM memory_outbox WHERE status = 'queued'
                            OR (status = 'failed' AND attempts < ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                          ORDER BY kind = 'corpus', outbox_id LIMIT ?""", (MAX_ATTEMPTS, _now(), settings.memory_batch_size)).fetchall()
    if not rows:
        return 0
    try:
        res = mem.retain_batch([_item(r) for r in rows])
    except mem.MemoryError_ as ex:
        for r in rows:
            _fail(con, r["outbox_id"], r["attempts"], str(ex))
        con.commit()
        raise
    op = res.get("operation_id")
    con.executemany("UPDATE memory_outbox SET status = 'submitted', operation_id = ?, submitted_at = ?, attempts = attempts + 1, last_error = NULL WHERE outbox_id = ?",
                    [(op, _now(), r["outbox_id"]) for r in rows])
    con.commit()
    state["submitted"] += len(rows)
    return len(rows)


def _fail(con, outbox_id: int, attempts: int, err: str) -> None:
    delay = min(3600, 30 * (2 ** attempts))
    status = "dead" if attempts + 1 >= MAX_ATTEMPTS else "failed"
    con.execute("UPDATE memory_outbox SET status = ?, last_error = ?, next_attempt_at = ?, attempts = MAX(attempts, ?) WHERE outbox_id = ?",
                (status, err[:500], (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds"),
                 attempts + 1, outbox_id))
    state["failed"] += 1


def _poll(con) -> int:
    ops = [r[0] for r in con.execute("""SELECT DISTINCT operation_id FROM memory_outbox WHERE status = 'submitted'
                                        AND operation_id IS NOT NULL ORDER BY submitted_at LIMIT 15""")]
    done = 0
    for op in ops:
        try:
            st = mem.operation(op)
        except mem.MemoryError_ as ex:
            if "404" in str(ex):
                con.execute("UPDATE memory_outbox SET status = 'failed', last_error = 'operation not found' WHERE operation_id = ?", (op,))
            continue
        s = st.get("status")
        if s == "completed":
            n = con.execute("UPDATE memory_outbox SET status = 'synced', synced_at = ? WHERE operation_id = ? AND status = 'submitted'",
                            (_now(), op)).rowcount
            state["synced"] += n
            done += n
        elif s in ("failed", "cancelled"):
            for r in con.execute("SELECT outbox_id, attempts FROM memory_outbox WHERE operation_id = ?", (op,)).fetchall():
                _fail(con, r[0], r[1], st.get("error_message") or s)
    con.commit()
    if done:
        mem.invalidate("stats")
    return done


def _loop() -> None:
    from app.db import connect
    state["running"] = True
    con = connect()
    migrate(con)
    while state["running"]:
        try:
            _submit(con)
            _poll(con)
            state["last_error"] = None
        except Exception as ex:  # keep the worker alive; the status page shows the last error
            state["last_error"] = f"{type(ex).__name__}: {str(ex)[:300]}"
            traceback.print_exc()
        state["last_cycle"] = _now()
        state["cycles"] += 1
        _wake.wait(timeout=8)
        _wake.clear()


def start() -> bool:
    if not (settings.memory_sync and mem.enabled()) or state["running"]:
        return False
    threading.Thread(target=_loop, name="memory-outbox", daemon=True).start()
    return True


def kick() -> None:
    _wake.set()


def summary(con) -> dict:
    by = {f"{r[0]}:{r[1]}": r[2] for r in con.execute("SELECT kind, status, COUNT(*) FROM memory_outbox GROUP BY 1, 2")}
    tot = {}
    for k, v in by.items():
        kind, status = k.split(":")
        tot.setdefault(kind, {})[status] = v
    return {"by_kind": tot, "worker": {k: v for k, v in state.items()}, "sync_enabled": settings.memory_sync and mem.enabled(),
            "lag": con.execute("""SELECT MIN(created_at) FROM memory_outbox WHERE status IN ('queued', 'submitted', 'failed')""").fetchone()[0]}
