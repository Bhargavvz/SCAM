"""Shared helpers: config, deterministic RNG streams, source loading, fingerprints, table IO."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent
SOURCE_TABLES = ["suppliers", "products", "warehouses", "inventory_opening_balances",
                 "purchase_orders", "purchase_order_lines", "inventory_movements"]
DATE_COLS = {
    "inventory_opening_balances": ["balance_date"],
    "purchase_orders": ["ordered_at", "expected_at", "received_at"],
    "inventory_movements": ["movement_at"],
}
ID_COLS = {"purchase_order_line_id", "transfer_id", "purchase_order_id", "movement_id", "product_id",
           "supplier_id", "warehouse_id", "opening_balance_id", "sku"}


# ------------------------------------------------------------------ context
@dataclass
class Ctx:
    input: Path
    output: Path
    seed: int
    scale: str
    cfg: dict
    _cache: dict = field(default_factory=dict)

    @property
    def work(self) -> Path:
        p = self.output / "_work"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def sc(self) -> dict:
        return self.cfg["scale"][self.scale]

    def rng(self, stream: str) -> np.random.Generator:
        """Independent, reproducible generator per named stream (stage/purpose)."""
        return np.random.default_rng([self.seed, zlib.crc32(stream.encode())])

    def src(self) -> dict[str, pd.DataFrame]:
        if "src" not in self._cache:
            self._cache["src"] = load_source(self.input)
        return self._cache["src"]

    def read(self, name: str) -> pd.DataFrame:
        if name not in self._cache:
            p = self.output / "tables" / "parquet" / f"{name}.parquet"
            if not p.exists():
                p = self.work / f"{name}.parquet"
            self._cache[name] = pd.read_parquet(p)
        return self._cache[name]

    def write(self, df: pd.DataFrame, name: str, work: bool = False) -> None:
        df = df.reset_index(drop=True)
        self._cache[name] = df
        if work:
            df.to_parquet(self.work / f"{name}.parquet", index=False)
            return
        (self.output / "tables" / "parquet").mkdir(parents=True, exist_ok=True)
        (self.output / "tables" / "csv").mkdir(parents=True, exist_ok=True)
        df.to_parquet(self.output / "tables" / "parquet" / f"{name}.parquet", index=False)
        out = df.copy()
        for c in out.columns:
            if pd.api.types.is_datetime64_any_dtype(out[c]):
                out[c] = out[c].dt.strftime("%Y-%m-%d")
        out.to_csv(self.output / "tables" / "csv" / f"{name}.csv", index=False)

    def save_json(self, obj, name: str, work: bool = True) -> Path:
        p = (self.work if work else self.output) / name
        p.write_text(json.dumps(obj, indent=2, default=_json_default), encoding="utf-8")
        return p

    def load_json(self, name: str, work: bool = True):
        p = (self.work if work else self.output) / name
        return json.loads(p.read_text(encoding="utf-8"))


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (pd.Timestamp, np.datetime64)):
        return str(pd.Timestamp(o).date())
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def load_config(path: Path | None = None) -> dict:
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def cli(description: str) -> Ctx:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--input", default=str(ROOT / "data" / "source"),
                    help="source dataset: directory of CSVs, the source CSV zip, or the v1.0.0 SQLite")
    ap.add_argument("--output", default=str(ROOT / "output"))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--scale", choices=["small", "full"], default="full")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    a, _ = ap.parse_known_args()
    return make_ctx(a.input, a.output, a.seed, a.scale, a.config)


def make_ctx(input, output, seed, scale, config=None) -> Ctx:
    cfg = load_config(Path(config) if config else None)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    return Ctx(Path(input), out, int(seed if seed is not None else cfg["seed"]), scale, cfg)


# ------------------------------------------------------------------ source loading (read-only)
def _resolve_source(path: Path) -> tuple[str, Path]:
    if path.is_file():
        return ("sqlite" if path.suffix in (".sqlite", ".db") else "zip"), path
    for pat, kind in (("*.sqlite", "sqlite"), ("csv/suppliers.csv", "dir"), ("suppliers.csv", "dir"),
                      ("*source-csv.zip", "zip")):
        hits = sorted(path.glob(pat))
        if hits:
            return kind, (hits[0].parent if kind == "dir" else hits[0])
    raise FileNotFoundError(f"No source dataset found under {path}")


def _typed(name: str, df: pd.DataFrame) -> pd.DataFrame:
    # the v1.0.0 SQLite stores missing values as '' - normalise to NA in memory (source untouched)
    for c in df.columns:
        if c in ID_COLS:
            df[c] = df[c].astype("string").replace("", pd.NA)
    for c in DATE_COLS.get(name, []):
        df[c] = pd.to_datetime(df[c].replace("", None))
    return df


def load_source(path: Path) -> dict[str, pd.DataFrame]:
    kind, p = _resolve_source(Path(path))
    out = {}
    if kind == "sqlite":
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        for t in SOURCE_TABLES:
            out[t] = _typed(t, pd.read_sql(f"SELECT * FROM {t}", con))
        con.close()
    elif kind == "zip":
        with zipfile.ZipFile(p) as z:
            names = {Path(n).stem: n for n in z.namelist() if n.endswith(".csv")}
            for t in SOURCE_TABLES:
                with z.open(names[t]) as f:
                    out[t] = _typed(t, pd.read_csv(f, dtype={c: "string" for c in ID_COLS}, keep_default_na=True))
    else:
        for t in SOURCE_TABLES:
            out[t] = _typed(t, pd.read_csv(p / f"{t}.csv", dtype={c: "string" for c in ID_COLS}))
    return out


def source_sqlite_path(path: Path) -> Path | None:
    kind, p = _resolve_source(Path(path))
    if kind == "sqlite":
        return p
    hits = sorted(Path(path).glob("*.sqlite")) if Path(path).is_dir() else []
    return hits[0] if hits else None


def table_fingerprint(df: pd.DataFrame) -> dict:
    """Order-independent content hash: canonical string of each row, sorted, then sha256."""
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].dt.strftime("%Y-%m-%d")
        elif pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].round(6)
    rows = d.astype("string").fillna("<NA>").agg("|".join, axis=1).sort_values()
    h = hashlib.sha256("\n".join(rows.tolist()).encode()).hexdigest()
    return {"rows": int(len(df)), "columns": list(df.columns), "sha256": h}


# ------------------------------------------------------------------ helpers
def fmt_ids(prefix: str, n: int, width: int, start: int = 1) -> list[str]:
    return [f"{prefix}{i:0{width}d}" for i in range(start, start + n)]


def id_pattern(series: pd.Series) -> dict:
    s = series.dropna().astype(str)
    m = s.str.extract(r"^([A-Za-z\-]*?)(\d+)$")
    return {"prefix": m[0].mode().iat[0] if m[0].notna().any() else "", "width": int(m[1].str.len().max()),
            "example": s.iat[0]}


def week_start(d: pd.Series) -> pd.Series:
    d = pd.to_datetime(d)
    return (d - pd.to_timedelta(d.dt.weekday, unit="D")).dt.normalize()


def cat_param(cfg: dict, key: str, cat: str):
    prof = cfg["category_profiles"]
    return prof.get(cat, prof["_default"])[key]


def banner(msg: str) -> None:
    print("\n" + "=" * 78 + f"\n{msg}\n" + "=" * 78, flush=True)
