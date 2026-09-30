"""Connections, query helpers, id sequences, audited writes and stock postings."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import HTTPException

from app.config import settings

TODAY = settings.today
_write_lock = threading.Lock()  # SQLite has one writer; serialise application transactions


def connect() -> sqlite3.Connection:
    if not settings.db_path.exists():
        raise RuntimeError(f"{settings.db_path} missing - run: python -m app.build_db")
    con = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def get_db():
    """FastAPI dependency: one connection per request."""
    con = connect()
    try:
        yield con
    finally:
        con.close()


@contextmanager
def tx(con: sqlite3.Connection):
    """Serialised write transaction."""
    with _write_lock:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------- reads
def rows(con, sql: str, params: Iterable[Any] | dict = ()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def one(con, sql: str, params: Iterable[Any] | dict = ()) -> dict | None:
    r = con.execute(sql, params).fetchone()
    return dict(r) if r else None


def scalar(con, sql: str, params: Iterable[Any] | dict = ()):
    r = con.execute(sql, params).fetchone()
    return r[0] if r else None


def must(row: dict | None, what: str) -> dict:
    if row is None:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return row


SAFE_SORT = re.compile(r"^[a-z_][a-z0-9_]*$")


def page(con, select_sql: str, where: list[str], params: list, *, sort: str | None, allowed: set[str],
         default_sort: str, limit: int, offset: int) -> dict:
    """Paginated listing. `select_sql` is 'SELECT ... FROM ...' without WHERE / ORDER BY / LIMIT."""
    w = f" WHERE {' AND '.join(where)}" if where else ""
    order = default_sort
    if sort:
        key, _, direction = sort.partition(":")
        if key in allowed and SAFE_SORT.match(key):
            order = f"{key} {'DESC' if direction == 'desc' else 'ASC'}"
    total = scalar(con, f"SELECT COUNT(*) FROM ({select_sql}{w})", params)
    data = rows(con, f"SELECT * FROM ({select_sql}{w}) ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset])
    return {"total": total, "rows": data, "limit": limit, "offset": offset}


def like(q: str | None) -> str:
    return f"%{(q or '').strip()}%"


# ---------------------------------------------------------------- dates
def today() -> date:
    return date.fromisoformat(TODAY)


def shift(days: int, base: str | None = None) -> str:
    return (date.fromisoformat(base or TODAY) + timedelta(days=days)).isoformat()


def last_complete_month() -> str:
    """First day of the last month that ended on or before the business date (scorecards are monthly)."""
    t = today()
    first = t.replace(day=1)
    if (t + timedelta(days=1)).month != t.month:  # business date is a month end: that month is complete
        return first.isoformat()
    return (first - timedelta(days=1)).replace(day=1).isoformat()


def months_back(n: int, base: str | None = None) -> str:
    d = date.fromisoformat(base or last_complete_month())
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    return date(y, m + 1, 1).isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- writes
def next_id(con, table: str, column: str, prefix: str, width: int) -> str:
    n = scalar(con, f"SELECT MAX(CAST(SUBSTR({column}, ?) AS INTEGER)) FROM {table} WHERE {column} LIKE ?",
               (len(prefix) + 1, f"{prefix}%")) or 0
    return f"{prefix}{int(n) + 1:0{width}d}"


def created(con, table: str, record_id: str) -> None:
    con.execute("INSERT OR IGNORE INTO app_records VALUES (?, ?, ?)", (table, record_id, now_iso()))


def audit(con, *, module: str, action: str, entity_type: str, entity_id: str, summary: str,
          payload: Any = None, actor: str = "planner") -> None:
    con.execute("""INSERT INTO audit_log (at, business_date, actor, module, action, entity_type, entity_id, summary, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now_iso(), TODAY, actor, module, action, entity_type, entity_id, summary,
                 json.dumps(payload, default=str) if payload is not None else None))


def post_movement(con, *, product_id: str, warehouse_id: str, qty: int, movement_type: str,
                  po_line_id: str | None = None, transfer_id: str | None = None) -> str:
    """Append to the inventory ledger and keep stock_levels current. Returns the movement id."""
    if qty == 0:
        raise HTTPException(status_code=400, detail="quantity must not be zero")
    if qty < 0:
        have = scalar(con, "SELECT on_hand FROM stock_levels WHERE product_id = ? AND warehouse_id = ?",
                      (product_id, warehouse_id)) or 0
        if have + qty < 0:
            raise HTTPException(status_code=409, detail=f"only {have} units of {product_id} on hand at {warehouse_id}")
    mid = next_id(con, "inventory_movements", "movement_id", "M", 8)
    con.execute("""INSERT INTO inventory_movements (movement_id, product_id, warehouse_id, movement_at, movement_type,
                   quantity_change, purchase_order_line_id, transfer_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (mid, product_id, warehouse_id, TODAY, movement_type, qty, po_line_id, transfer_id))
    created(con, "inventory_movements", mid)
    cur = con.execute("UPDATE stock_levels SET on_hand = on_hand + ? WHERE product_id = ? AND warehouse_id = ?",
                      (qty, product_id, warehouse_id))
    if cur.rowcount == 0:
        con.execute("INSERT INTO stock_levels (product_id, warehouse_id, on_hand, allocated) VALUES (?, ?, ?, 0)",
                    (product_id, warehouse_id, qty))
    return mid


def change_allocation(con, product_id: str, warehouse_id: str, delta: int) -> None:
    con.execute("UPDATE stock_levels SET allocated = MAX(0, allocated + ?) WHERE product_id = ? AND warehouse_id = ?",
                (delta, product_id, warehouse_id))


def available(con, product_id: str, warehouse_id: str) -> int:
    r = one(con, "SELECT on_hand, allocated FROM stock_levels WHERE product_id = ? AND warehouse_id = ?",
            (product_id, warehouse_id))
    return int(r["on_hand"] - r["allocated"]) if r else 0


def to_csv(data: list[dict]) -> str:
    import csv
    import io

    if not data:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(data[0].keys()))
    w.writeheader()
    w.writerows(data)
    return buf.getvalue()
