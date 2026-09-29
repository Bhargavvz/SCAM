"""One-time setup: create the Hindsight Cloud bank (mission + directives) and retain a small corpus.

  python hindsight_setup.py --dry-run      # show what would be sent, no API calls
  python hindsight_setup.py                # create bank, retain ~300 docs chronologically, smoke-test recall
Reads HINDSIGHT_API_KEY / HINDSIGHT_BANK_ID / HINDSIGHT_BASE_URL from .env.
"""
from __future__ import annotations

import argparse
import json

from agent import BANK_ID, ROOT, client, log_usage
from test_scenarios import SCENARIOS

MISSION = ("I am the institutional memory for a supply-chain operations team. I prioritize evidence over inference and always "
           "distinguish what happened from what was decided.")
DIRECTIVES = [("cite-sources", "Always cite source memories."),
              ("flag-commitments", "Never recommend an action that breaches an open commitment without flagging it.")]


def select_corpus(n_signal=270, n_noise=30):
    """Small, relevant slice of the memory split: docs about the test suppliers/RMs first, then the rest by recency."""
    docs = [json.loads(l) for l in (ROOT / "output" / "memory_corpus.jsonl").read_text(encoding="utf-8").splitlines()]
    docs = [d for d in docs if d["split"] == "memory"]
    import re
    ids = set()
    for sc in SCENARIOS.values():
        ids |= set(re.findall(r"SUP\d{4}|RM\d{4}", sc["report"]))
    hs = json.loads((ROOT / "output" / "holdout_scenarios.json").read_text(encoding="utf-8"))
    ids |= {h["supplier_id"] for h in hs} | {h["rm_id"] for h in hs}
    prec = {p for h in hs for p in h["gold_precedent_event_ids"]}
    sig = [d for d in docs if not d["is_noise"]]
    rel = [d for d in sig if set(d["entity_ids"]) & ids or d.get("event_id") in prec]
    rest = [d for d in sig if d not in rel]
    chosen = rel[:n_signal] + rest[-max(0, n_signal - len(rel)):] if len(rel) < n_signal else rel[:n_signal]
    chosen += [d for d in docs if d["is_noise"]][::37][:n_noise]
    return sorted(chosen, key=lambda d: d["timestamp"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=20)
    a = ap.parse_args()
    docs = select_corpus()
    words = sum(len(d["content"].split()) for d in docs)
    print(f"bank '{BANK_ID}': {len(docs)} docs, {docs[0]['timestamp'][:10]}..{docs[-1]['timestamp'][:10]}, ~{words:,} words (~{int(words * 1.35):,} tokens)")
    if a.dry_run:
        print(json.dumps({k: docs[0][k] for k in ("doc_id", "timestamp", "context", "content")}, indent=2)[:700])
        return
    hs = client()
    hs.create_bank(bank_id=BANK_ID, reflect_mission=MISSION, background=MISSION)
    for name, text in DIRECTIVES:
        try:
            hs.create_directive(bank_id=BANK_ID, name=name, content=text, priority=10)
        except Exception as e:  # re-running setup: directive may already exist
            print(f"  directive {name}: {e.__class__.__name__} (already present?)")
    print("bank created with mission + directives")
    for i in range(0, len(docs), a.batch):
        b = docs[i:i + a.batch]
        items = [{"content": d["content"], "context": d["context"], "timestamp": d["timestamp"], "document_id": d["doc_id"],
                  "tags": [f"type:{d['doc_type']}"]} for d in b]
        r = hs.retain_batch(bank_id=BANK_ID, items=items)
        log_usage(f"retain_batch {i // a.batch + 1}", usage=r.usage, extra={"docs": len(b), "first_ts": b[0]["timestamp"]})
    # Stage 0 smoke test
    r = hs.recall(bank_id=BANK_ID, query="Supplier 118 SUP0118 year-end delays", budget="low", max_tokens=1000)
    print(f"smoke recall: {len(r.results or [])} results")
    for x in (r.results or [])[:3]:
        print("  -", x.text[:150])


if __name__ == "__main__":
    main()
