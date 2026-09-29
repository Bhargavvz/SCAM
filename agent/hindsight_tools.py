"""Hindsight memory tools with the as-of rule enforced client-side.

Hindsight recall has no hard "nothing after date X" filter and reflect has no date filter, so:
* recall hits are resolved to corpus doc_ids (or live decision ids) and anything dated after as_of,
  from the holdout split, or unresolvable is dropped and counted;
* reflect runs natively only when nothing in the bank can be newer than as_of (corpus horizon and the
  bank-wide retain ledger); otherwise it is an as-of-filtered recall + local synthesis guided by the
  bank's own mission, traits and directives (agent/bank_setup.py). Mental Models are only ever consulted
  by native reflect, so they obey the same guard.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from agent.bank_setup import DIRECTIVES, make_client, reflect_mission
from agent.config import Settings
from agent.corpus import DocIndex, as_of_end, load_index, parse_ts

LIVE_PREFIX = "DECL"


def load_bank_policy() -> tuple[str, list[str]]:
    return reflect_mission(), [content for _, content in DIRECTIVES]


def _field(obj, name, default=None):
    if obj is None:
        return default
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _call(fn, **kwargs):
    """Pass only the kwargs the installed client accepts (client versions differ)."""
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**kwargs)
    return fn(**{k: v for k, v in kwargs.items() if k in params})


@dataclass
class MemoryHit:
    text: str
    fact_type: str | None
    doc_id: str | None
    doc_date: str | None
    doc_type: str | None
    source: str  # corpus | live
    superseded_by: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RecallResult:
    query: str
    as_of: str
    hits: list[MemoryHit]
    raw_count: int
    dropped_future: int
    dropped_unresolved: int

    def doc_ids(self) -> list[str]:
        return sorted({h.doc_id for h in self.hits if h.doc_id})

    def to_payload(self, limit: int = 25) -> dict:
        return {"query": self.query, "as_of": self.as_of, "hits": [h.to_dict() for h in self.hits[:limit]],
                "dropped_future": self.dropped_future, "dropped_unresolved": self.dropped_unresolved}


@dataclass
class ReflectResult:
    question: str
    as_of: str
    mode: str  # native | local_filtered
    text: str | None
    doc_ids: list[str]
    hits: list[MemoryHit]
    structured: dict | None = None


class RetainLedger:
    """Bank-wide record of every runtime retain, independent of which live DB a run uses."""

    def __init__(self, path: Path):
        self.path = path

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def _attempted(self, bank_id: str) -> list[dict]:
        # every attempt counts, whatever its status: a retain that timed out client-side may still have been
        # committed on the server, so it must keep disabling native reflect for other live DBs
        return [e for e in self.entries() if e.get("bank_id") == bank_id]

    def max_timestamp(self, bank_id: str) -> datetime | None:
        ts = [parse_ts(e["timestamp"]) for e in self._attempted(bank_id)]
        return max(ts) if ts else None

    def doc_ids(self, bank_id: str) -> set[str]:
        return {e["document_id"] for e in self._attempted(bank_id)}


class HindsightMemory:
    def __init__(self, client, bank_id: str, index: DocIndex, ledger: RetainLedger,
                 live_memory_ids: Callable[[], set[str]],
                 synthesizer: Callable[[str, list[MemoryHit], str], str] | None = None,
                 policy: tuple[str, list[str]] = ("", []), use_mental_models: bool = True):
        if not bank_id:
            raise ValueError("HINDSIGHT_BANK_ID is empty - set it in .env")
        if not use_mental_models and "exclude_mental_models" not in inspect.signature(client.reflect).parameters:
            raise ValueError("MENTAL_MODELS_IN_REFLECT=0 but the installed hindsight-client reflect() has no "
                             "exclude_mental_models parameter; run `python -m agent.mental_models delete` for the "
                             "ablation instead")
        self.client, self.bank_id, self.index, self.ledger = client, bank_id, index, ledger
        self.live_memory_ids, self.synthesizer = live_memory_ids, synthesizer
        self.use_mental_models = use_mental_models
        mission, directives = policy
        self.policy_text = f"Mission: {mission}\nDirectives:\n" + "\n".join(f"- {d}" for d in directives)

    def _resolve(self, raw, as_of: str) -> tuple[MemoryHit | None, str]:
        doc_id, text = _field(raw, "document_id"), _field(raw, "text") or ""
        mentioned, ctx, ftype = _field(raw, "mentioned_at"), _field(raw, "context"), _field(raw, "type")
        if doc_id and str(doc_id).startswith(LIVE_PREFIX):
            if doc_id not in self.live_memory_ids():
                return None, "unresolved"  # retained by a run whose live DB is not in use
            ts = parse_ts(mentioned)
            if ts is None or ts > as_of_end(as_of):
                return None, "future"
            return MemoryHit(text, ftype, str(doc_id).split(".")[0], ts.date().isoformat(), "agent_decision", "live"), "ok"
        doc = self.index.resolve(doc_id, mentioned, ctx)
        if doc is None:
            return None, "unresolved"
        if not self.index.visible(doc, as_of):
            return None, "future"
        sup = [{"doc_id": d.doc_id, "date": d.date} for d in self.index.superseding(doc.doc_id, as_of)]
        return MemoryHit(text, ftype, doc.doc_id, doc.date, doc.doc_type, "corpus", sup), "ok"

    def recall(self, query: str, as_of: str, *, window_days: int | None = None, budget: str = "mid",
               max_tokens: int = 4096) -> RecallResult:
        end = as_of_end(as_of)
        kw = dict(bank_id=self.bank_id, query=query, budget=budget, max_tokens=max_tokens,
                  types=["world", "experience"], query_timestamp=end.isoformat())
        start = end - timedelta(days=window_days) if window_days else None
        if start is not None:
            kw["temporal_window"] = {"start": start.isoformat(), "end": end.isoformat()}
        try:
            resp = _call(self.client.recall, **kw)
        except Exception as ex:  # Hindsight Cloud 500s on temporal_window; retry without it, window applied below
            if "temporal_window" not in kw or "TemporalWindow" not in str(ex):
                raise
            kw.pop("temporal_window")
            resp = _call(self.client.recall, **kw)
        raw = list(_field(resp, "results") or [])
        hits, future, unresolved = [], 0, 0
        for r in raw:
            h, why = self._resolve(r, as_of)
            if h and start is not None and h.doc_date and h.doc_date < start.date().isoformat():
                continue  # outside the requested window (client-side, so it holds even without server support)
            if h:
                hits.append(h)
            elif why == "future":
                future += 1
            else:
                unresolved += 1
        return RecallResult(query, as_of, hits, len(raw), future, unresolved)

    def native_reflect_allowed(self, as_of: str) -> bool:
        end = as_of_end(as_of)
        if end < self.index.memory_horizon:
            return False
        last = self.ledger.max_timestamp(self.bank_id)
        if last is not None and last > end:
            return False
        return self.ledger.doc_ids(self.bank_id) <= self.live_memory_ids()

    def reflect(self, question: str, as_of: str, *, budget: str = "mid",
                response_schema: dict | None = None) -> ReflectResult:
        if self.native_reflect_allowed(as_of):
            kw = dict(bank_id=self.bank_id, query=question, budget=budget, include_facts=True)
            if response_schema is not None:
                kw["response_schema"] = response_schema
            if not self.use_mental_models:
                kw["exclude_mental_models"] = True
            resp = _call(self.client.reflect, **kw)
            based = _field(_field(resp, "based_on"), "memories") or []
            hits = [h for h, _ in (self._resolve(m, as_of) for m in based) if h]
            return ReflectResult(question, as_of, "native", _field(resp, "text"),
                                 sorted({h.doc_id for h in hits if h.doc_id}), hits,
                                 structured=_field(resp, "structured_output"))
        rec = self.recall(question, as_of, budget="high")
        text = self.synthesizer(question, rec.hits, self.policy_text) if self.synthesizer else None
        return ReflectResult(question, as_of, "local_filtered", text, rec.doc_ids(), rec.hits)

    def get_document(self, doc_id: str, as_of: str) -> dict | None:
        doc = self.index.by_id.get(doc_id)
        if doc is None or not self.index.visible(doc, as_of):
            return None
        return {"doc_id": doc.doc_id, "date": doc.date, "doc_type": doc.doc_type, "context": doc.context,
                "content": doc.content, "source_record_ids": list(doc.source_record_ids),
                "supersedes": list(doc.supersedes),
                "superseded_by": [{"doc_id": d.doc_id, "date": d.date} for d in self.index.superseding(doc_id, as_of)]}

    def retain_experience(self, *, document_id: str, content: str, context: str, timestamp: str,
                          metadata: dict) -> str:
        self.ledger.append({"bank_id": self.bank_id, "document_id": document_id, "timestamp": timestamp,
                            "status": "pending", "logged_at": datetime.now(timezone.utc).isoformat()})
        try:
            _call(self.client.retain, bank_id=self.bank_id, content=content, context=context, timestamp=timestamp,
                  document_id=document_id, metadata=metadata, tags=["source:live"])
            status = "ok"
        except Exception as ex:  # external service: record the failure, the DB rows stay the source of truth
            status = f"error: {ex}"
        self.ledger.append({"bank_id": self.bank_id, "document_id": document_id, "timestamp": timestamp,
                            "status": status, "logged_at": datetime.now(timezone.utc).isoformat()})
        return status


def build_memory(settings: Settings, live_memory_ids: Callable[[], set[str]],
                 synthesizer: Callable[[str, list[MemoryHit], str], str] | None = None) -> HindsightMemory:
    return HindsightMemory(make_client(settings), settings.hindsight_bank_id, load_index(settings.corpus_path),
                           RetainLedger(settings.retain_ledger_path), live_memory_ids, synthesizer,
                           load_bank_policy(), use_mental_models=settings.mental_models_in_reflect)
