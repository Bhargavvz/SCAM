"""One-time setup of the Hindsight Cloud bank: conversations/discussions + query playbooks only (never DB records).

  python hindsight_setup.py --dry-run      # show what would be sent, no API calls
  python hindsight_setup.py                # create bank (mission + directives), retain playbooks + discussions, smoke-test recall
Reads HINDSIGHT_API_KEY / HINDSIGHT_BANK_ID / HINDSIGHT_BASE_URL from .env.
"""
from __future__ import annotations

import argparse
import json
import re

from agent import BANK_ID, ROOT, client, log_usage
from queries import QUERIES

MISSION = ("I am the institutional memory for a supply-chain operations team. I prioritize evidence over inference and always "
           "distinguish what happened from what was decided.")
DIRECTIVES = [("cite-sources", "Always cite source memories."),
              ("flag-commitments", "Never recommend an action that breaches an open commitment without flagging it.")]

# Only human conversations / discussions go to memory. Record-like notes (ETA/price/status updates, PO exception
# notices, commitment confirmations, receiving notes) duplicate DB rows and are answered from the DB instead.
CONVERSATION_TYPES = {"supplier_call_summary", "shift_handover", "escalation_email", "decision_memo", "post_mortem",
                      "supplier_qbr_notes", "forecast_review_minutes"}

PLAYBOOKS = [
    ("Supplier delay on a raw-material PO", ["po_status", "stock_cover", "open_commitments", "supplier_scorecard"],
     "When a supplier says a raw-material PO will be late, first confirm the PO and its latest ETA, then how many days of cover the plant has, "
     "then what we have already promised or agreed with that supplier, and how the supplier has performed recently."),
    ("Before switching supplier or cancelling a PO", ["open_commitments", "alternate_sources", "po_status"],
     "Never move volume away from a supplier without checking open commitments with them (paid expedites, confirmed dates, contract "
     "minimum volumes) and which alternate sources are qualified and how fast they can deliver."),
    ("Repeat offender or year-end slips", ["supplier_recent_slips", "past_decisions_supplier", "supplier_scorecard"],
     "If the supplier has slipped before, look at its recent ETA revisions and what we decided last time and how it worked out. "
     "First revised ETAs in Q4 have often been optimistic."),
    ("Deciding whether to expedite", ["past_decisions_supplier", "stock_cover", "po_status"],
     "Expedite only if cover runs out before the revised ETA; check how past expedites with this supplier turned out."),
]


def playbook_items():
    out = []
    for i, (title, names, text) in enumerate(PLAYBOOKS, 1):
        checks = "; ".join(f"{n} ({QUERIES[n]['desc']})" for n in names)
        out.append({"content": f"Team playbook - {title}. {text} Data checks to run: {checks}.",
                    "context": f"team playbook: {title}", "timestamp": "2022-12-01T09:00:00Z",
                    "document_id": f"playbook-{i}", "tags": ["playbook"]})
    return out


def select_corpus(max_docs=180, n_noise=15):
    """Conversation-type notes, prioritising the test suppliers/materials and the holdout precedents."""
    docs = [json.loads(l) for l in (ROOT / "output" / "memory_corpus.jsonl").read_text(encoding="utf-8").splitlines()]
    docs = [d for d in docs if d["split"] == "memory" and d["doc_type"] in CONVERSATION_TYPES]
    from test_scenarios import SCENARIOS
    ids = set()
    for sc in SCENARIOS.values():
        ids |= set(re.findall(r"SUP\d{4}|RM\d{4}", sc["report"]))
    hs = json.loads((ROOT / "output" / "holdout_scenarios.json").read_text(encoding="utf-8"))
    ids |= {h["supplier_id"] for h in hs} | {h["rm_id"] for h in hs}
    prec = {p for h in hs for p in h["gold_precedent_event_ids"]}
    sig = [d for d in docs if not d["is_noise"]]
    rel = [d for d in sig if set(d["entity_ids"]) & ids or d.get("event_id") in prec]
    rest = [d for d in sig if d not in rel]
    newest = lambda xs: sorted(xs, key=lambda d: d["timestamp"], reverse=True)
    chosen = (newest(rel) + newest(rest))[:max_docs - n_noise] + [d for d in docs if d["is_noise"]][::25][:n_noise]
    return sorted(chosen, key=lambda d: d["timestamp"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=20)
    a = ap.parse_args()
    docs = select_corpus()
    pb = playbook_items()
    words = sum(len(d["content"].split()) for d in docs) + sum(len(p["content"].split()) for p in pb)
    types = {}
    for d in docs:
        types[d["doc_type"]] = types.get(d["doc_type"], 0) + 1
    print(f"bank '{BANK_ID}': {len(pb)} playbooks + {len(docs)} discussion docs ({docs[0]['timestamp'][:10]}..{docs[-1]['timestamp'][:10]}), ~{words:,} words")
    print(f"  types: {types}")
    if a.dry_run:
        print(json.dumps(pb[0], indent=2)[:800])
        return
    hs = client()
    hs.create_bank(bank_id=BANK_ID, reflect_mission=MISSION, background=MISSION)
    for name, text in DIRECTIVES:
        try:
            hs.create_directive(bank_id=BANK_ID, name=name, content=text, priority=10)
        except Exception as e:
            print(f"  directive {name}: {e.__class__.__name__} (already present?)")
    print("bank created with mission + directives")
    r = hs.retain_batch(bank_id=BANK_ID, items=pb)
    log_usage("retain_batch playbooks", usage=r.usage, extra={"docs": len(pb)})
    for i in range(0, len(docs), a.batch):
        b = docs[i:i + a.batch]
        items = [{"content": d["content"], "context": d["context"], "timestamp": d["timestamp"], "document_id": d["doc_id"],
                  "tags": [f"type:{d['doc_type']}"]} for d in b]
        r = hs.retain_batch(bank_id=BANK_ID, items=items)
        log_usage(f"retain_batch {i // a.batch + 1}", usage=r.usage, extra={"docs": len(b), "first_ts": b[0]["timestamp"]})
    r = hs.recall(bank_id=BANK_ID, query="supplier delay on a raw material PO - which data checks?", budget="low", max_tokens=1000,
                  tags=["playbook"], tags_match="any")
    print(f"smoke recall (playbook): {len(r.results or [])} results")
    for x in (r.results or [])[:3]:
        print("  -", x.text[:160])
    hs.close()


if __name__ == "__main__":
    main()
