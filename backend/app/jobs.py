"""Background jobs for agent work (decisions and memory briefs take tens of seconds on rate-limited LLMs).

A job is identified by (kind, key). Finished results are persisted under runtime/cache/<kind>/ so a page that was
resolved once opens instantly afterwards, across restarts. A second request for a key that is already running
attaches to the running job instead of starting another one.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.config import ROOT

CACHE_DIR = ROOT / "data" / "cache"


@dataclass
class Job:
    id: str
    kind: str
    key: str
    status: str = "queued"  # queued | running | done | error
    result: Any = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    cached: bool = False
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        end = self.finished or time.time()
        d["elapsed_s"] = round(end - (self.started or self.created), 1)
        return d


def _path(kind: str, key: str) -> Path:
    return CACHE_DIR / kind / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".json")


def load_cached(kind: str, key: str) -> dict | None:
    p = _path(kind, key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def store_cached(kind: str, key: str, result: Any, meta: dict | None = None) -> None:
    p = _path(kind, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"key": key, "computed_at": time.time(), "meta": meta or {}, "result": result},
                            default=str))


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def submit(self, kind: str, key: str, fn: Callable[[], Any], *, force: bool = False,
               meta: dict | None = None) -> Job:
        with self._lock:
            running = self._by_key.get((kind, key))
            if running and self._jobs[running].status in ("queued", "running"):
                return self._jobs[running]
            if not force:
                hit = load_cached(kind, key)
                if hit is not None:
                    job = Job(id=f"cache-{uuid.uuid4().hex[:10]}", kind=kind, key=key, status="done",
                              result=hit["result"], cached=True, created=hit["computed_at"],
                              started=hit["computed_at"], finished=hit["computed_at"], meta=hit.get("meta") or {})
                    self._jobs[job.id] = job
                    return job
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, key=key, meta=meta or {})
            self._jobs[job.id] = job
            self._by_key[(kind, key)] = job.id
        threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        return job

    def _run(self, job: Job, fn: Callable[[], Any]) -> None:
        job.status, job.started = "running", time.time()
        try:
            job.result = fn()
            job.status = "done"
            store_cached(job.kind, job.key, job.result, job.meta)
        except Exception as ex:  # surfaced to the UI; the traceback goes to the server log
            job.status, job.error = "error", f"{type(ex).__name__}: {ex}"
            traceback.print_exc()
        finally:
            job.finished = time.time()


JOBS = JobManager()
