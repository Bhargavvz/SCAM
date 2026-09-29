"""Pull entity ids and an optional proposed action out of a free-text disruption report."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

_IDS = {
    "supplier_ids": r"\bSUP\d{4}\b", "rm_ids": r"\bRM\d{4}\b", "plant_ids": r"\bP\d{2}\b",
    "rpo_ids": r"\bRPO\d+\b", "run_ids": r"\bPR\d+\b", "warehouse_ids": r"\bW\d{3}\b", "event_ids": r"\bEVT\d+\b",
}
_PROPOSAL_CUE = re.compile(r"\b(propos\w*|suggest\w*|recommend\w*|wants? to|plans? to)\b", re.I)
_ACTION_WORDS = [  # checked in order; first match wins
    ("cancel_po", r"\bcancel"), ("expedite", r"\bexpedit|\bair[- ]freight"),
    ("switch_supplier", r"\bswitch\w* supplier|\balternat\w* supplier|\bspot (?:buy|lot)"),
    ("substitute_rm", r"\bsubstitut"), ("reallocate_stock", r"\btransfer|\breallocat"),
    ("build_safety_stock", r"\bsafety stock"), ("renegotiate", r"\brenegotiat"),
    ("reduce_allocation", r"\breduce\w* allocation"), ("accept_delay", r"\baccept\w* the delay|\bwait for"),
]


@dataclass
class ReportEntities:
    report_date: str | None
    supplier_ids: list[str]
    rm_ids: list[str]
    plant_ids: list[str]
    rpo_ids: list[str]
    run_ids: list[str]
    warehouse_ids: list[str]
    event_ids: list[str]
    proposed_action: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _proposed_action(text: str) -> str | None:
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if not _PROPOSAL_CUE.search(sentence):
            continue
        for action, pattern in _ACTION_WORDS:
            if re.search(pattern, sentence, re.I):
                return action
    return None


def parse_report(text: str) -> ReportEntities:
    date = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    ids = {k: list(dict.fromkeys(re.findall(p, text))) for k, p in _IDS.items()}
    return ReportEntities(report_date=date.group(1) if date else None, proposed_action=_proposed_action(text), **ids)
