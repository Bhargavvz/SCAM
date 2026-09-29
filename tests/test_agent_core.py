import json
from dataclasses import replace

import pytest

from agent.agent_core import DecisionAgent, MemoryUnavailable
from agent.scenarios import holdout_context, load_holdout
from agent.simulate import build_state, simulate_options
from tests.fakes import FakeLLM, FakeMemory, corpus_hit, response, submission, text, tool_use

CONFLICT = ("2025-01-13 - Supplier 169 (SUP0169) just moved RPO016183 (Elastic Film Laminate, RM0100) for Eastfield "
            "Plant (P03) to 2025-03-10 citing weather force majeure. Run PR0016522 is planned 2025-02-17 and needs "
            "this material. The buyer proposes cancelling RPO016183 and re-buying elsewhere. What should we do?")
CONFLICT_CTX = {"rpo_id": "RPO016183", "run_ids": ["PR0016522"], "rm_id": "RM0100"}


@pytest.fixture
def hs05(base_settings):
    return load_holdout(base_settings)["HS05"]


def agent(settings, script, hits=(), fail=False):
    # pin the knobs the assertions depend on, whatever the developer's .env says
    settings = replace(settings, llm_structured_effort="low", llm_rationale_effort="high",
                       llm_temperature_structured=None, llm_temperature_rationale=None)
    llm, mem = FakeLLM(script), FakeMemory(hits, fail)
    return DecisionAgent(settings, llm, mem, settings.live_db_path), llm, mem


def test_hs05_happy_path(settings, db_factory, hs05):
    a, llm, mem = agent(settings, [
        response(tool_use("simulate_action", {"action_type": "switch_supplier"})),
        response(tool_use("submit_recommendation", submission(
            "switch_supplier", cited_doc_ids=["DOC000001", "DOC999999"],
            cited_record_ids=["RPO023030", "DEC00353", "CMT99999"]))),
        response(text("Switch supplier: $2 and 0 stockout days [RPO023030]."), stop="end_turn"),
    ], hits=[corpus_hit("DOC000001", "2023-01-02")])
    card = a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))

    con = db_factory(hs05["day0"])
    expected = simulate_options(con, build_state(con, day0=hs05["day0"], **holdout_context(hs05)))
    assert card.options == expected
    assert card.recommended_action == card.simulator_best_action == "switch_supplier"
    assert not card.deviates_from_simulator_best
    # trap is visible: an accept_delay success precedent that does not apply today
    assert any(p["decision_type"] == "accept_delay" and p["outcome_label"] == "success" and not p["applies_today"]
               for p in card.precedents)
    assert sorted(c["commitment_id"] for c in card.open_commitments) == sorted(
        c["commitment_id"] for c in hs05["open_commitments"])
    assert card.cited_doc_ids == ["DOC000001"] and card.cited_record_ids == ["RPO023030", "DEC00353"]
    assert card.unverifiable_citations == ["DOC999999", "CMT99999"]
    assert card.warnings == []
    assert [c["effort"] for c in llm.calls] == ["low", "low", "high"]
    assert "tools" not in llm.calls[-1]
    assert {q[2] for q in mem.recalls} == {None, 120}  # broad, entity and temporal-window recalls
    assert card.usage["calls"] == 3 and card.trace


def test_commitment_conflict_is_caught_before_recommending(settings):
    a, llm, _ = agent(settings, [
        response(tool_use("submit_recommendation", submission("cancel_po"))),
        response(tool_use("submit_recommendation", submission("switch_supplier", cited_record_ids=["CMT00560"]))),
        response(text("Do not cancel: CMT00560 is an open volume commitment."), stop="end_turn"),
    ])
    card = a.run(CONFLICT, as_of="2025-01-13", context=CONFLICT_CTX)
    assert card.guardrail_events[0].startswith("Proposed action cancel_po blocked")
    assert any(e.startswith("Rejected cancel_po") and "CMT00560" in e for e in card.guardrail_events)
    rejection = llm.calls[1]["messages"][-1]["content"][0]
    assert rejection["is_error"] is True and "CMT00560" in rejection["content"]
    assert card.recommended_action == "switch_supplier"
    flagged = {c["commitment_id"]: c for c in card.open_commitments}
    assert flagged["CMT00560"]["affected_by"] == ["cancel_po"]


def test_persistent_violation_is_overridden(settings):
    a, _, _ = agent(settings, [response(tool_use("submit_recommendation", submission("cancel_po")))] * 3
                    + [response(text("Override applied."), stop="end_turn")])
    card = a.run(CONFLICT, as_of="2025-01-13", context=CONFLICT_CTX)
    assert card.recommended_action == card.simulator_best_action
    assert card.recommended_action != "cancel_po"
    assert any(e.startswith("Override:") for e in card.guardrail_events)


