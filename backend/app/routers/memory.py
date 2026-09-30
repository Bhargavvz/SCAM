"""Memory (Hindsight) API: status & sync, recall / reflect playground, memories, documents, entities, graph, mental
models, directives, knowledge base, bank configuration, and the memory features used on every record page."""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.config import settings
from app.db import TODAY, audit, get_db, one, page, rows, scalar, tx
from app.jobs import JOBS
from app.memory import bootstrap, intel
from app.memory import outbox as ob
from app.memory import service as mem

router = APIRouter(prefix="/api", tags=["memory"])


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except mem.MemoryError_ as ex:
        raise HTTPException(status_code=502, detail=str(ex)) from ex


def label_for(con, eid: str) -> tuple[str, str]:
    """Human label and kind for any record id."""
    lookups = [
        ("SUP", "supplier", "SELECT supplier_name FROM suppliers WHERE supplier_id = ?"),
        ("IP", "product", "SELECT sku || ' (' || category || ')' FROM products WHERE product_id = ?"),
        ("CUS", "customer", "SELECT customer_name FROM customers WHERE customer_id = ?"),
        ("RPO", "raw material purchase order", "SELECT 'from ' || supplier_id || ' to ' || plant_id FROM rm_purchase_orders WHERE rm_purchase_order_id = ?"),
        ("PO", "purchase order", "SELECT 'from ' || supplier_id || ' to ' || warehouse_id FROM purchase_orders WHERE purchase_order_id = ?"),
        ("SO", "sales order", "SELECT 'for ' || customer_id || ' from ' || warehouse_id FROM sales_orders WHERE sales_order_id = ?"),
        ("SH", "shipment", "SELECT direction || ' via ' || carrier_id FROM shipments WHERE shipment_id = ?"),
        ("RMA", "return", "SELECT product_id || ' ' || reason FROM returns WHERE rma_id = ?"),
        ("RM", "raw material", "SELECT rm_name FROM raw_materials WHERE rm_id = ?"),
        ("PR", "production run", "SELECT product_id || ' at ' || plant_id FROM production_runs WHERE production_run_id = ?"),
        ("W", "warehouse", "SELECT warehouse_name FROM warehouses WHERE warehouse_id = ?"),
        ("CR", "carrier", "SELECT carrier_name FROM carriers WHERE carrier_id = ?"),
        ("EVT", "disruption", "SELECT title FROM disruption_events WHERE event_id = ?"),
    ]
    for pref, kind, sql in lookups:
        if eid.startswith(pref) and eid[len(pref):len(pref) + 1].isdigit():
            return (scalar(con, sql, (eid,)) or eid), kind
    return eid, "record"


# ================================================================ status & sync
@router.get("/memory/status")
def status(con=Depends(get_db)):
    h = mem.health()
    out: dict[str, Any] = {"health": h, "bank_id": settings.bank_id, "outbox": ob.summary(con), "bootstrap": bootstrap.state,
                           "audit_entries": scalar(con, "SELECT COUNT(*) FROM audit_log"),
                           "events_synced": scalar(con, "SELECT COUNT(*) FROM memory_outbox WHERE kind = 'event' AND status = 'synced'"),
                           "corpus_docs": len(intel.corpus_index())}
    if h.get("ok"):
        try:
            out["stats"] = mem.stats()
        except mem.MemoryError_ as ex:
            out["stats_error"] = str(ex)
    return out


@router.get("/memory/coverage")
def coverage(con=Depends(get_db)):
    """Which dataset memories and Meridian events are actually in the bank (lists every document id)."""
    ids = _guard(lambda: mem.cached("docids", 120, mem.all_document_ids))
    corpus = set(intel.corpus_index())
    events = {r[0] for r in con.execute("SELECT document_id FROM memory_outbox WHERE kind = 'event'")}
    missing = sorted(corpus - ids)
    return {"bank_documents": len(ids), "corpus_total": len(corpus), "corpus_in_bank": len(corpus & ids), "corpus_missing": len(missing),
            "corpus_missing_sample": missing[:20], "events_total": len(events), "events_in_bank": len(events & ids),
            "other_documents": len(ids - corpus - events)}


@router.post("/memory/backfill")
def backfill(con=Depends(get_db)):
    ids = _guard(mem.all_document_ids)
    mem.invalidate("docids")
    p = settings.dataset_dir / "memory_corpus.jsonl"
    docs = [d for d in (json.loads(x) for x in p.open()) if d["doc_id"] not in ids]
    n = ob.enqueue_corpus(con, docs)
    with tx(con):
        audit(con, module="memory", action="backfill", entity_type="memory", entity_id="corpus",
              summary=f"Queued {n} dataset memories missing from the memory bank for retention")
    return {"queued": n, "missing": len(docs)}


