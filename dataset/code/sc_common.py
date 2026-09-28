"""Shared utilities for the supply-chain dataset extension.

Everything that more than one stage needs lives here: config loading,
deterministic RNG streams, source loading (CSV dir / ZIP / SQLite), hashing,
table writing (CSV + optional Parquet), and the pipeline context that stages
use to hand intermediate artifacts to each other.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import pickle
import re
import sqlite3
import sys
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

SOURCE_TABLES = [
    "suppliers",
    "products",
    "warehouses",
    "inventory_opening_balances",
    "purchase_orders",
    "purchase_order_lines",
    "inventory_movements",
]

SOURCE_PK = {
    "suppliers": ["supplier_id"],
    "products": ["product_id"],
    "warehouses": ["warehouse_id"],
    "inventory_opening_balances": ["opening_balance_id"],
    "purchase_orders": ["purchase_order_id"],
    "purchase_order_lines": ["purchase_order_line_id"],
    "inventory_movements": ["movement_id"],
}

SOURCE_NUMERIC = {
    "suppliers": {"lead_time_days": "int", "reliability_score": "float"},
    "products": {"unit_cost": "float", "reorder_point": "int"},
    "warehouses": {"capacity_units": "int"},
    "inventory_opening_balances": {"opening_units": "int"},
    "purchase_order_lines": {"quantity_ordered": "int", "quantity_received": "int", "unit_cost": "float"},
    "inventory_movements": {"quantity_change": "int"},
}
SOURCE_DATES = {
    "inventory_opening_balances": ["balance_date"],
    "purchase_orders": ["ordered_at", "expected_at", "received_at"],
    "inventory_movements": ["movement_at"],
}

HERE = Path(__file__).resolve().parent


# ----------------------------------------------------------------------------
# logging
# ----------------------------------------------------------------------------
_T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - _T0:7.1f}s] {msg}", flush=True)


def banner(msg: str) -> None:
    line = "=" * 78
    print(f"\n{line}\n{msg}\n{line}", flush=True)


# ----------------------------------------------------------------------------
# config + rng
# ----------------------------------------------------------------------------
def load_config(path: str | os.PathLike | None = None, scale: str = "default", seed: int | None = None) -> dict:
    path = Path(path) if path else HERE / "config.yaml"
    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    if scale not in cfg["scales"]:
        raise ValueError(f"unknown scale {scale!r}; choose from {list(cfg['scales'])}")
    cfg["scale_name"] = scale
    cfg["scale"] = copy.deepcopy(cfg["scales"][scale])
    if seed is not None:
        cfg["seed"] = int(seed)
    return cfg


def stable_hash(*parts: Any) -> int:
    s = "|".join(str(p) for p in parts)
    return zlib.crc32(s.encode("utf-8")) & 0xFFFFFFFF


def rng_for(seed: int, *name: Any) -> np.random.Generator:
    """Independent, reproducible RNG stream per (seed, name...)."""
    return np.random.default_rng([int(seed), stable_hash(*name)])


# ----------------------------------------------------------------------------
# source loading
# ----------------------------------------------------------------------------
def _read_csv_bytes(b: bytes) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(b), dtype=str, keep_default_na=False, na_values=[])


def load_source_raw(input_path: str | os.PathLike) -> tuple[dict[str, pd.DataFrame], dict]:
    """Load the 7 source tables as *strings* (exactly as stored).

    Supports: a directory containing the CSVs (searched recursively), a ZIP of
    CSVs, or a SQLite database. Returns (tables, provenance)."""
    p = Path(input_path)
    tables: dict[str, pd.DataFrame] = {}
    prov: dict[str, Any] = {"input": str(p), "files": {}}
    if p.is_dir():
        for t in SOURCE_TABLES:
            hits = sorted(p.rglob(f"{t}.csv"))
            if not hits:
                raise FileNotFoundError(f"{t}.csv not found under {p}")
            b = hits[0].read_bytes()
            tables[t] = _read_csv_bytes(b)
            prov["files"][t] = {"path": str(hits[0]), "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
        prov["kind"] = "csv_dir"
    elif p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            for t in SOURCE_TABLES:
                hit = [n for n in names if n.split("/")[-1] == f"{t}.csv"]
                if not hit:
                    raise FileNotFoundError(f"{t}.csv not in {p}")
                b = z.read(hit[0])
                tables[t] = _read_csv_bytes(b)
                prov["files"][t] = {"path": f"{p}!{hit[0]}", "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
        prov["kind"] = "zip"
        prov["zip_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    elif p.suffix.lower() in (".sqlite", ".db", ".sqlite3"):
        with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as con:
            for t in SOURCE_TABLES:
                df = pd.read_sql_query(f"SELECT * FROM {t}", con)
                tables[t] = df.astype(str).replace({"None": "", "nan": ""})
        prov["kind"] = "sqlite"
        prov["sqlite_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    else:
        raise ValueError(f"unsupported input {p}")
    return tables, prov


def typed_source(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Typed copies of the source tables for analysis (never written back)."""
    out = {}
    for t, df in raw.items():
        d = df.copy()
        for c, kind in SOURCE_NUMERIC.get(t, {}).items():
            d[c] = pd.to_numeric(d[c]).astype("int64" if kind == "int" else "float64")
        for c in SOURCE_DATES.get(t, []):
            d[c] = pd.to_datetime(d[c].replace("", None))
        for c in ("purchase_order_line_id", "transfer_id"):
            if c in d.columns and t == "inventory_movements":
                d[c] = d[c].mask(d[c] == "")
        out[t] = d
    return out


