import pytest
from rich.console import Console

from agent.agent_core import DecisionAgent
from agent.capabilities import OUTCOME_SCHEMA, PRECEDENT_SCHEMA, ask, route
from tests.fakes import FakeLLM, FakeMemory, corpus_hit

DEMO = [
    ("What is SUP0247's delivery track record in November and December? Should we trust their current promise "
     "for a November delivery?", "supplier_reliability"),
    ("What open commitments do we have with SUP0091? If we switch suppliers, what obligations would we breach?",
     "commitments"),
    ("Our primary RM supplier just had a 35% supply reduction. We're deciding between prioritizing high-margin "
     "products vs. switching to the backup supplier. What happened last time we faced this?", "decision_precedent"),
    ("Have we ever accepted partial shipments from suppliers? How many times has this exception been granted this "
     "quarter, and should we escalate for a policy review?", "exceptions"),
    ("We're seeing corrosion on steel components from a supplier that had humidity issues before. Are their "
     "corrective controls still in place, or has the problem recurred?", "quality_root_cause"),
    ("We need to renegotiate with SUP0179 whose lead times keep getting longer. What negotiation strategies have "
     "worked with them before?", "negotiation"),
    ("How accurate have our past 'switch to cheaper supplier' decisions been? Should we trust our cost-savings "
     "projections?", "decision_outcome_learning"),
]


@pytest.mark.parametrize("question,expected", DEMO + [("Tell me something useful.", "general")])
def test_route(question, expected):
    assert route(question) == expected


def _agent(settings, text="fake reflection", hits=()):
    mem = FakeMemory(hits, reflect_text=text)
    return DecisionAgent(settings, FakeLLM([]), mem, settings.live_db_path), mem


def test_supplier_reliability_grounded(settings):
    agent, mem = _agent(settings, "SUP0247 slips in Nov-Dec [DOC000001, 2023-01-02]; see RPO000412 and EVT99999.",
                        [corpus_hit("DOC000001", "2023-01-02")])
    ans = ask(agent, DEMO[0][0], as_of="2025-12-31")
    assert ans.capability == "supplier_reliability" and ans.budget == "high"
    months = {r["month"] for r in ans.ground_truth["delay_by_promised_month"]}
    assert {"11", "12"} <= months
    assert ans.cited_doc_ids == ["DOC000001"] and "RPO000412" in ans.cited_record_ids
    assert "EVT99999" in ans.unverifiable_citations
    question_sent = mem.reflects[-1][0]
    assert "delay_by_promised_month" in question_sent and mem.reflects[-1][2] == "high"


def test_commitments_checked_before_switch(settings):
    agent, _ = _agent(settings, "See CMT00560.")
    ans = ask(agent, DEMO[1][0], as_of="2025-06-30")
    assert ans.capability == "commitments" and ans.commitments_checked is True
    assert "open_commitments" in ans.ground_truth and "contracts_in_force" in ans.ground_truth
    assert len(ans.ground_truth["open_commitments"]) == 3


def test_switch_without_named_supplier_warns_and_uses_schema(settings):
    # one verifiable citation, so the structured confidence ("medium" from the fake) is used
    agent, mem = _agent(settings, "No record found of a 35% supply reduction; closest precedent DEC00006.")
    ans = ask(agent, DEMO[2][0], as_of="2025-12-31")
    assert ans.commitments_checked is False
    assert any("no supplier" in w for w in ans.warnings)
    assert mem.reflects[-1][3] == PRECEDENT_SCHEMA and ans.confidence == "medium"


def test_outcome_learning_accuracy_table(settings):
    agent, mem = _agent(settings, "DEC00006 ...")
    ans = ask(agent, DEMO[6][0], as_of="2025-12-31")
    rows = ans.ground_truth["decision_accuracy"]
    assert [r["decision_type"] for r in rows] == ["switch_supplier"] and rows[0]["n"] > 0
    assert mem.reflects[-1][3] == OUTCOME_SCHEMA


def test_no_citations_means_low_confidence(settings):
    agent, _ = _agent(settings, "No record found.")
    ans = ask(agent, DEMO[4][0], as_of="2025-12-31")
    assert ans.confidence == "low" and any("no evidence citations" in w for w in ans.warnings)


def test_render_capability(settings):
    from interface.decision_console import render_capability

    agent, _ = _agent(settings, "SUP0179 conceded price [RPO000412].")
    console = Console(record=True, width=160)
    render_capability(ask(agent, DEMO[5][0], as_of="2025-12-31"), console)
    out = console.export_text()
    assert "negotiation" in out and "RPO000412" in out and "negotiations" in out
