"""Load memory_corpus.jsonl (split=memory only) into a Hindsight memory bank.

    pip install hindsight-client
    python hindsight_loader.py --corpus out/memory_corpus.jsonl --bank sc-memory --base-url http://localhost:8888 [--dry-run]

* Creates / configures the bank (mission + directives) when the installed client
  exposes a bank-configuration method; otherwise prints the settings to apply.
* Retains docs in chronological order in async batches with content, context and
  ISO-8601 timestamp (client.retain(bank_id=..., content=..., context=..., timestamp=...)).
  document_id / metadata are passed only if the installed client's retain() accepts them.
* Writes a retention log (JSONL) and skips docs already logged as ok (resumable).
* --dry-run prints what would be sent plus a token estimate (retain is billed per token).
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import time
from pathlib import Path

MISSION = ("Institutional supply-chain memory for a multi-plant consumer-goods manufacturer: suppliers, raw materials, plants, DCs, "
           "purchase orders, disruptions, negotiations, commitments and past decisions with their outcomes. Use it to recall history, "
           "connect related events and open commitments, evaluate consequences of candidate actions and recommend responses.")
DIRECTIVES = [
    "Cite the evidence (document dates and record IDs such as RPO/PO/EVT/DEC/CMT) behind every claim and recommendation.",
    "Always list open commitments that a recommended action could affect.",
    "Never recommend cancelling a purchase order without first checking open commitments with that supplier.",
    "Prefer the most recent document when facts conflict; say which earlier statement was superseded.",
    "When citing a precedent, state how today's conditions differ from the precedent's conditions.",
]


def load_docs(path: Path) -> list[dict]:
    docs = [json.loads(l) for l in path.open()]
    docs = [d for d in docs if d["split"] == "memory"]
    return sorted(docs, key=lambda d: d["timestamp"])


def est_tokens(docs) -> int:
    return int(sum(len((d["content"] + d["context"]).split()) * 1.35 for d in docs))


def configure_bank(client, bank: str) -> str:
    for name in ("create_bank", "update_bank", "configure_bank", "set_bank_config"):
        fn = getattr(client, name, None)
        if fn is None:
            continue
        params = inspect.signature(fn).parameters
        kw = {"bank_id": bank}
        if "mission" in params:
            kw["mission"] = MISSION
        if "directives" in params:
            kw["directives"] = DIRECTIVES
        try:
            fn(**{k: v for k, v in kw.items() if k in params or k == "bank_id"})
            return f"configured via {name}"
        except Exception as ex:  # bank may already exist
            return f"{name} failed ({ex}); configure mission/directives in the Hindsight UI"
    return "client has no bank-config method; set mission/directives in the Hindsight UI/API"


async def retain_all(client, bank: str, docs: list[dict], log_path: Path, batch: int, concurrency: int):
    done = set()
    if log_path.exists():
        done = {json.loads(l)["doc_id"] for l in log_path.open() if json.loads(l).get("status") == "ok"}
    todo = [d for d in docs if d["doc_id"] not in done]
    params = inspect.signature(client.retain).parameters
    aretain = getattr(client, "aretain", None)
    sem = asyncio.Semaphore(concurrency)

    async def one(d):
        kw = {"bank_id": bank, "content": d["content"], "context": d["context"], "timestamp": d["timestamp"]}
        if "document_id" in params:
            kw["document_id"] = d["doc_id"]
        if "metadata" in params:
            kw["metadata"] = {"doc_type": d["doc_type"], "entity_ids": ",".join(d["entity_ids"]), "source_record_ids": ",".join(d["source_record_ids"][:20])}
        async with sem:
            t = time.time()
            try:
                if aretain:
                    await aretain(**kw)
                else:
                    await asyncio.to_thread(client.retain, **kw)
                st = "ok"
            except Exception as ex:
                st = f"error: {ex}"
            return {"doc_id": d["doc_id"], "timestamp": d["timestamp"], "status": st, "seconds": round(time.time() - t, 2)}

    with log_path.open("a") as fh:
        # batches are submitted in chronological order so temporal facts accumulate in order
        for i in range(0, len(todo), batch):
            res = await asyncio.gather(*[one(d) for d in todo[i:i + batch]])
            for r in res:
                fh.write(json.dumps(r) + "\n")
            fh.flush()
            print(f"  retained {min(i + batch, len(todo))}/{len(todo)}  errors so far: {sum(r['status'] != 'ok' for r in res)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--bank", default="supply-chain-memory")
    ap.add_argument("--base-url", default="http://localhost:8888")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--batch", type=int, default=25)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--log", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    path = Path(a.corpus)
    docs = load_docs(path)[: a.limit]
    log_path = Path(a.log) if a.log else path.with_name("retention_log.jsonl")
    print(f"{len(docs)} memory docs ({docs[0]['timestamp']} .. {docs[-1]['timestamp']}), ~{est_tokens(docs):,} tokens")
    if a.dry_run:
        print("mission:", MISSION)
        print("directives:", *DIRECTIVES, sep="\n  - ")
        for d in docs[:3]:
            print(json.dumps({"bank_id": a.bank, "content": d["content"][:160] + "...", "context": d["context"], "timestamp": d["timestamp"], "document_id": d["doc_id"]}, indent=1))
        return
    from hindsight_client import Hindsight
    client = Hindsight(base_url=a.base_url, **({"api_key": a.api_key} if a.api_key else {}))
    print(configure_bank(client, a.bank))
    asyncio.run(retain_all(client, a.bank, docs, log_path, a.batch, a.concurrency))
    print(f"retention log: {log_path}")


if __name__ == "__main__":
    main()
