import sqlite3

import pytest

from agent import sql_tools as st


@pytest.mark.parametrize("q", [
    "DELETE FROM decisions",
    "SELECT 1; DROP TABLE decisions",
    "PRAGMA table_info(decisions)",
    "SELECT * FROM main.decisions",
    "SELECT * FROM live.decisions_live",
    "select * from  MAIN . commitments",
    "",
])
def test_check_query_rejects(q):
    with pytest.raises(st.SQLGuardError):
        st.check_query(q)


def test_check_query_allows_keywords_inside_string_literals():
    q = "SELECT commitment_id FROM commitments WHERE commitment_text LIKE '%update the delete plan%';"
    assert st.check_query(q).endswith("'%update the delete plan%'")


def test_run_sql_respects_as_of_and_row_cap(db_factory):
    con = db_factory("2023-06-30")
    out = st.run_sql(con, "SELECT event_id, detected_at FROM disruption_events ORDER BY detected_at", max_rows=5)
    assert out["columns"] == ["event_id", "detected_at"]
    assert len(out["rows"]) == 5 and out["truncated"] is True
    latest = st.run_sql(con, "SELECT MAX(detected_at) FROM disruption_events")["rows"][0][0]
    assert latest <= "2023-06-30"


def test_run_sql_timeout(db_factory):
    con = db_factory("2025-12-31")
    heavy = "SELECT COUNT(*) FROM rm_inventory_movements a JOIN rm_inventory_movements b ON a.rm_id = b.rm_id"
    with pytest.raises(sqlite3.OperationalError):
        st.run_sql(con, heavy, timeout_s=0.05)
    assert st.run_sql(con, "SELECT 1")["rows"] == [[1]]  # handler removed after the timeout


def test_open_commitments_match_holdout_gold(db_factory):
    con = db_factory("2025-10-14")  # HS05: SUP0247 / P01
    ids = [c["commitment_id"] for c in st.open_commitments(con, ["SUP0247", "P01"], "2025-10-14")]
    assert sorted(ids) == ["CMT00785", "CMT00800", "CMT00817", "CMT00825", "CMT00828", "CMT00835"]


def test_open_commitments_flags_volume_commitment(db_factory):
    con = db_factory("2025-01-13")
    rows = {c["commitment_id"]: c for c in st.open_commitments(con, ["SUP0169", "P03", None], "2025-01-13")}
    assert rows["CMT00560"]["is_volume_commitment"] == 1


def test_prior_decisions_and_events(db_factory):
    con = db_factory("2025-10-14")
    decs = st.prior_decisions(con, "SUP0247", "RM0083", "2025-10-14")
    assert decs and all(d["decided_at"] <= "2025-10-14" for d in decs)
    # known outcomes rank before pending ones, so the trap precedent DEC00353 (accept_delay, success) is included
    assert "DEC00353" in [d["decision_id"] for d in decs[:6]]
    known = [d["outcome_label"] is not None for d in decs]
    assert known == sorted(known, reverse=True)
    evs = st.prior_events(con, "SUP0247", "RM0083", "P01")
    assert evs and all(e["detected_at"] <= "2025-10-14" for e in evs)
    links = st.event_links_for(con, [e["event_id"] for e in evs])
    assert all({"src_event_id", "dst_event_id", "link_type"} <= set(l) for l in links)


def test_supplier_scorecard(db_factory):
    rows = st.supplier_scorecard(db_factory("2025-10-14"), "SUP0247", months=3)
    assert len(rows) == 3 and rows[0]["month"] >= rows[-1]["month"]


@pytest.mark.parametrize("rid,expected", [
    ("EVT01987", True), ("DEC00021", True), ("CMT00560", True), ("RPO016183", True), ("REV000864", True),
    ("PR0016522", True), ("SUP0169", True), ("RM0100", True), ("P03", True), ("W012", True),
    ("EVT99999", False), ("DECL00001", False), ("RM0100|P03|2025-01-12", None), ("banana", None),
])
def test_record_exists(db_factory, rid, expected):
    assert st.record_exists(db_factory("2025-12-31"), rid) is expected


def test_record_exists_respects_as_of(db_factory):
    assert st.record_exists(db_factory("2024-01-01"), "EVT01987") is False


def test_schema_summary_lists_tables(db_factory):
    s = st.schema_summary(db_factory("2025-12-31"))
    assert "commitments(" in s and "rm_purchase_orders(" in s and "decisions_live" not in s
