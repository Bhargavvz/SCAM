"""CLI: paste a supplier-delay report, get a decision card.

  python console.py "Supplier 118 says RMPO037181 now arrives 2025-12-05 ..." --as-of 2025-11-16
  python console.py --offline ...     # skip Hindsight (DB + simulator only)
  echo "report..." | python console.py --as-of 2025-11-16
"""
from __future__ import annotations

import argparse
import sys
import textwrap

from agent import decide

W = 100


def wrap(t, indent="  "):
    return textwrap.fill(t, W, initial_indent=indent, subsequent_indent=indent)


def print_card(c: dict, trace: bool = False):
    s, ctx = c["situation"], c["ctx"]
    print("=" * W + "\nDECISION CARD\n" + "=" * W)
    print("SITUATION")
    print(wrap(f"{s.po_id}: {s.qty:,.1f} of {s.rm_id} from {s.supplier_id} to {s.plant_id}; promised {s.promised_at}, now ETA {s.eta} "
               f"(+{s.slip_days}d). As of {s.as_of}: on hand {ctx['on_hand']:,.1f}, usage {ctx['daily_usage']:,.2f}/day, cover {ctx['cover_days']:.1f} days."))
    sc = ctx["scorecard"]
    if len(sc):
        print(wrap(f"{s.supplier_id} scorecard {sc.month.iat[-1]}..{sc.month.iat[0]}: OTIF {sc.otif_rate.mean():.0%}, avg delay {sc.avg_delay_days.mean():.1f}d "
                   f"[supplier_scorecard_monthly]"))
    print(f"\nDB QUERIES (allowlisted; {c['plan_note']})")
    for n, df in c["db_rows"].items():
        print(f"  {n}: {len(df)} row(s)")
    print(f"  retained to memory ({len(c['retained_rows'])} rows):")
    for r in c["retained_rows"]:
        print(wrap(r[:220], "    "))
    print("\nPRECEDENTS (Hindsight recall)")
    if not c["memories"]:
        print("  (none - offline or nothing recalled)")
    for m in c["memories"][:5]:
        print(wrap(f"- [{m['when']}] {m['text'][:180]}  (memory {m['id']}, doc {m['document_id']})"))
    if c["reflect"]:
        print("\nHINDSIGHT REFLECT (LLM synthesis - narrative only, no numbers used from it)")
        print(wrap(c["reflect"][:900]))
        print(wrap(f"based on {len(c['reflect_facts'])} memories: " + ", ".join(str(f['id']) for f in c["reflect_facts"][:6])
                   if c["reflect_facts"] else "based_on: none returned - treat the synthesis as unverified"))
    print("\nOPTIONS (deterministic simulator)")
    print(f"  {'action':<16}{'arrival':<12}{'stockout d':>11}{'direct':>12}{'shortage':>12}{'total':>12}  note")
    for x in c["sims"]:
        if not x.get("feasible"):
            print(f"  {x['action']:<16}{'-':<12}{'-':>11}{'-':>12}{'-':>12}{'-':>12}  infeasible: {x['note']}")
            continue
        note = f"BLOCKED by {', '.join(x['blocked_by'])} (+penalty {x['commitment_penalty']:,.0f})" if x.get("blocked_by") else ""
        print(f"  {x['action']:<16}{x['arrival']:<12}{x['stockout_days']:>11}{x['direct_cost']:>12,.0f}{x['shortage_cost']:>12,.0f}{x['total_cost']:>12,.0f}  {note}")
        if trace:
            print(wrap(f"formula: {x['formula']}", "      "))
    print("\nCOMMITMENTS")
    oc = ctx["open_commitments"]
    print(f"  {len(oc)} open with {s.supplier_id} as of {s.as_of}" + (": " + ", ".join(oc.commitment_id) if len(oc) else ""))
    for k in c["conflicts"]:
        print(wrap(f"! {k['commitment_id']}: {k['text']} - {k['detail']}"))
    print("\nRECOMMENDATION")
    print(wrap(c["rationale"]))
    print(wrap(f"evidence records: {', '.join(x for x in dict.fromkeys(s.evidence) if x)}"))
    print("  (external actions are mocked - no emails or ERP changes were made)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report", nargs="?")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-retain", action="store_true", help="do not retain the query + DB rows into the bank")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()
    report = a.report or sys.stdin.read()
    print_card(decide(report, a.as_of, use_memory=not a.offline, retain=not a.no_retain), trace=a.trace)


if __name__ == "__main__":
    main()
