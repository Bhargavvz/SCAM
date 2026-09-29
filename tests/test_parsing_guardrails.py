import pytest

from agent.guardrails import action_violations, split_citations, ungrounded_numbers
from agent.parsing import parse_report

HS01 = ("2025-10-01 - Supplier 192 (SUP0192) just moved RPO023175 (PP Impact Copolymer Grade B, RM0079) for "
        "Riverbend Plant (P04) to 2025-10-30. We have about 111.9 kg on hand vs safety stock 105.2; run PR0021648 "
        "is planned 2025-10-21 and needs 178.0 kg. 1 run(s) and ~598 FG units for W012 depend on it. What should we do?")
CONFLICT = ("2025-01-13 - Supplier 169 (SUP0169) just moved RPO016183 (Elastic Film Laminate, RM0100) for Eastfield "
            "Plant (P03) to 2025-03-10. Run PR0016522 is planned 2025-02-17. The buyer proposes cancelling "
            "RPO016183 and re-buying elsewhere. What should we do?")


def test_parse_holdout_report():
    e = parse_report(HS01)
    assert e.report_date == "2025-10-01"
    assert (e.supplier_ids, e.rm_ids, e.plant_ids) == (["SUP0192"], ["RM0079"], ["P04"])
    assert (e.rpo_ids, e.run_ids, e.warehouse_ids) == (["RPO023175"], ["PR0021648"], ["W012"])
    assert e.proposed_action is None


def test_parse_proposed_action():
    assert parse_report(CONFLICT).proposed_action == "cancel_po"
    assert parse_report("Planner suggests we expedite RPO1 by air.").proposed_action == "expedite"
    assert parse_report("Should we cancel? Nobody proposed anything.").proposed_action is None


OPTS = [
    {"action": "accept_delay", "feasible": True, "supported": True, "cost": 311.0, "score": 311.0, "stockout_days": 21, "breaches_commitments": []},
    {"action": "switch_supplier", "feasible": True, "supported": True, "cost": 1.0, "score": 1.0, "stockout_days": 0, "breaches_commitments": []},
    {"action": "cancel_po", "feasible": True, "supported": True, "cost": 8.0, "score": 11.0, "stockout_days": 0, "breaches_commitments": ["CMT00560"]},
    {"action": "substitute_rm", "feasible": False, "supported": True, "note": "no substitute"},
    {"action": "renegotiate", "feasible": False, "supported": False, "note": "not modelled"},
]


@pytest.mark.parametrize("action,needle", [
    ("cancel_po", "CMT00560"), ("substitute_rm", "infeasible"), ("renegotiate", "not modelled"),
    ("expedite", "not simulated"), ("teleport", "taxonomy"),
])
def test_action_violations(action, needle):
    v = action_violations(action, OPTS)
    assert v and needle in v[0]


def test_no_violation_for_clean_action():
    assert action_violations("switch_supplier", OPTS) == []


def test_split_citations(db_factory):
    con = db_factory("2025-01-13")
    docs, recs, bad = split_citations(con, ["DOC000001", "DOC000002", "DOC000001"],
                                      ["CMT00560", "EVT99999", "RM0100|P03|2025-01-12", "RPO016183"], {"DOC000001"})
    assert docs == ["DOC000001"]
    assert recs == ["CMT00560", "RPO016183"]
    assert bad == ["DOC000002", "EVT99999", "RM0100|P03|2025-01-12"]


def test_ungrounded_numbers():
    ok = "Switch supplier costs $1 with 0 stockout days vs accept delay at $311 and 21 stockout days. QA takes ~6 days."
    assert ungrounded_numbers(ok, OPTS) == []
    bad = "Expedite would cost about $5,000 and cause 3 stockout days."
    assert ungrounded_numbers(bad, OPTS) == ["$5,000", "3 stockout days"]
    assert ungrounded_numbers("DEC00353 cost $11,555 in the end.", OPTS, extra_values=[11555.42]) == []


def test_report_date_only_from_leading_date():
    assert parse_report("SUP0192 moved RPO023175 to 2025-10-30. What now?").report_date is None
    assert parse_report("  2025-10-01 - SUP0192 moved RPO023175 to 2025-10-30").report_date == "2025-10-01"