@router.post("/memory/sync/kick")
def kick():
    ob.kick()
    return {"ok": True, "worker": ob.state}


@router.get("/memory/outbox")
def outbox_list(status: str = "", kind: str = "", q: str = "", limit: int = Query(50, le=500), offset: int = 0, con=Depends(get_db)):
    where, params = [], []
    for col, v in (("status", status), ("kind", kind)):
        if v:
            where.append(f"{col} = ?")
            params.append(v)
    if q:
        where.append("(document_id LIKE ? OR content LIKE ?)")
        params += [f"%{q}%"] * 2
    return page(con, """SELECT outbox_id, kind, document_id, substr(content, 1, 240) AS content, context, timestamp, status,
                               operation_id, attempts, last_error, created_at, submitted_at, synced_at FROM memory_outbox""",
                where, params, sort=None, default_sort="outbox_id DESC", allowed=set(), limit=limit, offset=offset)


@router.post("/memory/outbox/{outbox_id}/retry")
def outbox_retry(outbox_id: int, con=Depends(get_db)):
    with tx(con):
        con.execute("UPDATE memory_outbox SET status = 'queued', next_attempt_at = NULL, attempts = 0 WHERE outbox_id = ?", (outbox_id,))
    ob.kick()
    return one(con, "SELECT * FROM memory_outbox WHERE outbox_id = ?", (outbox_id,))


@router.get("/memory/operations")
def operations(status: Optional[str] = None, limit: int = Query(50, le=100), offset: int = 0):
    return _guard(mem.operations, status or None, limit, offset)


@router.post("/memory/operations/{op_id}/{action}")
def operation_action(op_id: str, action: str):
    if action == "retry":
        return _guard(mem.retry_operation, op_id)
    if action == "cancel":
        return _guard(mem.cancel_operation, op_id)
    raise HTTPException(status_code=400, detail="action must be retry or cancel")


@router.get("/memory/timeseries")
def timeseries(period: str = "30d", time_field: str = "created_at"):
    return _guard(mem.timeseries, period, time_field)


@router.get("/memory/tags")
def tag_list():
    return _guard(mem.tags)


# ================================================================ bootstrap
@router.post("/memory/bootstrap")
def run_bootstrap():
    return _guard(bootstrap.run)


# ================================================================ recall / reflect
class RecallReq(BaseModel):
    query: str = Field(..., min_length=2)
    types: Optional[list[str]] = None
    budget: str = "mid"
    tags: Optional[list[str]] = None
    tags_match: str = "any"
    as_of: Optional[str] = None
    window_days: Optional[int] = None
    prefer_observations: bool = False
    include_source_facts: bool = False
    trace: bool = False
    max_tokens: int = 3000


@router.post("/memory/recall")
def recall(body: RecallReq):
    r = _guard(mem.recall, body.query, types=body.types, tags=body.tags, tags_match=body.tags_match, budget=body.budget,
               max_tokens=body.max_tokens, as_of=body.as_of, window_days=body.window_days, include_entities=True,
               include_source_facts=body.include_source_facts, prefer_observations=body.prefer_observations, trace=body.trace)
    return {**r, "results": [intel.resolve(x) for x in r["results"]],
            "entities": [{"name": k, "observations": (v or {}).get("observations") or []} for k, v in (r.get("entities") or {}).items()][:30]}


SCHEMAS = {"brief": intel.BRIEF_SCHEMA, "advice": intel.ADVICE_SCHEMA, "precedent": intel.PRECEDENT_SCHEMA}


class ReflectReq(BaseModel):
    query: str = Field(..., min_length=2)
    context: Optional[str] = None
    budget: str = "mid"
    schema_name: Optional[str] = None
    response_schema: Optional[dict] = None
    tags: Optional[list[str]] = None
    include_facts: bool = True
    apply_all_directives: bool = True
    exclude_mental_models: bool = False
    fact_types: Optional[list[str]] = None


@router.post("/memory/reflect")
def reflect(body: ReflectReq):
    schema = body.response_schema or SCHEMAS.get(body.schema_name or "")

    def run():
        r = mem.reflect(body.query, context=body.context, budget=body.budget, response_schema=schema, tags=body.tags,
                        include_facts=body.include_facts, apply_all_directives=body.apply_all_directives,
                        exclude_mental_models=body.exclude_mental_models, fact_types=body.fact_types)
        return {**r, "memories": [intel.resolve(m) for m in r["memories"][:40]], "memory_count": len(r["memories"])}

    return JOBS.submit("reflect", json.dumps(body.model_dump(), sort_keys=True), run, force=True).to_dict()


