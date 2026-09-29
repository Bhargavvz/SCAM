"""The seven capabilities from the build prompt, routed from a natural-language question.

Each handler makes one Hindsight reflect call (budget / response_schema as the prompt specifies). The query carries
deterministic ground truth from the as-of SQL views (delays by month, scorecards, commitments, contracts,
negotiations, decision accuracy). Afterwards the answer is checked:
- record ids in it must exist;
- doc ids must be visible memory docs;
- any question about switching suppliers or cancelling a PO gets the open commitments attached.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field

from agent import sql_tools
from agent.parsing import parse_report

CAPABILITIES = ("supplier_reliability", "commitments", "decision_precedent", "exceptions", "quality_root_cause",
                "negotiation", "decision_outcome_learning", "general")
ROUTES = [  # first match wins
    ("commitments", r"\bcommitments?\b|\bobligations?\b|\bowe[sd]?\b"),
    ("negotiation", r"negotiat|\blevers?\b|\bconce(?:ssion|ded)"),
    ("decision_outcome_learning", r"\baccura(?:te|cy)\b|\bpredicted\b|\bprojections?\b|\bunderestimat|\boverestimat"),
    ("quality_root_cause", r"\bdefects?\b|\bcorrosion\b|\bquality\b|\broot cause\b|\breject|\bcorrective\b"),
    ("exceptions", r"\bexceptions?\b|\bpartial shipments?\b|\bpolicy review\b"),
    ("decision_precedent", r"\blast time\b|\bprecedents?\b|\bsimilar situation|\bdeciding between\b"),
    ("supplier_reliability", r"\btrack record\b|\breliab|\bon[- ]time\b|\blate\b|\bdelay|\btrust their\b"),
]
BUDGET = {"supplier_reliability": "high", "commitments": "mid", "decision_precedent": "high", "exceptions": "mid",
          "quality_root_cause": "high", "negotiation": "high", "decision_outcome_learning": "high", "general": "mid"}
INSTRUCTIONS = {
    "supplier_reliability": "Give the complete delivery and failure history: what went wrong, when, what action we "
                            "took, whether it worked, and what to do now. Flag any recurring pattern (seasonal, "
                            "size-dependent, lead-time trend).",
    "commitments": "List every open commitment with this counterparty: who promised what, by when, the penalty or "
                   "credit for breach, and current status. Say which ones a supplier switch or PO cancellation would "
                   "breach.",
    "decision_precedent": "Find the most similar past situations. For each give situation, decision, reasoning, "
                          "outcome, lesson and how today's conditions differ. Then recommend, with the predicted "
                          "outcome and a confidence level.",
    "exceptions": "Find past exceptions to this policy: how many, when, the justification, who approved them and the "
                  "outcome. Say whether the frequency warrants a policy review.",
    "quality_root_cause": "Has this issue occurred before? Give the root cause, the corrective action promised, "
                          "whether it was verified, and whether the problem recurred.",
    "negotiation": "Summarize every negotiation with this supplier: our ask, their offer, concessions each way and "
                   "the outcome. Then say which levers worked, what the supplier values and what to lead with next.",
    "decision_outcome_learning": "Compare predicted and actual outcomes for these past decisions: prediction "
                                 "accuracy, systematic biases (cost, stockout days) and an updated confidence level.",
    "general": "Answer from memory.",
}
COMMON = ("Cite document dates and record ids for every claim. Distinguish facts (documents) from observations "
          "(consolidated patterns). If memory holds no evidence for something, say 'No record found' instead of "
          "guessing.")
_STR = {"type": "string"}
PRECEDENT_SCHEMA = {"type": "object", "properties": {
    "precedents": {"type": "array", "items": {"type": "object", "properties": {
        "decision_id": _STR, "situation": _STR, "decision": _STR, "outcome": _STR, "how_today_differs": _STR},
        "required": ["decision_id", "situation", "decision", "outcome", "how_today_differs"]}},
    "recommendation": _STR, "predicted_outcome": _STR,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
    "required": ["precedents", "recommendation", "predicted_outcome", "confidence"]}
OUTCOME_SCHEMA = {"type": "object", "properties": {
    "decision_type": _STR, "decisions_reviewed": {"type": "array", "items": _STR}, "prediction_accuracy": _STR,
    "systematic_biases": {"type": "array", "items": _STR}, "updated_confidence": _STR,
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
    "required": ["decision_type", "decisions_reviewed", "prediction_accuracy", "systematic_biases",
                 "updated_confidence", "confidence"]}
SCHEMAS = {"decision_precedent": PRECEDENT_SCHEMA, "decision_outcome_learning": OUTCOME_SCHEMA}
_SWITCH_OR_CANCEL = re.compile(r"\bswitch\w*|\bcancel\w*|\bbackup supplier|\balternat\w* supplier", re.I)
_ACTION_MENTIONS = {
    "switch_supplier": r"switch\w*(?: to)?(?: a| the)?(?: cheaper| backup| alternat\w*)? supplier",
    "expedite": r"\bexpedit", "cancel_po": r"\bcancel", "reallocate_stock": r"\btransfer|\breallocat",
    "substitute_rm": r"\bsubstitut", "accept_delay": r"\baccept\w* (?:the )?delay",
    "build_safety_stock": r"\bsafety stock", "renegotiate": r"\brenegotiat", "reduce_allocation": r"\breduc\w* allocation",
}
_RECORD_ID = re.compile(r"\b(?:DECL|CMTL|RPOL|RPO|REV|EVT|DEC|CMT|NEG|CTR|CAT|POL|PR|PO|RT|SUP|RM|IP)\d+\b|\b[PW]\d{2,3}\b")
_DOC_ID = re.compile(r"\bDOC\d{6}\b")
MAX_GROUND_TRUTH_CHARS = 6000


@dataclass
class CapabilityAnswer:
    question: str
    capability: str
    as_of: str
    budget: str
    reflect_mode: str | None = None
    answer: str = ""
    structured: dict | None = None
    ground_truth: dict = field(default_factory=dict)
    cited_doc_ids: list[str] = field(default_factory=list)
    cited_record_ids: list[str] = field(default_factory=list)
    unverifiable_citations: list[str] = field(default_factory=list)
    commitments_checked: bool | None = None
    warnings: list[str] = field(default_factory=list)
    confidence: str = "low"
    latency_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def route(question: str) -> str:
    for cap, pattern in ROUTES:
        if re.search(pattern, question, re.I):
            return cap
    return "general"


def _q(con, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _delay_by_promised_month(con, supplier_id: str) -> list[dict]:
    return _q(con, """
        SELECT strftime('%m', promised_at) AS month, COUNT(*) AS n_received,
               ROUND(AVG(MAX(0, julianday(received_at) - julianday(promised_at))), 1) AS avg_delay_days,
               ROUND(AVG(julianday(received_at) > julianday(promised_at)), 2) AS late_share
        FROM rm_purchase_orders WHERE supplier_id = ? AND received_at IS NOT NULL
        GROUP BY month ORDER BY month""", (supplier_id,))


def _lead_time_by_month(con, supplier_id: str) -> list[dict]:
    return _q(con, """
        SELECT substr(ordered_at, 1, 7) AS ordered_month, COUNT(*) AS n,
               ROUND(AVG(julianday(received_at) - julianday(ordered_at)), 1) AS avg_lead_days
        FROM rm_purchase_orders WHERE supplier_id = ? AND received_at IS NOT NULL
        GROUP BY ordered_month ORDER BY ordered_month DESC LIMIT 12""", (supplier_id,))


def _decision_accuracy(con, types: list[str]) -> tuple[list[dict], list[dict]]:
    where = "outcome_label IS NOT NULL AND source = 'historical'"
    params: tuple = ()
    if types:
        where += f" AND decision_type IN ({','.join('?' * len(types))})"
        params = tuple(types)
    summary = _q(con, f"""
        SELECT decision_type, COUNT(*) AS n,
               ROUND(SUM(actual_cost) / NULLIF(SUM(expected_cost), 0), 2) AS actual_to_expected_cost,
               ROUND(AVG(actual_stockout_days - expected_stockout_days), 2) AS extra_stockout_days,
               SUM(outcome_label = 'success') AS success, SUM(outcome_label = 'partial') AS partial,
               SUM(outcome_label = 'failed') AS failed
        FROM decisions WHERE {where} GROUP BY decision_type ORDER BY decision_type""", params)
    examples = _q(con, f"""
        SELECT decision_id, decision_type, decided_at, expected_cost, actual_cost, expected_stockout_days,
               actual_stockout_days, outcome_label, outcome_attribution
        FROM decisions WHERE {where} ORDER BY decided_at DESC LIMIT 10""", params)
    return summary, examples


def _ground_truth(con, cap: str, question: str, as_of: str) -> tuple[dict, bool | None, list[str]]:
    ents = parse_report(question)
    sup = ents.supplier_ids[0] if ents.supplier_ids else None
    rm = ents.rm_ids[0] if ents.rm_ids else None
    parties = ents.supplier_ids + ents.plant_ids
    gt: dict = {}
    warnings: list[str] = []
    if cap == "supplier_reliability" and sup:
        gt["delay_by_promised_month"] = _delay_by_promised_month(con, sup)
        gt["scorecard_recent"] = sql_tools.supplier_scorecard(con, sup, 6)
        gt["prior_decisions"] = sql_tools.prior_decisions(con, sup, rm, as_of)
    elif cap == "commitments" and parties:
        gt["open_commitments"] = sql_tools.open_commitments(con, parties, as_of)
        gt["contracts_in_force"] = _q(con, """SELECT contract_id, scope, valid_from, valid_to, price_terms, min_volume,
                                              penalty_clause, force_majeure_flag FROM contracts
                                              WHERE supplier_id = ? AND valid_from <= ? AND valid_to >= ?""",
                                      (sup, as_of, as_of)) if sup else []
    elif cap == "decision_precedent":
        gt["prior_decisions"] = (sql_tools.prior_decisions(con, sup, rm, as_of) if (sup or rm)
                                 else _decision_accuracy(con, [])[1])
    elif cap == "negotiation" and sup:
        gt["negotiations"] = _q(con, "SELECT * FROM negotiations WHERE supplier_id = ? ORDER BY started_at", (sup,))
        gt["lead_time_by_month"] = _lead_time_by_month(con, sup)
    elif cap == "decision_outcome_learning":
        types = [a for a, p in _ACTION_MENTIONS.items() if re.search(p, question, re.I)]
        gt["decision_accuracy"], gt["decision_examples"] = _decision_accuracy(con, types)
    checked = None
    if _SWITCH_OR_CANCEL.search(question):
        if parties:
            gt.setdefault("open_commitments", sql_tools.open_commitments(con, parties, as_of))
            checked = True
        else:
            checked = False
            warnings.append("The question involves a supplier switch or cancellation but names no supplier id; open "
                            "commitments could not be checked - name the supplier (SUPxxxx) to check them.")
    elif cap == "commitments":
        checked = bool(parties)
    return gt, checked, warnings


def _verify(con, memory, text: str, seen_docs: set[str], as_of: str) -> tuple[list[str], list[str], list[str]]:
    docs, recs, bad = [], [], []
    for d in dict.fromkeys(_DOC_ID.findall(text)):
        (docs if d in seen_docs or memory.get_document(d, as_of) else bad).append(d)
    for r in dict.fromkeys(_RECORD_ID.findall(text)):
        (recs if sql_tools.record_exists(con, r) else bad).append(r)
    return docs, recs, bad


def ask(agent, question: str, as_of: str | None = None) -> CapabilityAnswer:
    t0 = time.monotonic()
    as_of = as_of or agent.settings.default_as_of
    cap = route(question)
    ans = CapabilityAnswer(question=question, capability=cap, as_of=as_of, budget=BUDGET[cap])
    con = agent._connect(as_of)
    try:
        gt, ans.commitments_checked, ans.warnings = _ground_truth(con, cap, question, as_of)
        ans.ground_truth = gt
        facts = json.dumps(gt, default=str)[:MAX_GROUND_TRUTH_CHARS] if gt else "(none for this question)"
        query = (f"{question}\n\nTask: {INSTRUCTIONS[cap]} {COMMON}\nAs of: {as_of}.\n"
                 f"Authoritative database records (quote their ids):\n{facts}")
        try:
            refl = agent.memory.reflect(query, as_of, budget=ans.budget, response_schema=SCHEMAS.get(cap))
        except Exception as ex:  # any client/network error: report it instead of a raw traceback
            from agent.agent_core import MemoryUnavailable
            raise MemoryUnavailable(f"Hindsight reflect failed ({ex}); check HINDSIGHT_BASE_URL / "
                                    f"HINDSIGHT_BANK_ID") from ex
        ans.reflect_mode, ans.structured = refl.mode, refl.structured
        ans.answer = refl.text or ""
        text = ans.answer + (" " + json.dumps(refl.structured) if refl.structured else "")
        ans.cited_doc_ids, ans.cited_record_ids, ans.unverifiable_citations = _verify(
            con, agent.memory, text, set(refl.doc_ids), as_of)
    finally:
        con.close()
    n_cites = len(ans.cited_doc_ids) + len(ans.cited_record_ids)
    stated = (ans.structured or {}).get("confidence")
    if n_cites == 0:
        ans.confidence = "low"
        ans.warnings.append("The answer contains no evidence citations (document ids or record ids).")
    elif stated in ("low", "medium", "high"):
        ans.confidence = stated
    else:
        ans.confidence = "high" if n_cites >= 3 else "medium"
    ans.latency_s = round(time.monotonic() - t0, 2)
    return ans
