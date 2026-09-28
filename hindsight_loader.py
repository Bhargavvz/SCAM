"""Load memory_corpus.jsonl (split=memory only) into a Hindsight bank.

API (Hindsight HTTP API, checked against hindsight.vectorize.io/api-reference):
  PUT  /v1/default/banks/{bank_id}              reflect_mission / retain_mission / observations_mission / disposition_*
  POST /v1/default/banks/{bank_id}/memories     {"items": [{content, context, timestamp, document_id, tags}], "async": bool}

Docs are retained in chronological order, in batches, with bounded concurrency *per time slice* so that
later batches never land before earlier ones. Retain is billed per token - use --dry-run first.

  python hindsight_loader.py --corpus output/memory_corpus.jsonl [--dry-run] [--limit N] [--bank-id ID]
Env: HINDSIGHT_BASE_URL (e.g. http://localhost:8888), HINDSIGHT_API_KEY (optional).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent
MISSION = ("Institutional supply-chain memory for a multi-plant manufacturer: suppliers, raw materials, inventory, "
           "negotiations, exceptions, disruptions, decisions and commitments. Connect related events over time and "
           "evaluate the consequences of candidate actions.")
DIRECTIVES = ["Always cite the evidence (document ids, PO/record ids, dates) behind a recommendation.",
              "Always list relevant open commitments before recommending an action.",
              "Never recommend cancelling a PO without first checking open commitments with that supplier."]


def items_from(corpus: Path, limit: int | None):
    docs = [json.loads(l) for l in corpus.read_text(encoding="utf-8").splitlines()]
    docs = [d for d in docs if d["split"] == "memory"]
    docs.sort(key=lambda d: (d["timestamp"], d["doc_id"]))
    if limit:
        docs = docs[:limit]
    return [{"content": d["content"], "context": d["context"], "timestamp": d["timestamp"], "document_id": d["doc_id"],
             "tags": [f"type:{d['doc_type']}"] + [f"entity:{e}" for e in d["entity_ids"][:8]]} for d in docs]


async def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))["hindsight"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(ROOT / "output" / "memory_corpus.jsonl"))
    ap.add_argument("--bank-id", default=cfg["bank_id"])
    ap.add_argument("--batch-size", type=int, default=cfg["batch_size"])
    ap.add_argument("--concurrency", type=int, default=cfg["concurrency"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--async-retain", action="store_true", help="ask the server to process retain asynchronously")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    items = items_from(Path(a.corpus), a.limit)
    words = sum(len(i["content"].split()) for i in items)
    log_path = Path(a.corpus).parent / "retention_log.jsonl"
    print(f"{len(items):,} memory docs, ~{words:,} words (~{int(words * 1.35):,} tokens) -> bank '{a.bank_id}'")
    if a.dry_run:
        print("dry run: first item ->", json.dumps(items[0], indent=2)[:800] if items else "none")
        return
    base = os.environ.get(cfg["base_url_env"], "http://localhost:8888").rstrip("/")
    hdr = {"Authorization": f"Bearer {os.environ[cfg['api_key_env']]}"} if os.environ.get(cfg["api_key_env"]) else {}
    async with httpx.AsyncClient(base_url=base, headers=hdr, timeout=300) as cli:
        r = await cli.put(f"/v1/default/banks/{a.bank_id}", json={
            "reflect_mission": MISSION + " Directives: " + " ".join(DIRECTIVES),
            "retain_mission": "Extract supplier, raw material, PO, plant, event, decision and commitment facts with their dates, "
                              "quantities, reasons and status changes; later updates supersede earlier values.",
            "observations_mission": "Synthesise recurring supplier behaviours, seasonal patterns and decision lessons."})
        r.raise_for_status()
        batches = [items[i:i + a.batch_size] for i in range(0, len(items), a.batch_size)]
        done = set()
        if log_path.exists():  # resumable
            done = {json.loads(l)["batch"] for l in log_path.read_text(encoding="utf-8").splitlines() if json.loads(l).get("ok")}
        sem = asyncio.Semaphore(a.concurrency)

        async def send(i, b):
            async with sem:
                for attempt in range(4):
                    t = time.time()
                    try:
                        rsp = await cli.post(f"/v1/default/banks/{a.bank_id}/memories", json={"items": b, "async": a.async_retain})
                        rsp.raise_for_status()
                        body = rsp.json()
                        return {"batch": i, "ok": True, "docs": [x["document_id"] for x in b], "first_ts": b[0]["timestamp"],
                                "seconds": round(time.time() - t, 2), "usage": body.get("usage")}
                    except httpx.HTTPError as e:
                        err = str(e)
                        await asyncio.sleep(2 ** attempt)
                return {"batch": i, "ok": False, "error": err, "docs": [x["document_id"] for x in b]}

        with open(log_path, "a", encoding="utf-8") as log:
            # chronological: concurrent only within a window of `concurrency` consecutive batches
            for w in range(0, len(batches), a.concurrency):
                todo = [(i, b) for i, b in enumerate(batches[w:w + a.concurrency], start=w) if i not in done]
                for res in await asyncio.gather(*(send(i, b) for i, b in todo)):
                    log.write(json.dumps(res) + "\n")
                    print(f"batch {res['batch'] + 1}/{len(batches)} {'ok' if res['ok'] else 'FAILED: ' + res['error']}")
    print(f"retention log: {log_path}")


if __name__ == "__main__":
    asyncio.run(main())
