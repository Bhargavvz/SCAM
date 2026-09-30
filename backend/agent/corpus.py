"""Local index over memory_corpus.jsonl.

Resolves Hindsight hits back to doc_ids, applies the as-of visibility rule and exposes the corpus
`supersedes` graph so later statements win over earlier ones.
"""
from __future__ import annotations

import functools
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HQ_OFFSET = "-05:00"


def as_of_end(as_of: str) -> datetime:
    return datetime.fromisoformat(f"{as_of}T23:59:59{HQ_OFFSET}")


def parse_ts(value) -> datetime | None:
    if value in (None, ""):
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _minute(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


@dataclass(frozen=True)
class CorpusDoc:
    doc_id: str
    timestamp: datetime
    context: str
    content: str
    doc_type: str
    entity_ids: tuple[str, ...]
    source_record_ids: tuple[str, ...]
    split: str
    supersedes: tuple[str, ...]

    @property
    def date(self) -> str:
        return self.timestamp.date().isoformat()


class DocIndex:
    def __init__(self, docs: list[CorpusDoc]):
        self.by_id = {d.doc_id: d for d in docs}
        self._by_minute_ctx: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._by_minute: dict[str, list[str]] = defaultdict(list)
        self._superseded_by: dict[str, list[str]] = defaultdict(list)
        for d in docs:
            self._by_minute_ctx[(_minute(d.timestamp), d.context)].append(d.doc_id)
            self._by_minute[_minute(d.timestamp)].append(d.doc_id)
            for old in d.supersedes:
                self._superseded_by[old].append(d.doc_id)
        mem = [d.timestamp for d in docs if d.split == "memory"]
        self.memory_horizon = max(mem) if mem else datetime.min.replace(tzinfo=timezone.utc)

    @classmethod
    def load(cls, path: Path) -> "DocIndex":
        docs = []
        with Path(path).open() as fh:
            for line in fh:
                r = json.loads(line)
                docs.append(CorpusDoc(r["doc_id"], parse_ts(r["timestamp"]), r["context"], r["content"], r["doc_type"],
                                      tuple(r["entity_ids"]), tuple(r["source_record_ids"]), r["split"],
                                      tuple(r.get("supersedes") or ())))
        return cls(docs)

    def resolve(self, document_id=None, mentioned_at=None, context=None) -> CorpusDoc | None:
        if document_id and document_id in self.by_id:
            return self.by_id[document_id]
        ts = parse_ts(mentioned_at)
        if ts is None:
            return None
        ids = self._by_minute_ctx.get((_minute(ts), context), []) if context else self._by_minute.get(_minute(ts), [])
        return self.by_id[ids[0]] if len(ids) == 1 else None  # ambiguous -> unresolved

    def visible(self, doc: CorpusDoc, as_of: str) -> bool:
        return doc.split == "memory" and doc.timestamp <= as_of_end(as_of)

    def superseding(self, doc_id: str, as_of: str) -> list[CorpusDoc]:
        later = (self.by_id[i] for i in self._superseded_by.get(doc_id, []))
        return sorted((d for d in later if self.visible(d, as_of)), key=lambda d: d.timestamp)


@functools.lru_cache(maxsize=4)
def load_index(path: Path) -> DocIndex:
    return DocIndex.load(path)
