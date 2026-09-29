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