def table_content_hash(df: pd.DataFrame, pk: list[str]) -> str:
    """Order-independent content hash of a string table (sorted by PK)."""
    d = df.astype(str).replace({"None": "", "nan": "", "NaT": ""})
    d = d.sort_values(pk).reset_index(drop=True)
    buf = d.to_csv(index=False).encode("utf-8")
    return hashlib.sha256(buf).hexdigest()


def source_fingerprint(raw: dict[str, pd.DataFrame]) -> dict:
    return {t: {"rows": int(len(df)), "cols": list(df.columns), "sha256": table_content_hash(df, SOURCE_PK[t])} for t, df in raw.items()}


# ----------------------------------------------------------------------------
# ID helpers
# ----------------------------------------------------------------------------
_ID_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def id_pattern(values: pd.Series) -> dict:
    v = values.dropna().astype(str)
    m = v.str.extract(r"^([A-Za-z]+)(\d+)$")
    if m[0].isna().any():
        return {"prefix": None, "width": None, "example": v.iloc[0]}
    prefixes = m[0].unique().tolist()
    widths = m[1].str.len().unique().tolist()
    return {"prefix": prefixes[0] if len(prefixes) == 1 else prefixes, "width": int(max(widths)), "example": v.iloc[0]}


def make_ids(prefix: str, n: int, width: int, start: int = 1) -> list[str]:
    return [f"{prefix}{i:0{width}d}" for i in range(start, start + n)]


def id_num(s: str) -> int:
    m = _ID_RE.match(str(s))
    return int(m.group(2)) if m else stable_hash(s)


# ----------------------------------------------------------------------------
# date helpers
# ----------------------------------------------------------------------------
def week_start(d) -> pd.Series:
    """Monday of the ISO week for each date."""
    d = pd.to_datetime(pd.Series(d))
    return (d - pd.to_timedelta(d.dt.dayofweek, unit="D")).dt.normalize()


def monday_on_or_before(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts).normalize()
    return ts - pd.Timedelta(days=ts.dayofweek)


def business_ts(date: pd.Timestamp | str, rng: np.random.Generator, cfg: dict, hour_bias: float | None = None) -> str:
    """ISO-8601 timestamp during business hours on the given date."""
    d = pd.Timestamp(date)
    h0, h1 = cfg["calendar"]["business_hours"]
    if hour_bias is None:
        minute_of_day = int(rng.integers(h0 * 60, h1 * 60))
    else:
        minute_of_day = int(np.clip(h0 * 60 + hour_bias * (h1 - h0) * 60 + rng.integers(-40, 40), h0 * 60, h1 * 60 - 1))
    hh, mm = divmod(minute_of_day, 60)
    return f"{d:%Y-%m-%d}T{hh:02d}:{mm:02d}:00{cfg['calendar']['hq_utc_offset']}"


def to_date_str(x) -> str | None:
    if x is None or (isinstance(x, float) and np.isnan(x)) or x is pd.NaT:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return pd.Timestamp(x).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# output
# ----------------------------------------------------------------------------
def parquet_engine() -> str | None:
    for eng in ("pyarrow", "fastparquet"):
        try:
            __import__(eng)
            return eng
        except ImportError:
            continue
    return None


def _to_output_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
    return out