# ================================================================ browse
@router.get("/memory/memories")
def memories(type: Optional[str] = None, search: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None,
             limit: int = Query(50, le=200), offset: int = 0):
    r = _guard(mem.memories, type=type or None, search=search, start=start, end=end, limit=limit, offset=offset)
    return {**r, "items": [{**i, "resolved": intel.resolve({**i, "type": i.get("fact_type")})} for i in r.get("items", [])]}


@router.get("/memory/memories/{memory_id}")
def memory_detail(memory_id: str):
    return _guard(mem.memory, memory_id)


@router.get("/memory/documents")
def documents(q: Optional[str] = None, tag: Optional[str] = None, limit: int = Query(50, le=200), offset: int = 0):
    return _guard(mem.documents, q, [tag] if tag else None, limit, offset)


@router.get("/memory/documents/{doc_id}")
def document(doc_id: str):
    d = _guard(mem.document, doc_id)
    try:
        chunks = mem.document_chunks(doc_id)
    except mem.MemoryError_:
        chunks = None
    units = _guard(mem.memory_graph, None, doc_id, 60)
    return {"document": d, "chunks": chunks, "graph": units, "corpus": intel.corpus_index().get(doc_id)}


@router.get("/memory/entities")
def entity_list(limit: int = Query(100, le=500), offset: int = 0):
    return _guard(mem.entities, limit, offset)


@router.get("/memory/entities/{entity_id}")
def entity_detail(entity_id: str):
    return _guard(mem.entity, entity_id)


@router.get("/memory/entity-graph")
def entity_graph(limit: int = Query(120, le=400), min_count: int = 3):
    return _guard(lambda: mem.cached(f"egraph:{limit}:{min_count}", 300, lambda: mem.entity_graph(limit, min_count)))


@router.get("/memory/graph")
def memory_graph(q: Optional[str] = None, limit: int = Query(80, le=300)):
    return _guard(mem.memory_graph, q, None, limit)


# ================================================================ mental models
class MMReq(BaseModel):
    name: str
    source_query: str
    tags: Optional[list[str]] = None
    refresh_cron: Optional[str] = None
    id: Optional[str] = None


@router.get("/memory/mental-models")
def mm_list():
    return _guard(mem.mental_models, "content")


@router.get("/memory/mental-models/{mm_id}")
def mm_get(mm_id: str):
    return _guard(mem.mental_model, mm_id)


@router.post("/memory/mental-models")
def mm_create(body: MMReq, con=Depends(get_db)):
    r = _guard(mem.create_mental_model, body.name, body.source_query, tags=body.tags, mm_id=body.id, refresh_cron=body.refresh_cron)
    with tx(con):
        audit(con, module="memory", action="create_mental_model", entity_type="mental_model", entity_id=str(r.get("id") or body.name),
              summary=f"Created mental model '{body.name}': {body.source_query}")
    return r


@router.patch("/memory/mental-models/{mm_id}")
def mm_update(mm_id: str, body: MMReq):
    return _guard(mem.update_mental_model, mm_id, name=body.name, source_query=body.source_query, tags=body.tags,
                  trigger={"refresh_cron": body.refresh_cron} if body.refresh_cron else None)


@router.post("/memory/mental-models/{mm_id}/{action}")
def mm_action(mm_id: str, action: str):
    if action == "refresh":
        return _guard(mem.refresh_mental_model, mm_id)
    if action == "dry-run":
        return JOBS.submit("mm-dry-run", mm_id, lambda: mem.dry_run_mental_model(mm_id), force=True).to_dict()
    raise HTTPException(status_code=400, detail="action must be refresh or dry-run")


@router.get("/memory/mental-models/{mm_id}/history")
def mm_history(mm_id: str):
    return _guard(mem.mental_model_history, mm_id)


@router.delete("/memory/mental-models/{mm_id}")
def mm_delete(mm_id: str):
    return _guard(mem.delete_mental_model, mm_id)


