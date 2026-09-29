import json

import pytest

from eval.scorecard import summarize_holdout, summarize_questions, write_scorecard
from eval.scoring import answer_score, citation_pr, key_facts, select_questions


def test_key_facts_extracts_ids_dates_numbers_and_words():
    f = key_facts("Expected 2 stockout days / $438; actual 3 days / $656 - partial. DEC00021 on 2023-04-14.")
    assert {"DEC00021", "2023-04-14", "#2", "#438", "#3", "#656", "@partial"} <= f


def test_answer_score_full_and_partial():
    gold = "2023-06-08 after the expedite (supersedes 2023-06-22)."
    assert answer_score(gold, "Latest ETA is 2023-06-08 (the 2023-06-22 date was superseded by the expedite).") == 1.0
    # key facts: 2023-06-08, 2023-06-22, @expedite -> only the stale date matches
    assert answer_score(gold, "The ETA is 2023-06-22.") == pytest.approx(1 / 3)
    assert answer_score("switch supplier + cancel original PO (DEC00006)", "They chose switch_supplier (DEC00006) and cancel_po.") == 1.0


def test_citation_pr():
    assert citation_pr(["A", "B"], ["B", "C"]) == (0.5, 0.5)
    assert citation_pr([], ["B"]) == (None, 0.0)
    assert citation_pr(["A"], []) == (0.0, None)


def test_select_questions_is_stratified_and_deterministic():
    qs = [{"question_id": f"Q{i:03d}", "type": t} for i, t in enumerate(["a", "b"] * 10)]
    s1, s2 = select_questions(qs, 3, seed=1), select_questions(qs, 3, seed=1)
    assert s1 == s2 and len(s1) == 6 and {q["type"] for q in s1} == {"a", "b"}


def test_scorecard_files(tmp_path):
    q = [{"question_id": "Q1", "type": "temporal", "hop_count": 1, "score": 1.0, "correct": True, "doc_precision": 1.0, "doc_recall": 0.5,
          "record_precision": None, "record_recall": 1.0, "future_doc_citations": [], "latency_s": 2.0,
          "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01}, "error": None}]
    h = [{"scenario_id": "HS05", "correct": True, "trap": True, "trap_pass": True, "precedent_recall": 1 / 3,
          "commitments_recall": 1.0, "parity_mismatches": [], "latency_s": 9.0,
          "usage": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.2}, "error": None}]
    (tmp_path / "questions_results.jsonl").write_text("\n".join(json.dumps(r) for r in q))
    (tmp_path / "holdout_results.jsonl").write_text("\n".join(json.dumps(r) for r in h))
    p = [{"pattern_id": "P01", "mental_models": True, "detected": True},
         {"pattern_id": "P01", "mental_models": False, "detected": False}]
    (tmp_path / "pattern_results.jsonl").write_text("\n".join(json.dumps(r) for r in p))
    s = summarize_questions(q)
    assert s["by_type"]["temporal"]["accuracy"] == 1.0 and s["by_hop_count"]["1"]["n"] == 1
    assert s["single_hop_fact_recall_accuracy"] is None  # no fact_recall rows
    assert summarize_holdout(h)["trap_accuracy"] == 1.0
    md = write_scorecard(tmp_path).read_text()
    assert "temporal" in md and "HS05" in md and "| hop_count |" in md and "1/12 with Mental Models" in md
    assert (tmp_path / "scorecard.json").exists()
