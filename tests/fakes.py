"""Deterministic stand-ins for Claude and Hindsight used by agent tests."""
from __future__ import annotations

import itertools
from types import SimpleNamespace

from agent.hindsight_tools import MemoryHit, RecallResult, ReflectResult  # noqa: F401 (re-exported for tests)
from agent.llm import Usage

_ids = itertools.count(1)


def tool_use(name, input):
    return SimpleNamespace(type="tool_use", id=f"tu_{next(_ids)}", name=name, input=input)


def text(t):
    return SimpleNamespace(type="text", text=t)


def response(*blocks, stop="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


def submission(action, **over):
    base = {"situation_summary": "summary", "recommended_action": action, "key_reasons": ["cheapest safe option"],
            "precedent_assessments": [], "commitments_considered": [], "cited_doc_ids": [], "cited_record_ids": [],
            "ungrounded_claims": [], "confidence": "medium"}
    return {**base, **over}


class FakeLLM:
    def __init__(self, script):
        self.script, self.calls, self.usage, self.model = list(script), [], Usage(), "fake-model"

    def create(self, **kw):
        # snapshot messages: the agent mutates the list after the call
        self.calls.append({**kw, "messages": list(kw["messages"])})
        if not self.script:
            raise AssertionError("FakeLLM script exhausted")
        resp = self.script.pop(0)
        self.usage.add(resp.usage, self.model)
        return resp


class FakeMemory:
    def __init__(self, hits=(), fail=False, reflect_text="fake reflection"):
        self.hits, self.fail, self.recalls, self.retained = list(hits), fail, [], []
        self.reflect_text = reflect_text
        self.reflects = []

    def recall(self, query, as_of, *, window_days=None, budget="mid", max_tokens=4096):
        if self.fail:
            raise ConnectionError("connection refused")
        self.recalls.append((query, as_of, window_days))
        return RecallResult(query, as_of, list(self.hits), len(self.hits), 0, 0)

    def reflect(self, question, as_of, *, budget="mid", response_schema=None):
        self.reflects.append((question, as_of, budget, response_schema))
        return ReflectResult(question, as_of, "local_filtered", self.reflect_text,
                             sorted({h.doc_id for h in self.hits if h.doc_id}), list(self.hits),
                             structured={"confidence": "medium"} if response_schema else None)

    def get_document(self, doc_id, as_of):
        return None

    def retain_experience(self, **kw):
        self.retained.append(kw)
        return "ok"


def corpus_hit(doc_id, date, text="memory fact"):
    return MemoryHit(text, "world", doc_id, date, "decision_memo", "corpus")
