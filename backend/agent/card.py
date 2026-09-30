"""Output records of the agent."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class DecisionCard:
    run_id: str
    as_of: str
    report: str
    entities: dict
    situation_summary: str = ""
    scenario_state: dict | None = None
    options: list[dict] = field(default_factory=list)
    simulator_best_action: str | None = None
    recommended_action: str | None = None
    deviates_from_simulator_best: bool = False
    confidence: str = "low"
    rationale: str = ""
    key_reasons: list[str] = field(default_factory=list)
    precedents: list[dict] = field(default_factory=list)
    precedent_assessments: list[dict] = field(default_factory=list)
    related_event_ids: list[str] = field(default_factory=list)
    open_commitments: list[dict] = field(default_factory=list)
    cited_doc_ids: list[str] = field(default_factory=list)
    cited_record_ids: list[str] = field(default_factory=list)
    unverifiable_citations: list[str] = field(default_factory=list)
    ungrounded_claims: list[str] = field(default_factory=list)
    guardrail_events: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reflect_mode: str | None = None
    reflection: str | None = None
    memory_hits: int = 0
    dropped_future_hits: int = 0
    writeback: dict | None = None
    mock_actions: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    trace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QAResult:
    question: str
    as_of: str
    answer: str = ""
    cited_doc_ids: list[str] = field(default_factory=list)
    cited_record_ids: list[str] = field(default_factory=list)
    unverifiable_citations: list[str] = field(default_factory=list)
    confidence: str = "low"
    memory_hits: int = 0
    dropped_future_hits: int = 0
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    trace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
