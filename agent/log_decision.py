"""Close the loop.

Persist the chosen action as append-only live rows (decision + commitment) and retain an experience summary
in Hindsight with the decision's business timestamp. External systems (ERP, supplier portal, email, MES, QMS)
are never called; their would-be actions are returned as clearly labelled mock no-ops.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent.card import DecisionCard
from agent.config import Settings
from agent.db import live_token, open_live_writer
from agent.simulate import ScenarioState

MOCK = "[MOCK - no external call made]"


def commitment_for(option: dict, st: ScenarioState) -> dict:
    a, qty, arr = option["action"], round(st.req, 1), option.get("arrival")
    if a == "switch_supplier":
        return dict(counterparty_type="supplier", counterparty_id=option["alt_supplier_id"], made_by_role="Buyer",
                    commitment_text=f"We place a spot order for {qty:,} {st.uom} of {st.rm_id} with "
                                    f"{option['alt_supplier_id']} for delivery to {st.plant_id} by {arr}",
                    quantity=qty, due_date=arr)
    if a == "expedite":
        return dict(counterparty_type="supplier", counterparty_id=st.supplier_id, made_by_role="Buyer",
                    commitment_text=f"{st.supplier_id} air-expedites {', '.join(st.lot_rpo_ids)} to {st.plant_id}, "
                                    f"arrival by {arr}", quantity=None, due_date=arr)
    if a == "reallocate_stock":
        return dict(counterparty_type="plant", counterparty_id=option["donor_plant"], made_by_role="Materials Manager",
                    commitment_text=f"{option['donor_plant']} transfers {qty:,} {st.uom} of {st.rm_id} to "
                                    f"{st.plant_id} by {arr}", quantity=qty, due_date=arr)
    if a == "substitute_rm":
        return dict(counterparty_type="internal", counterparty_id=st.plant_id, made_by_role="Quality Lead",
                    commitment_text=f"QA approves {option['substitute']} as substitute for {st.rm_id} at "
                                    f"{st.plant_id} by {arr}", quantity=None, due_date=arr)
    if a == "cancel_po":
        return dict(counterparty_type="supplier", counterparty_id=st.supplier_id, made_by_role="Buyer",
                    commitment_text=f"We cancel {st.rpo_id} with {st.supplier_id} and re-buy elsewhere by {arr}",
                    quantity=None, due_date=arr or st.eta.isoformat())
    if a == "accept_delay":
        return dict(counterparty_type="plant", counterparty_id=st.plant_id, made_by_role="Plant Scheduler",
                    commitment_text=f"{st.plant_id} reschedules {st.primary_run_id} to start once {st.rpo_id} "
                                    f"arrives ({st.eta.isoformat()})", quantity=None, due_date=st.eta.isoformat())
    raise ValueError(f"no commitment template for {a}")


def mock_actions_for(option: dict, st: ScenarioState) -> list[str]:
    a, qty = option["action"], f"{round(st.req, 1):,} {st.uom}"
    return {
        "switch_supplier": [f"{MOCK} ERP: would create a spot PO with {option.get('alt_supplier_id')} for {qty} of {st.rm_id}",
                            f"{MOCK} Supplier portal: would request delivery confirmation from {option.get('alt_supplier_id')}"],
        "expedite": [f"{MOCK} Forwarder portal: would book air freight for {', '.join(st.lot_rpo_ids)}"],
        "reallocate_stock": [f"{MOCK} ERP: would create transfer order {option.get('donor_plant')} -> {st.plant_id} for {qty}"],
        "substitute_rm": [f"{MOCK} QMS: would open QA approval of {option.get('substitute')} for {st.rm_id}"],
        "cancel_po": [f"{MOCK} ERP: would cancel {st.rpo_id}",
                      f"{MOCK} Email: would notify {st.supplier_id} of the cancellation"],
        "accept_delay": [f"{MOCK} MES: would move {st.primary_run_id} start to {st.eta.isoformat()}"],
    }[a]


def _next_id(con, table: str, prefix: str) -> str:
    return f"{prefix}{con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] + 1:05d}"


def log_decision(settings: Settings, live_db_path: Path, memory, card: DecisionCard,
                 state: ScenarioState | None, event_ref: str | None = None) -> tuple[dict, list[str]]:
    if card.recommended_action is None or state is None:
        raise ValueError("nothing to log: the card has no recommendation or no scenario state")
    option = next(o for o in card.options if o["action"] == card.recommended_action)
    c = commitment_for(option, state)
    now = datetime.now(timezone.utc).isoformat()
    con = open_live_writer(live_db_path)
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")  # serialise concurrent writers so COUNT(*)+1 ids cannot collide
            dec_id = _next_id(con, "decisions_live", "DECL")
            cmt_id = _next_id(con, "commitments_live", "CMTL")
            con.execute("""INSERT INTO decisions_live (decision_id, event_ref, supplier_id, rm_id, plant_id, rpo_id,
                           decided_at, decided_by_role, decision_type, options_considered_json, chosen_option,
                           rationale_text, expected_cost, expected_stockout_days, evidence_doc_ids,
                           evidence_record_ids, run_id, logged_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (dec_id, event_ref or f"LIVE:{state.rpo_id}", state.supplier_id, state.rm_id, state.plant_id, state.rpo_id,
                         card.as_of, "Decision Agent (prototype)", card.recommended_action,
                         json.dumps([o for o in card.options if o.get("feasible")], sort_keys=True),
                         card.recommended_action, card.rationale, option["cost"], option["stockout_days"],
                         json.dumps(card.cited_doc_ids), json.dumps(card.cited_record_ids), card.run_id, now))
            con.execute("""INSERT INTO commitments_live (commitment_id, decision_id, counterparty_type, counterparty_id,
                           made_by_role, made_at, commitment_text, quantity, due_date, penalty_or_credit, logged_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (cmt_id, dec_id, c["counterparty_type"], c["counterparty_id"], c["made_by_role"], card.as_of,
                         c["commitment_text"], c["quantity"], c["due_date"], None, now))
    finally:
        con.close()
    retain_status = "skipped (WRITEBACK_RETAIN=0)"
    if settings.writeback_retain:
        alts = "; ".join(f"{o['action']} score {o['score']:,.0f}" for o in card.options
                         if o.get("feasible") and o["action"] != card.recommended_action)
        summary = (f"Agent decision {dec_id} on {card.as_of}: {state.supplier_id} moved {state.rpo_id} ({state.rm_id}) "
                   f"for {state.plant_id} to {state.eta.isoformat()}; run {state.primary_run_id} was planned "
                   f"{state.planned_start.isoformat()}. Chose {card.recommended_action}: simulated cost "
                   f"${option['cost']:,.0f}, {option['stockout_days']} stockout days, risk {option['risk']}. "
                   f"Alternatives: {alts}. Commitment {cmt_id}: {c['commitment_text']} (due {c['due_date']}). "
                   f"Evidence: docs {', '.join(card.cited_doc_ids) or 'none'}; records "
                   f"{', '.join(card.cited_record_ids) or 'none'}. Outcome not yet known.")
        retain_status = memory.retain_experience(
            document_id=f"{dec_id}.{live_token(live_db_path)}", content=summary, context="agent decision log",
            timestamp=f"{card.as_of}T12:00:00-05:00",
            metadata={"decision_id": dec_id, "commitment_id": cmt_id, "run_id": card.run_id, "logged_at": now})
    return ({"decision_id": dec_id, "commitment_id": cmt_id, "commitment_text": c["commitment_text"],
             "retain_status": retain_status, "live_db": str(live_db_path)}, mock_actions_for(option, state))
