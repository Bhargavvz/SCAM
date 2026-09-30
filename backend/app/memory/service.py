"""Hindsight memory service: one place that talks to the memory bank.

Covers the whole API surface Meridian uses:
  retain / retain_batch (async, tagged, with entities)       recall (types, tags, temporal window, entities, trace)
  reflect (structured output, tags, facts, directives)      mental models (CRUD, refresh, dry run, history)
  directives (CRUD)                                          knowledge base (folders, pages, tree, search)
  memories / documents / chunks / entities / entity graph    operations (status, list, retry, cancel)
  bank stats, config, tags, timeseries, export               health / version

The generated Hindsight client binds its HTTP session to an event loop, so every thread gets its own client.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

from app.config import settings

_local = threading.local()


class MemoryError_(RuntimeError):
    pass


def enabled() -> bool:
    return bool(settings.hindsight_base_url and settings.hindsight_api_key and settings.bank_id)


def _client(timeout: float = 60.0):
    key = f"c{int(timeout)}"
    c = getattr(_local, key, None)
    if c is None:
        from hindsight_client import Hindsight
        c = Hindsight(base_url=settings.hindsight_base_url, api_key=settings.hindsight_api_key, timeout=timeout, max_attempts=2)
        setattr(_local, key, c)
    return c


def _run(coro):
    from hindsight_client.hindsight_client import _run_async
    return _run_async(coro)


def to_plain(obj: Any) -> Any:
    """Pydantic models / lists / dicts -> JSON-safe plain data."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_plain(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return to_plain(obj.model_dump(by_alias=False, exclude_none=False))
    if hasattr(obj, "to_dict"):
        return to_plain(obj.to_dict())
    return str(obj)


def _describe(ex: Exception, timeout: float) -> str:
    import asyncio
    if isinstance(ex, (TimeoutError, asyncio.TimeoutError)) or type(ex).__name__ == "TimeoutError":
        return f"no response within {int(timeout)}s (Hindsight may still complete it in the background)"
    msg = " ".join(m for m in str(ex).split("\n")[:2] if m)[:300]
    return msg or type(ex).__name__


def call(fn_name: str, *args, timeout: float = 60.0, **kwargs):
    if not enabled():
        raise MemoryError_("Hindsight is not configured (HINDSIGHT_BASE_URL / HINDSIGHT_API_KEY / bank id)")
    fn = getattr(_client(timeout), fn_name)
    try:
        return fn(*args, **kwargs)
    except Exception as ex:  # surface API errors with their status line only
        raise MemoryError_(f"Hindsight {fn_name} failed: {_describe(ex, timeout)}") from ex


def low(api: str, method: str, *args, timeout: float = 60.0, **kwargs):
    """Call a low-level generated API method (documents, entities, operations, banks, memory)."""
    if not enabled():
        raise MemoryError_("Hindsight is not configured")
    c = _client(timeout)
    try:
        return _run(getattr(getattr(c, f"_{api}_api"), method)(*args, _request_timeout=timeout, **kwargs))
    except Exception as ex:
        raise MemoryError_(f"Hindsight {api}.{method} failed: {_describe(ex, timeout)}") from ex


B = lambda: settings.bank_id  # noqa: E731


# ------------------------------------------------------------------ health / bank
_cache: dict[str, tuple[float, Any]] = {}


def cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def invalidate(prefix: str = "") -> None:
    for k in list(_cache):
        if k.startswith(prefix):
            _cache.pop(k, None)


def health() -> dict:
    if not enabled():
        return {"ok": False, "configured": False, "error": "not configured"}
    try:
        t = time.time()
        v = to_plain(low("monitoring", "get_version", timeout=10))
        return {"ok": True, "configured": True, "bank_id": B(), "version": v.get("api_version"), "features": v.get("features"),
                "latency_ms": round((time.time() - t) * 1000)}
    except MemoryError_ as ex:
        return {"ok": False, "configured": True, "bank_id": B(), "error": str(ex)}


def stats() -> dict:
    return cached("stats", 20, lambda: to_plain(low("banks", "get_agent_stats", B())))


def config() -> dict:
    return to_plain(call("get_bank_config", B()))


def update_config(**kw) -> dict:
    invalidate()
    return to_plain(call("update_bank_config", B(), **{k: v for k, v in kw.items() if v is not None}))


def timeseries(period: str = "30d", time_field: str = "mentioned_at") -> dict:
    return to_plain(low("banks", "get_memories_timeseries", B(), period=period, time_field=time_field))


def tags() -> dict:
    return to_plain(low("memory", "list_tags", B()))