def test_no_rpo_skips_decision_llm(settings):
    a, llm, _ = agent(settings, [])
    card = a.run("Supplier 247 (SUP0247) is late again on Stretch Wrap. What do we do?", as_of="2025-10-14")
    assert card.recommended_action is None and card.options == []
    assert "names no RM purchase order" in card.warnings[0]
    assert llm.calls == []


def test_ungrounded_number_in_rationale_is_flagged(settings, hs05):
    a, _, _ = agent(settings, [
        response(tool_use("submit_recommendation", submission("switch_supplier"))),
        response(text("Switching costs $99,999 and avoids 4 stockout days."), stop="end_turn"),
    ])
    card = a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))
    assert any("$99,999" in w and "4 stockout days" in w for w in card.warnings)


def test_sql_guard_error_goes_back_to_model(settings, hs05):
    a, llm, _ = agent(settings, [
        response(tool_use("sql_query", {"query": "DELETE FROM decisions"})),
        response(tool_use("submit_recommendation", submission("switch_supplier"))),
        response(text("ok"), stop="end_turn"),
    ])
    a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))
    result = llm.calls[1]["messages"][-1]["content"][0]
    assert "only SELECT" in json.loads(result["content"])["error"]


def test_reflect_tool_available_to_model(settings, hs05):
    a, llm, _ = agent(settings, [
        response(tool_use("hindsight_reflect", {"question": "What happened with SUP0247 before?"})),
        response(tool_use("submit_recommendation", submission("switch_supplier", cited_doc_ids=["DOC000001"]))),
        response(text("ok"), stop="end_turn"),
    ], hits=[corpus_hit("DOC000001", "2023-01-02")])
    card = a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))
    out = json.loads(llm.calls[1]["messages"][-1]["content"][0]["content"])
    assert out["mode"] == "local_filtered" and out["doc_ids"] == ["DOC000001"]
    assert card.cited_doc_ids == ["DOC000001"]


def test_memory_failure_is_reported(settings, hs05):
    a, _, _ = agent(settings, [], fail=True)
    with pytest.raises(MemoryUnavailable, match="HINDSIGHT_BASE_URL"):
        a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))


class ReflectFailsMemory(FakeMemory):
    def reflect(self, *a, **kw):
        raise ConnectionError("connection reset")


def test_reflect_failure_after_gather_is_reported(settings, hs05):
    a = DecisionAgent(settings, FakeLLM([]), ReflectFailsMemory(), settings.live_db_path)
    with pytest.raises(MemoryUnavailable, match="HINDSIGHT_BASE_URL"):
        a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))


def test_report_without_leading_date_uses_default_as_of_with_warning(settings):
    a, _, _ = agent(settings, [])
    card = a.run("SUP0247 (RM0083) slipped to 2025-12-07; what now?")
    assert card.as_of == settings.default_as_of
    assert any("as_of" in w for w in card.warnings)


def test_malformed_tool_inputs_are_returned_as_errors(settings, hs05):
    # non-strict providers (Groq) can send tool calls with missing keys
    a, llm, _ = agent(settings, [
        response(tool_use("sql_query", {})),
        response(tool_use("submit_recommendation", {"recommended_action": "switch_supplier"})),
        response(tool_use("submit_recommendation", submission("switch_supplier"))),
        response(text("ok"), stop="end_turn"),
    ])
    card = a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))
    assert "query" in json.loads(llm.calls[1]["messages"][-1]["content"][0]["content"])["error"]
    missing = llm.calls[2]["messages"][-1]["content"][0]
    assert missing["is_error"] is True and "situation_summary" in missing["content"]
    assert card.recommended_action == "switch_supplier"


def test_malformed_answer_submission_is_returned_as_error(settings):
    a, llm, _ = agent(settings, [
        response(tool_use("submit_answer", {"answer": "2023-06-08"})),
        response(tool_use("submit_answer", {"answer": "2023-06-08", "cited_doc_ids": [], "cited_record_ids": [],
                                            "confidence": "high"})),
    ])
    res = a.answer_question("What is the latest ETA for RPO003179?", "2023-06-11")
    assert "cited_doc_ids" in llm.calls[1]["messages"][-1]["content"][0]["content"]
    assert res.answer == "2023-06-08"


class MainThreadOnlyMemory(FakeMemory):
    """hindsight-client's sync API wraps an aiohttp loop and breaks when called from worker threads."""

    def recall(self, *a, **kw):
        import threading
        assert threading.current_thread() is threading.main_thread(), "recall called from a worker thread"
        return super().recall(*a, **kw)


def test_recalls_run_on_the_calling_thread(settings, hs05):
    a = DecisionAgent(settings, FakeLLM([response(tool_use("submit_recommendation", submission("switch_supplier"))),
                                         response(text("ok"), stop="end_turn")]),
                      MainThreadOnlyMemory(), settings.live_db_path)
    card = a.run(hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))
    assert card.recommended_action == "switch_supplier"
