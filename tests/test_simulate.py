import pytest

from agent.scenarios import compare_to_gold, holdout_context, load_holdout
from agent.simulate import (SIMULATED, TAXONOMY, SimulationError, best_option, build_state, simulate_action,
                            simulate_options)


@pytest.fixture(scope="module")
def holdout(base_settings):
    return load_holdout(base_settings)


@pytest.mark.parametrize("sid", [f"HS{i:02d}" for i in range(1, 15)])
def test_options_match_generator(db_factory, holdout, sid):
    s = holdout[sid]
    con = db_factory(s["day0"])
    opts = simulate_options(con, build_state(con, day0=s["day0"], **holdout_context(s)))
    assert compare_to_gold(opts, s["candidate_actions"]) == []
    assert best_option(opts)["action"] == s["gold_best_action"]


def test_state_matches_report_numbers(db_factory, holdout):
    s = holdout["HS01"]  # "...needs 178.0 kg" ; slipped to 2025-10-30
    con = db_factory(s["day0"])
    st = build_state(con, day0=s["day0"], **holdout_context(s))
    assert round(st.req, 1) == 178.0
    assert st.eta.isoformat() == "2025-10-30"
    assert st.lot_rpo_ids == ("RPO023175", "RPO023521")


def test_cancel_po_flags_open_volume_commitment(db_factory):
    con = db_factory("2025-01-13")  # EVT01987: SUP0169 slipped RPO016183 (RM0100 at P03)
    opts = {o["action"]: o for o in simulate_options(
        con, build_state(con, rpo_id="RPO016183", run_ids=["PR0016522"], day0="2025-01-13"))}
    assert opts["cancel_po"]["breaches_commitments"] == ["CMT00560"]
    assert opts["cancel_po"]["risk"] == "high"
    assert best_option(list(opts.values()))["action"] in {"switch_supplier", "reallocate_stock"}


def test_deterministic_and_complete(db_factory, holdout):
    s = holdout["HS05"]
    con = db_factory(s["day0"])
    a = simulate_options(con, build_state(con, day0=s["day0"], **holdout_context(s)))
    b = simulate_options(con, build_state(con, day0=s["day0"], **holdout_context(s)))
    assert a == b
    assert [o["action"] for o in a][:len(SIMULATED)] == list(SIMULATED)
    assert {o["action"] for o in a} == set(TAXONOMY)
    unsupported = {o["action"]: o for o in a if not o["supported"]}
    assert set(unsupported) == {"build_safety_stock", "renegotiate", "reduce_allocation"}
    assert all("cost" not in o for o in unsupported.values())


def test_simulate_action_single(db_factory, holdout):
    s = holdout["HS05"]
    con = db_factory(s["day0"])
    out = simulate_action(con, "switch_supplier", day0=s["day0"], **holdout_context(s))
    assert out["action"] == "switch_supplier" and out["feasible"] is True
    with pytest.raises(SimulationError):
        simulate_action(con, "teleport", day0=s["day0"], **holdout_context(s))


@pytest.mark.parametrize("kwargs,match", [
    (dict(rpo_id="RPO016183", run_ids=["PR0016522"], day0="2025-01-06"), "no date revision"),
    (dict(rpo_id="RPO024268", run_ids=["PR0016522"], day0="2025-01-13"), "not found"),
    (dict(rpo_id="RPO016183", run_ids=[], day0="2025-01-13"), "production_run_id"),
    (dict(rpo_id="RPO016183", run_ids=["PR9999999"], day0="2025-01-13"), "not found"),
    (dict(rpo_id="RPO016183", run_ids=["PR0016522"], day0="2025-01-13", rm_id="RM0001"), "not on"),
])
def test_build_state_errors(db_factory, kwargs, match):
    con = db_factory("2025-01-13")
    with pytest.raises(SimulationError, match=match):
        build_state(con, **kwargs)