def export_bank() -> bytes:
    return call("export_bank", B(), include_data=True, include_bank_config=True, timeout=300)


# ------------------------------------------------------------------ write
def retain_batch(items: list[dict], *, document_tags: list[str] | None = None) -> dict:
    """Async batch retain. Returns {'operation_id': ...}; completion is tracked through the operations API."""
    r = to_plain(call("retain_batch", B(), items=items, document_tags=document_tags, retain_async=True))
    return {"operation_id": r.get("operation_id"), "operation_ids": r.get("operation_ids"), "items": r.get("items_count")}


def operation(op_id: str) -> dict:
    return to_plain(low("operations", "get_operation_status", B(), op_id, timeout=20))


def operations(status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    return to_plain(low("operations", "list_operations", B(), status=status, limit=limit, offset=offset, exclude_parents=False))


def retry_operation(op_id: str) -> dict:
    return to_plain(low("operations", "retry_operation", B(), op_id))


def cancel_operation(op_id: str) -> dict:
    return to_plain(low("operations", "cancel_operation", B(), op_id))


# ------------------------------------------------------------------ read
def recall(query: str, *, types: list[str] | None = None, tags: list[str] | None = None, tags_match: str = "any",
           budget: str = "mid", max_tokens: int = 2048, as_of: str | None = None, window_days: int | None = None,
           include_entities: bool = True, include_source_facts: bool = False, prefer_observations: bool = False,
           trace: bool = False) -> dict:
    kw: dict[str, Any] = {"types": types, "budget": budget, "max_tokens": max_tokens, "include_entities": include_entities,
                          "include_source_facts": include_source_facts, "prefer_observations": prefer_observations, "trace": trace}
    if tags:
        kw.update(tags=tags, tags_match=tags_match)
    if as_of:
        kw["query_timestamp"] = f"{as_of}T23:59:59Z"
    if window_days and as_of:
        kw["temporal_window"] = {"before_days": window_days, "after_days": 0}
    t = time.time()
    try:
        r = call("recall", B(), query, **kw)
    except MemoryError_:
        if "temporal_window" not in kw:
            raise
        kw.pop("temporal_window")  # some server versions reject windows; filter client side instead
        r = call("recall", B(), query, **kw)
    out = to_plain(r)
    results = out.get("results") or []
    if as_of:  # never surface memories about events after the business date
        cut = f"{as_of}T23:59:59"
        results = [x for x in results if not (x.get("occurred_start") or x.get("mentioned_at") or "") or
                   (x.get("occurred_start") or x.get("mentioned_at")) <= cut]
    return {"results": results, "entities": out.get("entities"), "source_facts": out.get("source_facts"),
            "trace": out.get("trace"), "latency_ms": round((time.time() - t) * 1000)}


def reflect(query: str, *, context: str | None = None, budget: str = "mid", response_schema: dict | None = None,
            tags: list[str] | None = None, tags_match: str = "any", include_facts: bool = True,
            apply_all_directives: bool = True, fact_types: list[str] | None = None, max_tokens: int | None = None,
            exclude_mental_models: bool = False) -> dict:
    kw: dict[str, Any] = {"budget": budget, "context": context, "response_schema": response_schema, "include_facts": include_facts,
                          "apply_all_directives": apply_all_directives, "fact_types": fact_types, "max_tokens": max_tokens,
                          "exclude_mental_models": exclude_mental_models}
    if tags:
        kw.update(tags=tags, tags_match=tags_match)
    t = time.time()
    out = to_plain(call("reflect", B(), query, **{k: v for k, v in kw.items() if v is not None}, timeout=150))
    based = out.get("based_on") or {}
    return {"text": out.get("text") or "", "structured": out.get("structured_output"),
            "structured_error": out.get("structured_output_error"),
            "memories": based.get("memories") or [], "mental_models": based.get("mental_models") or [],
            "directives": based.get("directives") or [], "usage": out.get("usage"), "latency_ms": round((time.time() - t) * 1000)}


def memories(*, type: str | None = None, search: str | None = None, start: str | None = None, end: str | None = None,
             time_field: str = "mentioned_at", limit: int = 50, offset: int = 0) -> dict:
    return to_plain(call("list_memories", B(), type=type, search_query=search or None, time_field=time_field if (start or end) else None,
                         start_date=start, end_date=end, limit=limit, offset=offset))


def memory(memory_id: str) -> dict:
    return to_plain(low("memory", "get_memory", B(), memory_id))


def documents(q: str | None = None, tags: list[str] | None = None, limit: int = 50, offset: int = 0) -> dict:
    return to_plain(low("documents", "list_documents", B(), q=q or None, tags=tags or None, tags_match="any" if tags else None,
                        limit=limit, offset=offset))


def document(doc_id: str) -> dict:
    return to_plain(low("documents", "get_document", B(), doc_id))


def document_chunks(doc_id: str) -> dict:
    return to_plain(low("documents", "list_document_chunks", B(), doc_id))


def all_document_ids(page_size: int = 500) -> set[str]:
    ids, off = set(), 0
    while True:
        r = documents(limit=page_size, offset=off)
        items = r.get("items") or []
        ids |= {i["id"] for i in items}
        if len(items) < page_size:
            return ids
        off += page_size


def entities(limit: int = 100, offset: int = 0) -> dict:
    return to_plain(low("entities", "list_entities", B(), limit=limit, offset=offset))


def entity(entity_id: str) -> dict:
    return to_plain(low("entities", "get_entity", B(), entity_id))


def entity_graph(limit: int = 150, min_count: int = 2) -> dict:
    return to_plain(low("entities", "get_entity_graph", B(), limit=limit, min_count=min_count))


def memory_graph(q: str | None = None, document_id: str | None = None, limit: int = 120) -> dict:
    return to_plain(low("memory", "get_graph", B(), q=q or None, document_id=document_id, limit=limit))


# ------------------------------------------------------------------ mental models
def mental_models(detail: str = "content") -> list[dict]:
    r = to_plain(call("list_mental_models", B(), detail=detail))
    return r.get("items", []) if isinstance(r, dict) else r


def mental_model(mm_id: str) -> dict:
    return to_plain(call("get_mental_model", B(), mm_id, detail="full"))


def create_mental_model(name: str, source_query: str, tags: list[str] | None = None, mm_id: str | None = None,
                        refresh_cron: str | None = None, max_tokens: int | None = None) -> dict:
    trigger = {"refresh_cron": refresh_cron} if refresh_cron else None
    return to_plain(call("create_mental_model", B(), name=name, source_query=source_query, tags=tags, id=mm_id,
                         trigger=trigger, max_tokens=max_tokens, timeout=150))


def update_mental_model(mm_id: str, **kw) -> dict:
    return to_plain(call("update_mental_model", B(), mm_id, **{k: v for k, v in kw.items() if v is not None}, timeout=150))


def refresh_mental_model(mm_id: str) -> dict:
    return to_plain(call("refresh_mental_model", B(), mm_id, timeout=150))


def dry_run_mental_model(mm_id: str) -> dict:
    return to_plain(call("dry_run_refresh_mental_model", B(), mm_id, timeout=150))


def mental_model_history(mm_id: str) -> Any:
    return to_plain(call("get_mental_model_history", B(), mm_id))


def delete_mental_model(mm_id: str) -> Any:
    return to_plain(call("delete_mental_model", B(), mm_id, timeout=150))


# ------------------------------------------------------------------ directives
def directives() -> list[dict]:
    r = to_plain(call("list_directives", B()))
    return r.get("items", []) if isinstance(r, dict) else r


def create_directive(name: str, content: str, priority: int = 0, tags: list[str] | None = None) -> dict:
    return to_plain(call("create_directive", B(), name=name, content=content, priority=priority, is_active=True, tags=tags))


def update_directive(d_id: str, **kw) -> dict:
    return to_plain(call("update_directive", B(), d_id, **{k: v for k, v in kw.items() if v is not None}))


def delete_directive(d_id: str) -> Any:
    return to_plain(call("delete_directive", B(), d_id))


# ------------------------------------------------------------------ knowledge base
def kb_tree() -> dict:
    return to_plain(call("get_knowledge_base_tree", B()))


def kb_create_folder(name: str, parent_id: str | None = None) -> dict:
    return to_plain(call("create_knowledge_folder", B(), name=name, parent_id=parent_id, timeout=150))


def kb_create_page(name: str, source_query: str, parent_id: str | None = None, tags: list[str] | None = None,
                   refresh_cron: str | None = None) -> dict:
    trigger = {"refresh_cron": refresh_cron} if refresh_cron else None
    return to_plain(call("create_knowledge_page", B(), name=name, source_query=source_query, parent_id=parent_id, tags=tags,
                         trigger=trigger, timeout=150))


def kb_page(page_id: str) -> dict:
    return to_plain(call("get_knowledge_page", B(), page_id))


def kb_search(q: str, limit: int = 10) -> dict:
    return to_plain(call("search_knowledge_base", B(), q, limit=limit))


def kb_delete(node_id: str) -> Any:
    return to_plain(call("delete_knowledge_node", B(), node_id, timeout=150))
