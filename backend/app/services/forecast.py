"""Weekly demand forecasting.

* Regular series (total, category, warehouse): additive Holt-Winters with 52-week seasonality; smoothing parameters
  chosen by grid search on one-step-ahead error.
* Intermittent series (most single SKUs: many zero weeks): Syntetos-Boylan approximation of Croston's method.
Every forecast comes with a backtest over the last 13 complete weeks, compared with the planner forecast stored in the
dataset (product_demand_weekly.forecast_units), so accuracy claims are measured, not asserted.
"""
from __future__ import annotations

import itertools
from datetime import date, timedelta

import numpy as np

from app.db import TODAY, rows

SEASON = 52
BACKTEST = 13


def last_complete_week() -> date:
    t = date.fromisoformat(TODAY)
    monday = t - timedelta(days=t.weekday())
    return monday - timedelta(days=7) if (t - monday).days < 6 else monday


def series(con, level: str, key: str | None) -> tuple[list[str], np.ndarray, np.ndarray]:
    where, params = "week_start <= ?", [last_complete_week().isoformat()]
    join = ""
    if level == "category":
        join, where = "JOIN products p USING (product_id)", where + " AND p.category = ?"
        params.append(key)
    elif level == "product":
        where += " AND d.product_id = ?"
        params.append(key)
    elif level == "warehouse":
        where += " AND d.warehouse_id = ?"
        params.append(key)
    data = rows(con, f"""SELECT week_start, SUM(actual_demand_units) AS actual, SUM(forecast_units) AS planner
                         FROM product_demand_weekly d {join} WHERE {where} GROUP BY 1 ORDER BY 1""", params)
    # fill missing weeks with zeros (sparse SKUs)
    if not data:
        return [], np.array([]), np.array([])
    start, end = date.fromisoformat(data[0]["week_start"]), last_complete_week()
    by = {r["week_start"]: r for r in data}
    weeks, act, plan = [], [], []
    d = start
    while d <= end:
        k = d.isoformat()
        weeks.append(k)
        act.append(float(by[k]["actual"]) if k in by else 0.0)
        plan.append(float(by[k]["planner"]) if k in by else 0.0)
        d += timedelta(days=7)
    return weeks, np.array(act), np.array(plan)


def _hw_fit(y: np.ndarray, a: float, b: float, g: float, m: int = SEASON):
    level = y[:m].mean()
    trend = (y[m:2 * m].mean() - y[:m].mean()) / m if len(y) >= 2 * m else 0.0
    season = list(y[:m] - level)
    fitted = np.zeros(len(y))
    for t in range(len(y)):
        s = season[t % m] if t >= m else season[t]
        fitted[t] = level + trend + s
        if t < m:
            continue
        prev_level = level
        level = a * (y[t] - s) + (1 - a) * (level + trend)
        trend = b * (level - prev_level) + (1 - b) * trend
        season[t % m] = g * (y[t] - level) + (1 - g) * s
    return level, trend, season, fitted


def holt_winters(y: np.ndarray, h: int) -> tuple[np.ndarray, float, dict]:
    m = SEASON if len(y) >= 2 * SEASON else max(4, len(y) // 3)
    best = None
    for a, b, g in itertools.product((0.05, 0.15, 0.3, 0.5), (0.0, 0.02, 0.1), (0.05, 0.15, 0.3)):
        lv, tr, se, fit = _hw_fit(y, a, b, g, m)
        err = float(np.mean((y[m:] - fit[m:]) ** 2)) if len(y) > m else float("inf")
        if best is None or err < best[0]:
            best = (err, a, b, g, lv, tr, se)
    err, a, b, g, lv, tr, se = best
    n = len(y)
    fc = np.array([lv + (k + 1) * tr + se[(n + k) % m] for k in range(h)])
    return np.maximum(fc, 0), float(np.sqrt(err)), {"method": "Holt-Winters additive", "alpha": a, "beta": b, "gamma": g, "season": m}


def sba(y: np.ndarray, h: int, alpha: float = 0.1) -> tuple[np.ndarray, float, dict]:
    nz = np.nonzero(y)[0]
    if len(nz) == 0:
        return np.zeros(h), 0.0, {"method": "Syntetos-Boylan (no demand)"}
    z, p, q = y[nz[0]], float(nz[0] + 1), 1
    errs = []
    for t in range(nz[0] + 1, len(y)):
        est = (1 - alpha / 2) * z / p
        errs.append(y[t] - est)
        if y[t] > 0:
            z = alpha * y[t] + (1 - alpha) * z
            p = alpha * q + (1 - alpha) * p
            q = 1
        else:
            q += 1
    rate = (1 - alpha / 2) * z / p
    return np.full(h, rate), float(np.std(errs)) if errs else 0.0, {"method": "Syntetos-Boylan (intermittent)", "alpha": alpha}


def model(y: np.ndarray, h: int):
    zero_share = float(np.mean(y == 0)) if len(y) else 1.0
    return sba(y, h) if zero_share > 0.3 or len(y) < 2 * 8 else holt_winters(y, h)


def wape(actual: np.ndarray, pred: np.ndarray) -> float | None:
    s = float(np.sum(np.abs(actual)))
    return round(float(np.sum(np.abs(actual - pred))) / s, 4) if s > 0 else None


def forecast(con, level: str = "total", key: str | None = None, horizon: int = 12, history: int = 104) -> dict:
    weeks, y, planner = series(con, level, key)
    if len(y) == 0:
        return {"level": level, "key": key, "history": [], "forecast": [], "accuracy": None}
    fc, sigma, meta = model(y, horizon)
    # backtest: refit without the last 13 weeks and compare with what happened, and with the planner's forecast
    bt = None
    if len(y) > BACKTEST + 20:
        train, test = y[:-BACKTEST], y[-BACKTEST:]
        pred, _, _ = model(train, BACKTEST)
        bt = {"weeks": BACKTEST, "model_wape": wape(test, pred), "planner_wape": wape(test, planner[-BACKTEST:]),
              "model_bias": round(float(np.sum(pred - test) / max(np.sum(test), 1e-9)), 4),
              "planner_bias": round(float(np.sum(planner[-BACKTEST:] - test) / max(np.sum(test), 1e-9)), 4)}
    last = date.fromisoformat(weeks[-1])
    z = 1.28  # ~80% interval
    out = []
    for k in range(horizon):
        w = (last + timedelta(days=7 * (k + 1))).isoformat()
        band = z * sigma * np.sqrt(k + 1)
        out.append({"week": w, "forecast": round(float(fc[k]), 1), "low": round(max(0.0, float(fc[k] - band)), 1),
                    "high": round(float(fc[k] + band), 1)})
    hist = [{"week": weeks[i], "actual": float(y[i]), "planner": float(planner[i])} for i in range(max(0, len(y) - history), len(y))]
    return {"level": level, "key": key, "model": meta, "history": hist, "forecast": out, "accuracy": bt,
            "next_4w": round(float(np.sum(fc[:4])), 1), "last_4w": round(float(np.sum(y[-4:])), 1),
            "same_4w_last_year": round(float(np.sum(y[-52:-48])), 1) if len(y) >= 56 else None}
