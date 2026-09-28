"""Deterministic cost / outcome model shared by Stage 3 (decision outcomes) and
Stage 4 (holdout-scenario consequence simulation). Nothing here is sampled:
costs are functions of quantities, prices, lanes and dates in the data.
"""
from __future__ import annotations

import math


def block_cost_per_day(fg_value: float, cfg: dict) -> float:
    """Cost of one day of delay for blocked FG output worth `fg_value` (sales value)."""
    return fg_value * cfg["memory"]["stockout_cost_rate_per_day"]


def rm_expedite_premium_pct(lead_days: int, congested: bool) -> float:
    """Air-freight premium over RM value: longer lanes cost more; congestion adds a surcharge."""
    return round(0.18 + 0.10 * min(lead_days, 60) / 60 + (0.08 if congested else 0.0), 4)


def rm_expedite_days(lead_days: int, congested: bool) -> int:
    """Door-to-door days for an air expedite (customs + trucking); congestion adds clearance time."""
    return int(3 + math.ceil(lead_days / 30) + (4 if congested else 0))


def spot_lead_days(lead_days: int) -> int:
    return max(3, int(round(0.4 * lead_days)))


def transfer_days(dist_units: float) -> int:
    return int(1 + math.ceil(dist_units))


def fg_expedite_cost(line_value: float, warehouse_multiplier: float, cfg: dict) -> float:
    return round(line_value * cfg["memory"]["expedite_cost_rate"] * warehouse_multiplier, 2)


def holding_cost(units_value: float, days: int, cfg: dict) -> float:
    return round(units_value * cfg["memory"]["holding_cost_rate_per_year"] * days / 365, 2)


def outcome_label(exp_cost: float, act_cost: float, exp_days: float, act_days: float) -> str:
    """success: no worse than planned (+0.5 day, +25% cost); failed: >3 days or >2x cost worse."""
    if act_days > exp_days + 3 or act_cost > 2.0 * max(exp_cost, 1.0):
        return "failed"
    if act_days <= exp_days + 0.5 and act_cost <= 1.25 * max(exp_cost, 1.0):
        return "success"
    return "partial"


def total_score(cost: float, days: float, daily: float, risk: str) -> float:
    """Ex-ante utility used to rank options (lower is better)."""
    rp = {"low": 1.0, "medium": 1.15, "high": 1.4}.get(risk, 1.0)
    return (cost + days * daily) * rp
