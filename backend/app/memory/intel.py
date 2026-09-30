"""Memory-powered features used across Meridian.

- recall_entity: what the organisation remembers about any record (facts, experiences, consolidated observations),
  dated no later than the business date, resolved back to Meridian records.
- brief: structured reflect (response_schema) -> summary, patterns, risks, recommendations, confidence, citations.
- advise: a pre-action check ("should I cancel this PO / use this carrier / order from this supplier?") -> verdict
  proceed | caution | stop with reasons and precedents, used as a guardrail inside workflows.
- precedent: "has this happened before?" for AI insights.
Results are cached in memory_cache and invalidated when the entity gets new application events.
"""
from __future__ import annotations

import json
import time
from functools import lru_cache

from app.config import settings
from app.db import TODAY, now_iso, one, scalar
from app.memory import service as mem
from app.memory.outbox import ids_in

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentences: what the organisation knows about this record"},
        "patterns": {"type": "array", "items": {"type": "string"}, "description": "recurring behaviours, with dates or periods"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "past_decisions": {"type": "array", "items": {"type": "object", "properties": {
            "what": {"type": "string"}, "when": {"type": "string"}, "outcome": {"type": "string"}}, "required": ["what", "outcome"]}},
        "open_commitments": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["summary", "patterns", "risks", "recommendations", "confidence"],
}

ADVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["proceed", "caution", "stop"]},
        "headline": {"type": "string", "description": "one sentence a planner can act on"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "precedents": {"type": "array", "items": {"type": "object", "properties": {
            "when": {"type": "string"}, "what_happened": {"type": "string"}, "lesson": {"type": "string"}}, "required": ["what_happened"]}},
        "commitments_at_risk": {"type": "array", "items": {"type": "string"}},
        "alternative": {"type": "string", "description": "a better option if the verdict is caution or stop, else empty"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["verdict", "headline", "reasons", "confidence"],
}

PRECEDENT_SCHEMA = {
    "type": "object",
    "properties": {
        "happened_before": {"type": "boolean"},
        "episodes": {"type": "array", "items": {"type": "object", "properties": {
            "when": {"type": "string"}, "what": {"type": "string"}, "response": {"type": "string"}, "outcome": {"type": "string"}},
            "required": ["what"]}},
        "lesson": {"type": "string"},
    },
    "required": ["happened_before", "episodes", "lesson"],
}


# ------------------------------------------------------------------ corpus index (resolve hits to records)
@lru_cache(maxsize=1)
def corpus_index() -> dict[str, dict]:
    p = settings.dataset_dir / "memory_corpus.jsonl"
    out = {}
    if p.exists():
        for line in p.open():
            d = json.loads(line)
            out[d["doc_id"]] = {"doc_type": d.get("doc_type"), "timestamp": d.get("timestamp"), "context": d.get("context"),
                                "records": list(dict.fromkeys((d.get("source_record_ids") or []) + (d.get("entity_ids") or [])))[:12]}
    return out


def resolve(hit: dict) -> dict:
    doc = hit.get("document_id") or ""
    if doc.startswith("MER-"):
        src = "meridian"
        records = [t.split(":", 1)[1] for t in (hit.get("tags") or []) if t.startswith("entity:")]
    else:
        info = corpus_index().get(doc) or {}
        src = "dataset" if info else "memory"
        records = info.get("records") or []
    records = list(dict.fromkeys(records + ids_in(hit.get("text"))))[:10]
    return {"id": hit.get("id"), "text": hit.get("text"), "type": hit.get("type"), "context": hit.get("context"),
            "when": (hit.get("occurred_start") or hit.get("mentioned_at") or "")[:10] or None, "document_id": doc,
            "source": src, "records": records, "tags": hit.get("tags") or [],
            "score": (hit.get("scores") or {}).get("final") if isinstance(hit.get("scores"), dict) else None}


# ------------------------------------------------------------------ cache
def _version(con, entity_id: str | None) -> str:
    """Changes whenever the entity (or anything, when entity is None) gets a new application event."""
    if entity_id:
        n = scalar(con, "SELECT COUNT(*) FROM audit_log WHERE entity_id = ? OR summary LIKE ?", (entity_id, f"%{entity_id}%"))
    else:
        n = scalar(con, "SELECT COUNT(*) FROM audit_log")
    return f"{TODAY}:{n}"


def _get(con, key: str):
    r = one(con, "SELECT payload, created_at FROM memory_cache WHERE key = ?", (key,))
    return ({**json.loads(r["payload"]), "cached_at": r["created_at"]} if r else None)


def _put(con, key: str, payload: dict) -> None:
    con.execute("INSERT OR REPLACE INTO memory_cache VALUES (?, ?, ?)", (key, now_iso(), json.dumps(payload, default=str)))
    con.commit()


# ------------------------------------------------------------------ features
def recall_entity(entity_id: str, label: str = "", query: str | None = None, limit: int = 30, types: list[str] | None = None) -> dict:
    q = query or f"{entity_id} {label}: history, problems, delays, decisions, commitments and outcomes"
    r = mem.recall(q, as_of=TODAY, budget="mid", max_tokens=3000, types=types, include_entities=True)
    hits = [resolve(h) for h in r["results"]]
    # memories that mention the record first, then the rest by relevance
    hits.sort(key=lambda h: (entity_id not in (h["text"] or "") and entity_id not in h["records"]))
    by_type: dict[str, int] = {}
    for h in hits:
        by_type[h["type"]] = by_type.get(h["type"], 0) + 1
    return {"entity_id": entity_id, "query": q, "results": hits[:limit], "by_type": by_type, "latency_ms": r["latency_ms"],
            "entities": sorted(((k, len((v or {}).get("observations") or [])) for k, v in (r.get("entities") or {}).items()),
                               key=lambda x: -x[1])[:15]}


def brief(con, entity_id: str, label: str, kind: str, force: bool = False) -> dict:
    key = f"brief:{entity_id}:{_version(con, entity_id)}"
    if not force and (hit := _get(con, key)):
        return {**hit, "cached": True}
    q = (f"Brief a supply chain manager on {kind} {entity_id} ({label}) as of {TODAY}: what has happened with it, recurring "
         f"patterns, risks, past decisions and how they turned out, open commitments, and what to do next. Only use memories "
         f"dated on or before {TODAY}.")
    r = mem.reflect(q, response_schema=BRIEF_SCHEMA, budget="mid", include_facts=True)
    out = {"entity_id": entity_id, "brief": r["structured"] or {"summary": r["text"], "patterns": [], "risks": [], "recommendations": [],
                                                                 "confidence": "low"},
           "text": r["text"], "evidence": [resolve(m) for m in r["memories"][:12]], "evidence_count": len(r["memories"]),
           "mental_models": [{"id": m.get("id"), "text": (m.get("text") or "")[:300]} for m in r["mental_models"]],
           "directives": [d.get("name") for d in r["directives"]], "latency_ms": r["latency_ms"], "generated_at": now_iso()}
    _put(con, key, out)
    return {**out, "cached": False}


def advise(con, action: str, entity_ids: list[str], details: str = "") -> dict:
    ids = ", ".join(entity_ids)
    key = f"advice:{action}:{ids}:{hash(details)}:{_version(con, entity_ids[0] if entity_ids else None)}"
    if hit := _get(con, key):
        return {**hit, "cached": True}
    q = (f"A planner is about to: {action} (records: {ids}). {details} Based on what happened before with these records "
         f"(delays, quality, commitments, contracts, earlier decisions and their outcomes), should they proceed? Give a verdict "
         f"proceed / caution / stop, the reasons, relevant precedents, any commitment this would put at risk, and a better "
         f"alternative if there is one. Only use memories dated on or before {TODAY}.")
    t = time.time()
    r = mem.reflect(q, response_schema=ADVICE_SCHEMA, budget="mid", include_facts=True)
    out = {"action": action, "entities": entity_ids,
           "advice": r["structured"] or {"verdict": "caution", "headline": r["text"][:300], "reasons": [], "confidence": "low"},
           "evidence": [resolve(m) for m in r["memories"][:10]], "evidence_count": len(r["memories"]),
           "directives": [d.get("name") for d in r["directives"]], "latency_ms": round((time.time() - t) * 1000), "generated_at": now_iso()}
    _put(con, key, out)
    return {**out, "cached": False}


def precedent(con, topic: str, entity_ids: list[str]) -> dict:
    key = f"precedent:{topic}:{','.join(entity_ids)}:{TODAY}"
    if hit := _get(con, key):
        return {**hit, "cached": True}
    q = (f"Has this happened before: {topic} (records: {', '.join(entity_ids) or 'n/a'})? List earlier episodes with when they "
         f"happened, how the organisation responded and the outcome, and the lesson for now. Only memories on or before {TODAY}.")
    r = mem.reflect(q, response_schema=PRECEDENT_SCHEMA, budget="low", include_facts=True)
    out = {"topic": topic, "precedent": r["structured"] or {"happened_before": False, "episodes": [], "lesson": r["text"][:400]},
           "evidence": [resolve(m) for m in r["memories"][:8]], "latency_ms": r["latency_ms"], "generated_at": now_iso()}
    _put(con, key, out)
    return {**out, "cached": False}
