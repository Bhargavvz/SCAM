from demo.run_demo import check_expectations


def test_checks_pass_and_fail():
    card = {"recommended_action": "switch_supplier", "guardrail_events": ["Rejected cancel_po: would breach CMT00560"],
            "open_commitments": [{"commitment_id": "CMT00786"}],
            "precedents": [{"decision_type": "accept_delay", "applies_today": False, "source": "historical"}]}
    spec = {"recommended_action": "switch_supplier", "not_action": "cancel_po", "guardrail_mentions": "CMT00560",
            "commitments_flagged": ["CMT00786", "CMT00802"], "precedent_not_applicable": "accept_delay"}
    res = {c["check"]: c["ok"] for c in check_expectations(spec, {"card": card})}
    assert res == {"recommended_action": True, "not_action": True, "guardrail_mentions": True,
                   "commitments_flagged": False, "precedent_not_applicable": True}


def test_question_and_chain_checks():
    ans = {"answer": "Latest ETA 2023-06-08 (2023-06-22 superseded).", "cited_doc_ids": ["DOC000254"]}
    res = check_expectations({"answer_contains": ["2023-06-08"], "cites_doc": "DOC000254"}, {"answer": ans})
    assert all(c["ok"] for c in res)
    chain = {"cards": [{}, {"precedents": [{"source": "live", "decision_id": "DECL00001"}]}]}
    assert check_expectations({"second_has_live_precedent": True}, chain)[0]["ok"] is True


def test_capability_checks():
    cap = {"capability": "exceptions", "answer": "No record found of partial-shipment exceptions.",
           "cited_doc_ids": [], "cited_record_ids": [], "commitments_checked": None}
    res = {c["check"]: c["ok"] for c in check_expectations(
        {"capability": "exceptions", "grounded_or_declines": True, "has_citations": True}, {"capability": cap})}
    assert res == {"capability": True, "grounded_or_declines": True, "has_citations": False}
    cap2 = {"capability": "supplier_reliability", "answer": "Deliveries promised in November arrive late.",
            "cited_doc_ids": ["DOC000001"], "cited_record_ids": [], "commitments_checked": True}
    res2 = {c["check"]: c["ok"] for c in check_expectations(
        {"answer_matches": ["nov|dec", "late"], "commitments_checked": True}, {"capability": cap2})}
    assert res2 == {"answer_matches": True, "commitments_checked": True}
