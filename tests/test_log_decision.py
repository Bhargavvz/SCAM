import sqlite3
from dataclasses import replace

import pytest

from agent import sql_tools
from agent.agent_core import DecisionAgent
from agent.db import live_token, open_live_writer
from agent.log_decision import MOCK, log_decision
from agent.scenarios import holdout_context, load_holdout
from tests.fakes import FakeLLM, FakeMemory, response, submission, text, tool_use


def _run(settings, sid, writeback=True):
    s = load_holdout(settings)[sid]
    llm = FakeLLM([response(tool_use("submit_recommendation", submission("switch_supplier", cited_record_ids=[s["event_id"]]))),
                   response(text("Switch."), stop="end_turn")])
    mem = FakeMemory()
    card = DecisionAgent(settings, llm, mem, settings.live_db_path).run(
        s["day0_report"], as_of=s["day0"], context=holdout_context(s), writeback=writeback)
    return card, mem


def test_writeback_rows_retain_and_mocks(settings, db_factory):
    card, mem = _run(settings, "HS05")
    wb = card.writeback
    assert (wb["decision_id"], wb["commitment_id"]) == ("DECL00001", "CMTL00001")
    token = live_token(settings.live_db_path)
    assert mem.retained[0]["document_id"] == f"DECL00001.{token}"
    assert mem.retained[0]["timestamp"] == "2025-10-14T12:00:00-05:00"
    assert "switch_supplier" in mem.retained[0]["content"]
    assert card.mock_actions and all(m.startswith(MOCK) for m in card.mock_actions)
    # the next disruption (HS08, same supplier, 2025-10-19) sees the live decision as a precedent
    decs = sql_tools.prior_decisions(db_factory("2025-10-19"), "SUP0247", "RM0083", "2025-10-19")
    assert decs[0]["decision_id"] == "DECL00001" and decs[0]["source"] == "live"


def test_second_decision_gets_next_id_and_rows_are_append_only(settings):
    _run(settings, "HS05")
    card, _ = _run(settings, "HS08")
    assert card.writeback["decision_id"] == "DECL00002"
    con = open_live_writer(settings.live_db_path)
    with pytest.raises(sqlite3.DatabaseError):
        with con:
            con.execute("DELETE FROM commitments_live")
    con.close()


def test_retain_can_be_disabled(settings):
    card, mem = _run(replace(settings, writeback_retain=False), "HS05")
    assert mem.retained == [] and card.writeback["retain_status"].startswith("skipped")


def test_log_decision_requires_recommendation(settings):
    card, _ = _run(settings, "HS05", writeback=False)
    card.recommended_action = None
    with pytest.raises(ValueError):
        log_decision(settings, settings.live_db_path, FakeMemory(), card, None)
