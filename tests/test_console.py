import pytest
from rich.console import Console

from agent.card import DecisionCard, QAResult
from agent.scenarios import holdout_context, load_holdout
from agent.simulate import best_option, build_state, simulate_options
from interface.decision_console import main, render_answer, render_card


def _card(db_factory, base_settings):
    s = load_holdout(base_settings)["HS05"]
    con = db_factory(s["day0"])
    opts = simulate_options(con, build_state(con, day0=s["day0"], **holdout_context(s)))
    return DecisionCard(
        run_id="run-test", as_of=s["day0"], report=s["day0_report"], entities={}, situation_summary="SUP0247 slipped.",
        options=opts, simulator_best_action=best_option(opts)["action"], recommended_action="switch_supplier",
        confidence="high", rationale="Switch [DOC000001].",
        precedents=[{"decision_id": "DEC00353", "event_id": "EVT02501", "decided_at": "2025-08-28",
                     "decision_type": "accept_delay", "outcome_label": "success", "today_rank": 5,
                     "applies_today": False, "note": "today accept_delay ranks 5 of 6"}],
        open_commitments=[{"commitment_id": "CMT00835", "counterparty_id": "SUP0247", "due_date": "2025-12-07",
                           "commitment_text": "Supplier 247 confirmed RPO023030", "affected_by": []}],
        cited_doc_ids=["DOC000001"], cited_record_ids=["DEC00353"], unverifiable_citations=["DOC999999"],
        guardrail_events=["Rejected cancel_po: would breach CMT00560"], warnings=["example warning"],
        mock_actions=["[MOCK - no external call made] ERP: would create a spot PO"],
        usage={"calls": 2, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}, latency_s=1.5)


def test_render_card_sections(db_factory, base_settings):
    console = Console(record=True, width=180)
    render_card(_card(db_factory, base_settings), console)
    out = console.export_text()
    for needle in ("Candidate options", "switch_supplier", "★", "Precedents", "DEC00353", "Open commitments",
                   "CMT00835", "Guardrails", "CMT00560", "Unverifiable", "DOC999999", "MOCK", "example warning",
                   "not modelled"):
        assert needle in out, needle


def test_render_answer():
    console = Console(record=True, width=120)
    render_answer(QAResult(question="q?", as_of="2023-06-11", answer="2023-06-08", cited_doc_ids=["DOC000254"]), console)
    out = console.export_text()
    assert "2023-06-08" in out and "DOC000254" in out


def test_question_requires_as_of():
    with pytest.raises(SystemExit):
        main(["--question", "What is the ETA?"])
