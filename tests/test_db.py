import sqlite3

import pytest

from agent.db import asof_views, live_memory_ids, live_token, open_live_writer


def _one(con, sql, *p):
    return con.execute(sql, p).fetchone()


def test_dataset_is_read_only(db_factory):
    con = db_factory("2025-01-13")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM main.decisions")


def test_invalid_as_of_rejected(settings):
    with pytest.raises(ValueError):
        asof_views("2025-1-13")


def test_events_hidden_after_as_of(db_factory):
    con = db_factory("2023-06-30")
    assert _one(con, "SELECT MAX(detected_at) FROM disruption_events")[0] <= "2023-06-30"
    assert _one(con, "SELECT COUNT(*) FROM event_links l JOIN main.disruption_events d ON d.event_id = l.dst_event_id "
                     "WHERE d.detected_at > '2023-06-30'")[0] == 0


def test_decision_outcome_masked_until_assessed(db_factory):
    # DEC00021 decided 2023-04-14, outcome assessed 2023-04-26 (partial, actual cost 655.89)
    before, after = db_factory("2023-04-25"), db_factory("2023-04-26")
    row = _one(before, "SELECT actual_cost, outcome_label, lesson_text FROM decisions WHERE decision_id='DEC00021'")
    assert tuple(row) == (None, None, None)
    row = _one(after, "SELECT outcome_label, source FROM decisions WHERE decision_id='DEC00021'")
    assert tuple(row) == ("partial", "historical")
    assert _one(db_factory("2023-04-13"), "SELECT COUNT(*) FROM decisions WHERE decision_id='DEC00021'")[0] == 0


def test_commitment_status_as_of(db_factory):
    # CMT00560: volume commitment with SUP0169 made 2024-12-26, breached 2025-12-30
    row = _one(db_factory("2025-01-13"), "SELECT status, resolved_at, evidence_ids FROM commitments WHERE commitment_id='CMT00560'")
    assert tuple(row) == ("open", None, None)
    assert _one(db_factory("2025-12-31"), "SELECT status FROM commitments WHERE commitment_id='CMT00560'")[0] == "breached"


def test_rpo_expected_at_follows_revisions(db_factory):
    # RPO003179: slipped to 2023-06-22 on 2023-06-02, expedited to 2023-06-08 on 2023-06-04, received 2023-06-08
    q = "SELECT expected_at, received_at, status FROM rm_purchase_orders WHERE rm_purchase_order_id='RPO003179'"
    assert tuple(_one(db_factory("2023-06-03"), q)) == ("2023-06-22", None, "open")
    assert tuple(_one(db_factory("2023-06-05"), q)) == ("2023-06-08", None, "open")
    assert tuple(_one(db_factory("2023-06-08"), q)) == ("2023-06-08", "2023-06-08", "received")


def test_scorecard_complete_months_only(db_factory):
    con = db_factory("2025-10-14")
    months = [r[0] for r in con.execute("SELECT month FROM supplier_scorecard_monthly WHERE supplier_id='SUP0247'")]
    assert months and max(months) <= "2025-09-01"


def test_live_rows_are_unioned_and_append_only(settings, db_factory):
    w = open_live_writer(settings.live_db_path)
    with w:
        w.execute("INSERT INTO decisions_live VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("DECL00001", "LIVE:RPO023030", "SUP0247", "RM0083", "P01", "RPO023030", "2025-10-14",
                   "Decision Agent (prototype)", "switch_supplier", "[]", "switch_supplier", "why", 2.0, 0.0,
                   "[]", "[]", "run-x", "2026-09-29T00:00:00+00:00"))
    with pytest.raises(sqlite3.DatabaseError):
        with w:
            w.execute("UPDATE decisions_live SET rationale_text='x'")
    w.close()
    assert _one(db_factory("2025-10-14"), "SELECT source FROM decisions WHERE decision_id='DECL00001'")[0] == "live"
    assert _one(db_factory("2025-10-13"), "SELECT COUNT(*) FROM decisions WHERE decision_id='DECL00001'")[0] == 0
    token = live_token(settings.live_db_path)
    assert live_memory_ids(settings.live_db_path) == {f"DECL00001.{token}"}


def test_ongoing_events_do_not_leak_whole_life_aggregates(db_factory):
    # EVT01472 (SUP0247, detected 2024-07-19) is rolled up until 2025-12-23: title "... hits 86 RM lots",
    # anchor_ids with RPOs ordered after 2025-10-14, lots_slipped=86 in event_impacts
    con = db_factory("2025-10-14")
    row = _one(con, "SELECT end_date, anchor_ids, title, severity FROM disruption_events WHERE event_id='EVT01472'")
    assert row["end_date"] is None and row["anchor_ids"] is None and row["severity"] is None
    assert "86" not in row["title"]
    assert _one(con, "SELECT COUNT(*) FROM event_impacts WHERE event_id='EVT01472'")[0] == 0
    assert _one(con, "SELECT COUNT(*) FROM disruption_events WHERE end_date > '2025-10-14'")[0] == 0
    # closed events keep their details
    assert _one(con, "SELECT anchor_ids FROM disruption_events WHERE event_id='EVT00185'")[0] is not None


def test_dated_reference_tables_are_shadowed(db_factory):
    con = db_factory("2025-06-30")
    assert _one(con, "SELECT COUNT(*) FROM contracts WHERE valid_from > '2025-06-30'")[0] == 0
    assert _one(con, "SELECT COUNT(*) FROM rm_supplier_catalog WHERE price_valid_from > '2025-06-30'")[0] == 0
    assert _one(con, "SELECT COUNT(*) FROM bill_of_materials WHERE effective_from > '2025-06-30'")[0] == 0
    assert _one(con, "SELECT COUNT(*) FROM bill_of_materials WHERE effective_to > '2025-06-30'")[0] == 0
    # NEG00126 is still open on 2025-06-30; its resulting contract CTR0117 (valid from 2025-08-01) must not show
    assert _one(con, "SELECT contract_id FROM negotiations WHERE negotiation_id='NEG00126'")[0] is None
