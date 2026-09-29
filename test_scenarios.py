"""Hand-picked scenarios (Q4-2025 - the holdout quarter, so none of these reports are in the memory bank).

Run:  python test_scenarios.py [--offline] [--only T3]
Output is compared by eye: expected (from the dataset's holdout oracle, restricted to our 3 actions) vs the agent.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent import ACTIONS, decide
from console import print_card

ROOT = Path(__file__).resolve().parent
HS = {h["scenario_id"]: h for h in json.loads((ROOT / "output" / "holdout_scenarios.json").read_text(encoding="utf-8"))}


def from_holdout(sid, note):
    h = HS[sid]
    gold = min([a for a in h["candidate_actions"] if a["action"] in ACTIONS], key=lambda a: a["est_cost"])["action"]
    return {"report": h["day0_report"], "as_of": h["as_of_date"], "expected": gold, "note": note}


SCENARIOS = {
    "T1": from_holdout("HS07", "SUP0118 year-end slip, plenty of cover -> waiting is cheapest; recall should surface prior Nov-Dec slips."),
    "T2": from_holdout("HS13", "Single-source RM0043 after the Nov-2025 price spike; low cover -> expedite."),
    "T3": {"report": ("Planner report 2025-11-19: Supplier 118 (SUP0118) now quotes 2025-11-30 for RMPO037977 (for P03) after "
                      "missing its date. Plant is running thin. Should we just move this order to another supplier?"),
           "as_of": "2025-11-19", "expected": "expedite",
           "note": "COMMITMENT CONFLICT: switching is cheapest on paper, but CMT01777 (paid expedite on this PO) is open -> must be flagged; "
                   "recommendation falls back to expedite."},
    "T4": from_holdout("HS14", "SUP0118 in late December: naive = switch, but open commitment on the PO blocks it."),
    "T5": from_holdout("HS09", "Trap in the holdout set: SUP0179 is reliable on small POs only; surface precedent misleads."),
    "T6": from_holdout("HS05", "Oracle says expedite; our expected-rate simulator may disagree (known limitation - note it)."),
    "T7": from_holdout("HS01", "Routine delay with ample stock -> accept delay."),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--only", default=None)
    a = ap.parse_args()
    rows = []
    for k, sc in SCENARIOS.items():
        if a.only and k != a.only:
            continue
        print(f"\n\n##### {k}: {sc['note']}")
        c = decide(sc["report"], sc["as_of"], use_memory=not a.offline)
        print_card(c, trace=True)
        got = c["recommendation"]["action"]
        flagged = [x["commitment_id"] for x in c["conflicts"]]
        rows.append((k, sc["expected"], got, c["naive"]["action"], flagged, len(c["memories"])))
    print("\n\nSUMMARY  (expected = holdout oracle best among the 3 actions)")
    print(f"{'id':<4}{'expected':<16}{'agent':<16}{'naive':<16}{'flagged':<26}{'memories':>8}")
    for k, e, g, n, f, m in rows:
        print(f"{k:<4}{e:<16}{g:<16}{n:<16}{','.join(f) or '-':<26}{m:>8}  {'OK' if e == g else 'DIFF'}")


if __name__ == "__main__":
    main()
