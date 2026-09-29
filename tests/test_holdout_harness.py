from agent.card import DecisionCard
from agent.scenarios import load_holdout
from eval.run_holdout_scenarios import score_card


def test_score_card_trap_and_recalls(base_settings):
    s = load_holdout(base_settings)["HS05"]
    card = DecisionCard(run_id="r", as_of=s["day0"], report="", entities={}, recommended_action="switch_supplier",
                        options=[{**a, "breaches_commitments": []} for a in s["candidate_actions"]],
                        precedents=[{"event_id": "EVT02501"}], related_event_ids=["EVT02515"],
                        open_commitments=[{"commitment_id": c["commitment_id"]} for c in s["open_commitments"][:3]])
    r = score_card(card, s)
    assert r["correct"] is True and r["trap"] is True and r["trap_pass"] is True
    assert r["precedent_recall"] == round(2 / 3, 3)
    assert r["commitments_recall"] == 0.5
    assert r["parity_mismatches"] == []
    card.recommended_action = "accept_delay"
    assert score_card(card, s)["trap_pass"] is False