# ================================================================ directives
class DirectiveReq(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None


@router.get("/memory/directives")
def directive_list():
    return _guard(mem.directives)


@router.post("/memory/directives")
def directive_create(body: DirectiveReq):
    if not body.name or not body.content:
        raise HTTPException(status_code=400, detail="name and content are required")
    return _guard(mem.create_directive, body.name, body.content, body.priority or 0, ["meridian"])


@router.patch("/memory/directives/{d_id}")
def directive_update(d_id: str, body: DirectiveReq):
    return _guard(mem.update_directive, d_id, name=body.name, content=body.content, priority=body.priority, is_active=body.is_active)


@router.delete("/memory/directives/{d_id}")
def directive_delete(d_id: str):
    return _guard(mem.delete_directive, d_id)


# ================================================================ knowledge base
class KBReq(BaseModel):
    name: str
    source_query: Optional[str] = None
    parent_id: Optional[str] = None
    refresh_cron: Optional[str] = None


@router.get("/memory/kb")
def kb_tree():
    return _guard(mem.kb_tree)


@router.get("/memory/kb/pages/{page_id}")
def kb_page(page_id: str):
    return _guard(mem.kb_page, page_id)


@router.get("/memory/kb/search")
def kb_search(q: str, limit: int = 10):
    return _guard(mem.kb_search, q, limit)


@router.post("/memory/kb")
def kb_create(body: KBReq):
    if body.source_query:
        return _guard(mem.kb_create_page, body.name, body.source_query, body.parent_id, ["meridian"], body.refresh_cron)
    return _guard(mem.kb_create_folder, body.name, body.parent_id)


@router.delete("/memory/kb/{node_id}")
def kb_delete(node_id: str):
    return _guard(mem.kb_delete, node_id)


# ================================================================ bank configuration
CONFIG_KEYS = ["reflect_mission", "retain_mission", "observations_mission", "enable_observations", "enable_auto_consolidation",
               "enable_text_search", "enable_temporal_retrieval", "enable_graph_retrieval", "enable_reranking",
               "disposition_skepticism", "disposition_literalism", "disposition_empathy", "recall_max_tokens", "retain_extraction_mode"]


@router.get("/memory/config")
def get_config():
    c = _guard(mem.config)
    cfg = c.get("config", c)
    return {"editable": {k: cfg.get(k) for k in CONFIG_KEYS}, "all": cfg}


@router.patch("/memory/config")
def patch_config(body: dict, con=Depends(get_db)):
    changes = {k: v for k, v in body.items() if k in CONFIG_KEYS}
    if not changes:
        raise HTTPException(status_code=400, detail=f"editable keys: {', '.join(CONFIG_KEYS)}")
    r = _guard(mem.update_config, **changes)
    with tx(con):
        audit(con, module="memory", action="configure", entity_type="bank", entity_id=settings.bank_id,
              summary=f"Updated memory bank settings: {', '.join(changes)}", payload=changes)
    return r


@router.get("/memory/export")
def export():
    data = _guard(mem.export_bank)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{settings.bank_id}_{TODAY}.zip"'})


# ================================================================ record-level memory features
class EntityReq(BaseModel):
    query: Optional[str] = None
    force: bool = False


@router.post("/memory/entity/{eid}/recall")
def entity_recall(eid: str, body: EntityReq, con=Depends(get_db)):
    lab, _ = label_for(con, eid)
    return _guard(intel.recall_entity, eid, lab, body.query)


@router.post("/memory/entity/{eid}/brief")
def entity_brief(eid: str, body: EntityReq, con=Depends(get_db)):
    lab, kind = label_for(con, eid)

    def run():
        from app.db import connect
        c = connect()
        try:
            return intel.brief(c, eid, lab, kind, force=body.force)
        finally:
            c.close()
    return JOBS.submit("brief", f"{eid}:{TODAY}", run, force=True).to_dict()


class AdviceReq(BaseModel):
    action: str
    entity_ids: list[str]
    details: str = ""


@router.post("/memory/advise")
def advise(body: AdviceReq):
    def run():
        from app.db import connect
        c = connect()
        try:
            return intel.advise(c, body.action, body.entity_ids, body.details)
        finally:
            c.close()
    return JOBS.submit("advice", json.dumps(body.model_dump(), sort_keys=True), run, force=True).to_dict()


class PrecedentReq(BaseModel):
    topic: str
    entity_ids: list[str] = []


@router.post("/memory/precedent")
def precedent(body: PrecedentReq):
    def run():
        from app.db import connect
        c = connect()
        try:
            return intel.precedent(c, body.topic, body.entity_ids)
        finally:
            c.close()
    return JOBS.submit("precedent", json.dumps(body.model_dump(), sort_keys=True), run, force=True).to_dict()


class NoteReq(BaseModel):
    entity_id: str
    note: str = Field(..., min_length=5, max_length=4000)


@router.post("/memory/notes")
def add_note(body: NoteReq, con=Depends(get_db)):
    """A planner's note about a record becomes a memory (through the audited outbox)."""
    lab, kind = label_for(con, body.entity_id)
    with tx(con):
        audit(con, module="memory", action="note", entity_type=kind, entity_id=body.entity_id,
              summary=f"Planner note on {body.entity_id} ({lab}): {body.note}")
    ob.kick()
    return {"ok": True}


@router.get("/jobs/{job_id}")
def job(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="unknown job (the server may have restarted)")
    return j.to_dict()
