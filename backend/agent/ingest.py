"""Resume memory ingestion into Hindsight.

Retains the split=memory docs that are not yet logged "ok" in the retention log, in chronological order, with live
progress. It stops after a few consecutive failures instead of timing out doc after doc. When Hindsight Cloud's write
path is down, every retain waits 60 s for a 504, so the original loader looked frozen for ~45 minutes.

    .venv/bin/python -m agent.ingest --probe      # one write to see whether Hindsight accepts writes right now
    .venv/bin/python -m agent.ingest              # resume (writes to the same runtime/retention_log.jsonl)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from agent.bank_setup import make_client
from agent.config import load_settings

DOWN_HELP = ("Hindsight Cloud is not accepting writes for this bank right now (retain returns 504 / times out). "
             "Reads still work, so the app runs on the documents already stored. Check the Hindsight dashboard "
             "(status, plan limits) or contact Hindsight support, then run scripts/finish_ingestion.sh again - "
             "it resumes where it stopped.")


def _log_ok(log_path) -> set[str]:
    if not log_path.exists():
        return set()
    return {r["doc_id"] for r in map(json.loads, log_path.read_text().splitlines()) if r.get("status") == "ok"}


def pending_docs(settings) -> list[dict]:
    done = _log_ok(settings.retention_log_path)
    docs = [json.loads(line) for line in settings.corpus_path.open()]
    docs = sorted((d for d in docs if d["split"] == "memory"), key=lambda d: d["timestamp"])
    return [d for d in docs if d["doc_id"] not in done]


async def _retain(client, bank: str, d: dict, timeout: float) -> dict:
    kw = dict(bank_id=bank, content=d["content"], context=d["context"], timestamp=d["timestamp"], document_id=d["doc_id"],
              metadata={"doc_type": d["doc_type"], "entity_ids": ",".join(d["entity_ids"]),
                        "source_record_ids": ",".join(d["source_record_ids"][:20])})
    t = time.time()
    try:
        call = client.aretain(**kw) if hasattr(client, "aretain") else asyncio.to_thread(client.retain, **kw)
        await asyncio.wait_for(call, timeout)
        status = "ok"
    except asyncio.TimeoutError:
        status = f"error: client timeout after {timeout:.0f}s"
    except Exception as ex:  # record every failure mode; the log is the resume point
        status = "error: " + str(ex).split("\n")[0][:200]
    return {"doc_id": d["doc_id"], "timestamp": d["timestamp"], "status": status, "seconds": round(time.time() - t, 2)}


async def run(batch: int, concurrency: int, max_failures: int, timeout: float, probe: bool) -> int:
    s = load_settings()
    todo = pending_docs(s)
    total = sum(1 for line in s.corpus_path.open() if '"split": "memory"' in line)
    print(f"{total - len(todo)}/{total} memory docs already stored; {len(todo)} to go", flush=True)
    if not todo:
        return 0
    client = make_client(s)
    log = s.retention_log_path
    log.parent.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)

    async def one(d):
        async with sem:
            return await _retain(client, s.hindsight_bank_id, d, timeout)

    # probe with the first pending doc alone (idempotent by document_id), so a dead write path is reported in ~1 min
    first = await one(todo[0])
    with log.open("a") as fh:
        fh.write(json.dumps(first) + "\n")
    print(f"probe {first['doc_id']}: {first['status']} ({first['seconds']}s)", flush=True)
    if first["status"] != "ok":
        print(DOWN_HELP, flush=True)
        return 2
    if probe:
        return 0
    todo, streak, stored = todo[1:], 0, 1
    for i in range(0, len(todo), batch):
        results = await asyncio.gather(*(one(d) for d in todo[i:i + batch]))
        with log.open("a") as fh:
            for r in results:
                fh.write(json.dumps(r) + "\n")
        for r in results:
            if r["status"] == "ok":
                streak, stored = 0, stored + 1
            else:
                streak += 1
        print(f"  {total - len(todo) + i + len(results) - 1}/{total} processed · stored this run {stored} · "
              f"failures in a row {streak}", flush=True)
        if streak >= max_failures:
            print(f"stopping after {streak} consecutive failures: {results[-1]['status']}", flush=True)
            print(DOWN_HELP, flush=True)
            return 2
    left = len(pending_docs(s))
    print(f"done: {total - left}/{total} memory docs stored", flush=True)
    return 0 if left == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="only test one write and exit")
    ap.add_argument("--batch", type=int, default=25)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--max-failures", type=int, default=5, help="stop after this many consecutive failed retains")
    ap.add_argument("--timeout", type=float, default=90, help="seconds before a single retain is abandoned")
    a = ap.parse_args(argv)
    return asyncio.run(run(a.batch, a.concurrency, a.max_failures, a.timeout, a.probe))


if __name__ == "__main__":
    sys.exit(main())
