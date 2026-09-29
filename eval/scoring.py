"""Deterministic scoring of eval answers against gold answers and citations."""
from __future__ import annotations

import random
import re
from collections import defaultdict

CORRECT_THRESHOLD = 0.75
ACTIONS = ("accept_delay", "expedite", "switch_supplier", "reallocate_stock", "substitute_rm", "cancel_po",
           "build_safety_stock", "renegotiate", "reduce_allocation")
STATUS_WORDS = ("fulfilled", "breached", "renegotiated", "success", "partial", "failed")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_ID = re.compile(r"\b[A-Z]{1,4}\d{2,}\b")
_NUM = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")


def _num(s: str) -> str:
    v = float(s.replace("$", "").replace(",", ""))
    return "#" + (str(int(v)) if v == int(v) else f"{v:g}")


def _split(text: str) -> tuple[set, set, set]:
    dates = set(_DATE.findall(text))
    rest = _DATE.sub(" ", text)
    ids = set(_ID.findall(rest))
    rest = _ID.sub(" ", rest)
    nums = {_num(m) for m in _NUM.findall(rest) if re.search(r"\d", m)}
    return dates, ids, nums


def _words(text: str) -> set[str]:
    low = text.lower()
    out = {"@" + a for a in ACTIONS if a in low or a.replace("_", " ") in low}
    return out | {"@" + w for w in STATUS_WORDS if re.search(rf"\b{w}\b", low)}


def key_facts(gold: str) -> set[str]:
    dates, ids, nums = _split(gold)
    return dates | ids | nums | _words(gold)


def answer_score(gold: str, answer: str) -> float:
    facts = key_facts(gold)
    if not facts:
        g, a = set(re.findall(r"\w+", gold.lower())), set(re.findall(r"\w+", answer.lower()))
        return len(g & a) / len(g) if g else 0.0
    dates, ids, nums = _split(answer)
    found = (dates | ids | nums | _words(answer)) & facts
    return len(found) / len(facts)


def citation_pr(pred, gold) -> tuple[float | None, float | None]:
    p, g = set(pred), set(gold)
    hit = len(p & g)
    return (hit / len(p) if p else None, hit / len(g) if g else None)


def select_questions(questions: list[dict], per_type: int, seed: int = 7, types=None) -> list[dict]:
    by_type = defaultdict(list)
    for q in sorted(questions, key=lambda q: q["question_id"]):
        if types is None or q["type"] in types:
            by_type[q["type"]].append(q)
    rng = random.Random(seed)
    out = []
    for t in sorted(by_type):
        pool = by_type[t]
        out += sorted(rng.sample(pool, min(per_type, len(pool))), key=lambda q: q["question_id"])
    return out


def llm_judge(llm, question: str, gold: str, answer: str) -> bool:
    """Optional semantic check (--judge). First word of the reply must be CORRECT or INCORRECT."""
    resp = llm.create(system="You grade answers against a gold answer. Reply with CORRECT or INCORRECT as the first "
                             "word, then one short reason. Paraphrase is fine; missing or wrong ids, dates, numbers "
                             "or statuses are INCORRECT.",
                      messages=[{"role": "user", "content": f"Question: {question}\nGold: {gold}\nAnswer: {answer}"}],
                      effort="low")
    reply = "".join(b.text for b in resp.content if b.type == "text").strip().upper()
    return reply.startswith("CORRECT")