def write_table(ctx: "Ctx", name: str, df: pd.DataFrame) -> None:
    """Write a NEW table as CSV (+ Parquet when an engine is available)."""
    from schema import SCHEMA  # local import to avoid a cycle

    if name in SOURCE_TABLES:
        raise RuntimeError(f"refusing to write source table {name}")
    if name in SCHEMA:
        cols = [c[0] for c in SCHEMA[name]["columns"]]
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise RuntimeError(f"{name}: missing columns {missing}")
        df = df[cols]
    out = _to_output_frame(df)
    tdir = ctx.out / "tables"
    tdir.mkdir(parents=True, exist_ok=True)
    out.to_csv(tdir / f"{name}.csv", index=False)
    eng = ctx.parquet
    if eng:
        pdir = ctx.out / "tables" / "parquet"
        pdir.mkdir(parents=True, exist_ok=True)
        out.to_parquet(pdir / f"{name}.parquet", index=False, engine=eng)
    ctx.tables[name] = df
    log(f"  wrote {name:34s} {len(df):>9,d} rows")


def read_typed(ctx: "Ctx", name: str) -> pd.DataFrame:
    """New table with date columns parsed (per schema)."""
    from schema import SCHEMA
    df = read_new_table(ctx, name).copy()
    for col, typ, *_ in SCHEMA[name]["columns"]:
        if typ == "date" and col in df and not pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = pd.to_datetime(df[col])
    return df


def read_new_table(ctx: "Ctx", name: str) -> pd.DataFrame:
    if name in ctx.tables:
        return ctx.tables[name]
    p = ctx.out / "tables" / f"{name}.csv"
    df = pd.read_csv(p, keep_default_na=True)
    ctx.tables[name] = df
    return df


# ----------------------------------------------------------------------------
# pipeline context
# ----------------------------------------------------------------------------
@dataclass
class Ctx:
    cfg: dict
    input: Path
    out: Path
    seed: int
    raw: dict = field(default_factory=dict)          # source tables as strings
    src: dict = field(default_factory=dict)          # typed source tables
    prov: dict = field(default_factory=dict)
    fingerprint: dict = field(default_factory=dict)
    calib: dict = field(default_factory=dict)
    tables: dict = field(default_factory=dict)       # new tables (in-memory cache)
    internal: dict = field(default_factory=dict)     # stage-to-stage artifacts
    parquet: str | None = None

    def rng(self, *name) -> np.random.Generator:
        return rng_for(self.seed, *name)

    # --- persistence of internal state between stages -------------------
    @property
    def internal_dir(self) -> Path:
        d = self.out / "_internal"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_internal(self, key: str, obj: Any) -> None:
        self.internal[key] = obj
        with open(self.internal_dir / f"{key}.pkl", "wb") as fh:
            pickle.dump(obj, fh, protocol=pickle.HIGHEST_PROTOCOL)

    def load_internal(self, key: str) -> Any:
        if key not in self.internal:
            with open(self.internal_dir / f"{key}.pkl", "rb") as fh:
                self.internal[key] = pickle.load(fh)
        return self.internal[key]

    def table(self, name: str) -> pd.DataFrame:
        return read_new_table(self, name)


def build_ctx(input_path: str, output: str, cfg: dict) -> Ctx:
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    raw, prov = load_source_raw(input_path)
    ctx = Ctx(cfg=cfg, input=Path(input_path), out=out, seed=int(cfg["seed"]), raw=raw, prov=prov)
    ctx.src = typed_source(raw)
    ctx.fingerprint = source_fingerprint(raw)
    want = cfg["output"]["write_parquet"]
    eng = parquet_engine()
    if want is True and not eng:
        raise RuntimeError("write_parquet: true but neither pyarrow nor fastparquet is installed")
    ctx.parquet = eng if want in ("auto", True) else None
    calib_path = out / "calibration.json"
    if calib_path.exists():
        ctx.calib = json.loads(calib_path.read_text())
    return ctx


def dump_json(obj: Any, path: Path) -> None:
    def default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return None if np.isnan(o) else float(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        if isinstance(o, (pd.Timestamp,)):
            return o.strftime("%Y-%m-%d")
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, set):
            return sorted(o)
        raise TypeError(type(o))

    path.write_text(json.dumps(obj, indent=2, default=default, ensure_ascii=False))


def js(obj: Any) -> str:
    """Compact JSON for *_json columns."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=lambda o: o.item() if hasattr(o, "item") else str(o))
