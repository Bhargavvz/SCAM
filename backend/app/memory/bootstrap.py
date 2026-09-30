"""Idempotent setup of the Meridian layer in the memory bank: directives, mental models and knowledge-base playbooks.
Existing items (matched by id or name) are left alone, so this is safe to run at every start."""
from __future__ import annotations

import threading
import traceback

from app.db import now_iso
from app.memory import service as mem

DIRECTIVES = [
    ("Respect the business date", "Never use or cite memories about events after the business date given in the question. "
     "If the question states an 'as of' date, treat later events as unknown.", 10),
    ("Name records by id", "Refer to suppliers, orders, products, shipments, runs and decisions by their record ids "
     "(e.g. SUP0247, RPO019444, SO012345) so answers can be traced back to the operational system.", 8),
    ("Separate history from recent actions", "Distinguish dataset history from actions recorded in Meridian (documents "
     "MER-...) and say when a conclusion rests on a single episode.", 5),
]

MENTAL_MODELS = [
    ("mm-supplier-reliability", "Supplier reliability", "Which suppliers repeatedly deliver late or short, by how much, for what reasons, and has it improved or worsened recently?", ["suppliers"]),
    ("mm-seasonal-slippage", "Seasonal slippage", "Which suppliers, materials or lanes slip in particular months or quarters, and how large is the seasonal effect?", ["suppliers", "planning"]),
    ("mm-disruption-playbook", "Disruption response playbook", "When a supplier delay threatens production, which responses (expedite, switch supplier, substitute material, reallocate stock, accept delay, cancel) worked, which failed, and under what conditions?", ["decisions"]),
    ("mm-expedite-outcomes", "Expedite vs accept delay", "How did expediting and accepting delays turn out compared with expectations: actual vs expected cost and stockout days, and when was each the right call?", ["decisions"]),
    ("mm-commitments", "Commitments and obligations", "What volume commitments, credits, penalties and contract obligations exist with suppliers and customers, and which were kept, breached or renegotiated?", ["commitments"]),
    ("mm-carrier-performance", "Carrier performance", "How reliable is each carrier and lane, which carriers degraded recently, and what happened to shipments that were late?", ["logistics"]),
    ("mm-returns-quality", "Returns and quality", "Which products or brands generate returns, for what reasons, and what quality holds or defects were reported by suppliers?", ["returns", "quality"]),
    ("mm-material-shortages", "Raw material shortages", "Which raw materials run short at which plants, what caused it, and which production runs were blocked?", ["production"]),
    ("mm-lead-time-creep", "Lead-time creep", "Which suppliers' actual lead times have been drifting longer than quoted, and by how much?", ["suppliers"]),
    ("mm-forecast-lessons", "Demand and forecast lessons", "Where did demand forecasts miss, in which direction, and what demand shocks or promotions explain it?", ["planning"]),
    ("mm-production-delays", "Production delay causes", "Why do production runs start late at each plant and how long are the delays?", ["production"]),
    ("mm-recent-actions", "Recent operational actions", "What have planners done in Meridian recently (orders, receipts, returns, adjustments, decisions) and what did it change?", ["meridian"]),
]

KB_FOLDER = "Meridian playbooks"
KB_PAGES = [
    ("Supplier risk playbook", "Write a playbook for managing supplier risk: the suppliers with the worst delivery and quality history, warning signs that preceded past failures, and the responses that worked."),
    ("Disruption response playbook", "Write a playbook for responding to a late raw material purchase order that threatens production: options, when each worked or failed in the past, costs, and commitment traps."),
    ("Logistics and carrier playbook", "Write a playbook for choosing carriers and handling late shipments, based on how carriers and lanes actually performed."),
    ("Returns and quality playbook", "Write a playbook for handling returns and supplier quality problems: which products and brands are affected, root causes, and dispositions that worked."),
    ("Inventory and replenishment lessons", "Write the lessons learned about inventory levels, shortages, excess stock and replenishment decisions."),
]

state: dict = {"status": "idle", "started_at": None, "finished_at": None, "created": [], "errors": []}


def _try(label: str, fn, exists) -> None:
    """Create one item. Hindsight often finishes the work after the gateway times out (504) or returns an unparsable
    body, so on error confirm whether the item now exists before recording a failure."""
    try:
        fn()
        state["created"].append(label)
    except Exception as ex:
        try:
            ok = exists()
        except Exception:
            ok = False
        if ok:
            state["created"].append(f"{label} (confirmed after slow response)")
        else:
            state["errors"].append(f"{label}: {str(ex)[:160] or type(ex).__name__}")


def _kb_nodes() -> list[dict]:
    out = []

    def walk(n):
        out.append(n)
        for c in n.get("children") or []:
            walk(c)
    for r in mem.kb_tree().get("roots") or []:
        walk(r)
    return out


def run() -> dict:
    state.update(status="running", started_at=now_iso(), created=[], errors=[])
    try:
        have = {d.get("name") for d in mem.directives()}
        for name, content, prio in DIRECTIVES:
            if name not in have:
                _try(f"directive: {name}", lambda: mem.create_directive(name, content, priority=prio, tags=["meridian"]),
                     lambda: name in {d.get("name") for d in mem.directives()})
        existing = {m.get("id") for m in mem.mental_models(detail="metadata")}
        for mm_id, name, query, tags in MENTAL_MODELS:
            if mm_id not in existing:
                _try(f"mental model: {name}",
                     lambda: mem.create_mental_model(name, query, tags=["meridian", *tags], mm_id=mm_id, refresh_cron="0 5 * * *"),
                     lambda: mm_id in {m.get("id") for m in mem.mental_models(detail="metadata")})
        folder = next((n for n in _kb_nodes() if n.get("kind") == "folder" and n.get("name") == KB_FOLDER), None)
        if not folder:
            _try(f"knowledge folder: {KB_FOLDER}", lambda: mem.kb_create_folder(KB_FOLDER),
                 lambda: any(n.get("name") == KB_FOLDER for n in _kb_nodes()))
            folder = next((n for n in _kb_nodes() if n.get("kind") == "folder" and n.get("name") == KB_FOLDER), None)
        names = {n.get("name") for n in _kb_nodes() if n.get("kind") == "page"}
        for name, query in KB_PAGES:
            if name not in names:
                _try(f"knowledge page: {name}",
                     lambda: mem.kb_create_page(name, query, parent_id=folder and folder.get("id"), tags=["meridian", "playbook"],
                                                refresh_cron="0 6 * * *"),
                     lambda: name in {n.get("name") for n in _kb_nodes() if n.get("kind") == "page"})
    except Exception as ex:
        traceback.print_exc()
        state["errors"].append(str(ex)[:200])
    state.update(status="done" if not state["errors"] else "done with errors", finished_at=now_iso())
    return state


def start() -> None:
    if mem.enabled():
        threading.Thread(target=run, name="memory-bootstrap", daemon=True).start()
