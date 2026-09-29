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
