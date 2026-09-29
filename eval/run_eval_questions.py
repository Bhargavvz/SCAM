"""Run the QA agent on eval_questions.jsonl, respecting each question's as_of_date, and score it."""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from agent.agent_core import build_agent
from agent.card import QAResult
from agent.config import ROOT, load_settings
from agent.corpus import as_of_end, load_index
from agent.setup_data import ensure_db
from agent.sql_tools import record_exists
from eval.scorecard import write_scorecard
from eval.scoring import CORRECT_THRESHOLD, answer_score, citation_pr, llm_judge, select_questions

_RECORD_ID = re.compile(r"\b(?:DECL|CMTL|RPOL|RPO|REV|EVT|DEC|CMT|NEG|CTR|CAT|POL|PR|PO|RT|SUP|RM|IP)\d+\b|\b[PW]\d{2,3}\b")


def hindsight_answer(agent, q: dict) -> QAResult:
    """The build prompt's eval strategy, as-of safe: recall(mid) for single-hop fact recall, reflect(high) otherwise
    (reflect falls back to local synthesis over as-of-filtered recall when native reflect could see later memory)."""
    t0 = time.monotonic()
    agent.llm.usage = type(agent.llm.usage)()
    if q["type"] == "fact_recall" and q["hop_count"] == 1:
        r = agent.memory.recall(q["question"], q["as_of_date"], budget="mid")
        answer, docs = "\n".join(h.text for h in r.hits[:10]), r.doc_ids()
    else:
        r = agent.memory.reflect(q["question"], q["as_of_date"], budget="high")
        answer, docs = r.text or "", r.doc_ids
    con = agent._connect(q["as_of_date"])
    try:
        recs = [x for x in dict.fromkeys(_RECORD_ID.findall(answer)) if record_exists(con, x)]
    finally:
        con.close()
    return QAResult(question=q["question"], as_of=q["as_of_date"], answer=answer, cited_doc_ids=docs,
                    cited_record_ids=recs, usage=agent.llm.usage.to_dict(), latency_s=round(time.monotonic() - t0, 2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--per-type", type=int, default=5, help="questions sampled per type (default 5 -> 40 total)")
    ap.add_argument("--all", action="store_true", help="run all 241 questions")
    ap.add_argument("--types", nargs="*", help="restrict to these question types")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--judge", action="store_true", help="also grade with an LLM judge; correct = judge verdict")
    ap.add_argument("--mode", choices=["agent", "hindsight"], default="agent",
                    help="agent = QA tool loop; hindsight = the build prompt's strategy (recall budget=mid for "
                         "single-hop fact_recall, reflect budget=high otherwise), still as-of filtered")
    ap.add_argument("--out", default=str(ROOT / "eval" / "out"))
    a = ap.parse_args(argv)

    settings = load_settings()
    ensure_db(settings)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    live = out / "eval_live.sqlite"  # fresh and empty: runtime decisions from demos are invisible
    live.unlink(missing_ok=True)
    agent = build_agent(settings, live_db_path=live)
    index = load_index(settings.corpus_path)
    qs = [json.loads(l) for l in (settings.dataset_dir / "eval_questions.jsonl").open()]
    selected = qs if a.all else select_questions(qs, a.per_type, a.seed, a.types)
    results_path = out / ("questions_results.jsonl" if a.mode == "agent" else "questions_results_hindsight.jsonl")
    results_path.write_text("")
    for i, q in enumerate(selected, 1):
        row = {"question_id": q["question_id"], "type": q["type"], "hop_count": q["hop_count"], "mode": a.mode,
               "as_of": q["as_of_date"], "gold": q["gold_answer"]}
        try:
            res = (agent.answer_question(q["question"], q["as_of_date"]) if a.mode == "agent"
                   else hindsight_answer(agent, q))
        except Exception as ex:  # harness: one failed question must not abort the run; it is recorded and scored wrong
            row.update(error=f"{type(ex).__name__}: {ex}", score=0.0, correct=False, answer="", latency_s=None, usage={},
                       doc_precision=None, doc_recall=0.0, record_precision=None, record_recall=0.0,
                       future_doc_citations=[])
        else:
            score = answer_score(q["gold_answer"], res.answer)
            judged = llm_judge(agent.llm, q["question"], q["gold_answer"], res.answer) if a.judge else None
            dp, dr = citation_pr(res.cited_doc_ids, q["gold_doc_ids"])
            rp, rr = citation_pr(res.cited_record_ids, q["gold_record_ids"])
            end = as_of_end(q["as_of_date"])
            leaks = [d for d in res.cited_doc_ids if d in index.by_id and index.by_id[d].timestamp > end]
            row.update(error=None, answer=res.answer, score=round(score, 3), judged=judged,
                       correct=judged if a.judge else score >= CORRECT_THRESHOLD,
                       doc_precision=dp, doc_recall=dr, record_precision=rp, record_recall=rr,
                       cited_doc_ids=res.cited_doc_ids, cited_record_ids=res.cited_record_ids,
                       future_doc_citations=leaks, latency_s=res.latency_s, usage=res.usage)
        with results_path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"[{i}/{len(selected)}] {q['question_id']} {q['type']:<27} score={row['score']} "
              f"correct={row['correct']} {row['error'] or ''}")
    print(f"scorecard: {write_scorecard(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
