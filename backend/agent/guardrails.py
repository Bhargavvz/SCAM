"""Deterministic checks applied to the LLM's recommendation and prose before they reach the card."""
from __future__ import annotations

import re

from agent.simulate import TAXONOMY
from agent.sql_tools import record_exists

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
_STOCKOUT_DAYS = re.compile(r"(\d+(?:\.\d+)?)\s+(?:stockout|stock-out|blocked)\s+days?", re.I)
_MONEY_WORDS = re.compile(r"(?<![\w$])(\d[\d,]*(?:\.\d+)?)\s*(k|K)?\s*(?:USD|dollars)\b|\$\s?(\d+(?:\.\d+)?)\s*[kK]\b")
_DAY_STOCKOUT = re.compile(r"(\d+(?:\.\d+)?)[- ]day\s+(?:stockout|stock-out|block)", re.I)


def action_violations(action: str, options: list[dict]) -> list[str]:
    if action not in TAXONOMY:
        return [f"{action!r} is not in the decision taxonomy {TAXONOMY}"]
    opt = next((o for o in options if o["action"] == action), None)
    if opt is None:
        return [f"{action} was not simulated for this disruption"]
    if not opt.get("supported", True):
        return [f"{action} is not modelled by the simulator, so its consequences cannot be quantified"]
    if not opt["feasible"]:
        return [f"{action} is infeasible: {opt.get('note', '')}"]
    if opt.get("breaches_commitments"):
        return [f"{action} would breach open commitment(s) {', '.join(opt['breaches_commitments'])}"]
    return []


def split_citations(con, doc_ids, record_ids, seen_doc_ids: set[str]):
    docs, recs, bad = [], [], []
    for d in dict.fromkeys(doc_ids):
        (docs if d in seen_doc_ids else bad).append(d)
    for r in dict.fromkeys(record_ids):
        (recs if record_exists(con, r) else bad).append(r)  # None (unrecognised id) is unverifiable
    return docs, recs, bad


def ungrounded_numbers(text: str, options: list[dict], extra_values=()) -> list[str]:
    feasible = [o for o in options if o.get("feasible")]
    money = {float(o[k]) for o in feasible for k in ("cost", "score") if o.get(k) is not None}
    money |= {float(v) for v in extra_values if v is not None}
    days = {float(o["stockout_days"]) for o in feasible}
    out = []
    for m in _MONEY.finditer(text):
        v = float(m.group(1).replace(",", ""))
        if not any(abs(v - x) <= 1 for x in money):
            out.append(m.group(0))
    for m in _MONEY_WORDS.finditer(text):  # "25,386 USD", "$25k", "3k dollars"
        num = m.group(1) or m.group(3)
        v = float(num.replace(",", "")) * (1000 if (m.group(2) or m.group(3)) else 1)
        if not any(abs(v - x) <= max(1, 0.05 * x if m.group(3) or m.group(2) else 1) for x in money):
            out.append(m.group(0))
    for rx in (_STOCKOUT_DAYS, _DAY_STOCKOUT):
        for m in rx.finditer(text):
            if float(m.group(1)) not in days:
                out.append(m.group(0))
    return out
