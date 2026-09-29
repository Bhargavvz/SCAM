import json
from dataclasses import replace

import httpx
import pytest

from agent import llm_groq
from agent.agent_core import DecisionAgent
from agent.llm import LLMUnavailable
from agent.llm_groq import GroqLLM, estimate_tokens
from agent.scenarios import holdout_context, load_holdout
from tests.fakes import FakeLLM, FakeMemory, MemoryHit, response, submission, text, tool_use


def _hits(n=40):
    return [MemoryHit("memory fact about SUP0247 and RPO023030 " * 12, "world", f"DOC{i:06d}", "2025-09-01",
                      "decision_memo", "corpus") for i in range(1, n + 1)]


def _first_request_tokens(settings, compact):
    s = replace(settings, llm_compact=compact, llm_max_tokens=2000)
    llm = FakeLLM([response(tool_use("submit_recommendation", submission("switch_supplier"))),
                   response(text("ok"), stop="end_turn")])
    hs05 = load_holdout(settings)["HS05"]
    from agent.agent_core import DECIDE_TOOLS
    DecisionAgent(s, llm, FakeMemory(_hits()), s.live_db_path).run(
        hs05["day0_report"], as_of=hs05["day0"], context=holdout_context(hs05))
    call = llm.calls[0]
    body = {"messages": [{"role": "system", "content": call["system"]}] + call["messages"],
            "tools": call["tools"], "max_completion_tokens": 2000}
    return estimate_tokens(json.loads(json.dumps(body, default=str)))


def test_compact_first_request_fits_free_tier(settings):
    compact, full = _first_request_tokens(settings, True), _first_request_tokens(settings, False)
    assert compact <= 7000, compact
    assert full > compact


def test_compact_truncates_tool_results(settings):
    s = replace(settings, llm_compact=True)
    hs05 = load_holdout(settings)["HS05"]
    llm = FakeLLM([response(tool_use("sql_query", {"query": "SELECT * FROM disruption_events"})),
                   response(tool_use("submit_recommendation", submission("switch_supplier"))),
                   response(text("ok"), stop="end_turn")])
    DecisionAgent(s, llm, FakeMemory(), s.live_db_path).run(hs05["day0_report"], as_of=hs05["day0"],
                                                           context=holdout_context(hs05))
    result = llm.calls[1]["messages"][-1]["content"][0]["content"]
    assert len(result) <= 1600 and result.endswith("[truncated]")


def _groq(settings, handler):
    s = replace(settings, llm_provider="groq", llm_model="openai/gpt-oss-120b", groq_api_key="k",
                groq_base_url="https://api.groq.test/openai/v1", llm_tokens_per_minute=8000, llm_max_tokens=2000)
    return GroqLLM(s, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_pacing_waits_for_token_reset(settings, monkeypatch):
    slept = []
    monkeypatch.setattr(llm_groq.time, "sleep", lambda s: slept.append(s))

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
                              headers={"x-ratelimit-remaining-tokens": "100", "x-ratelimit-reset-tokens": "7.5s"})

    llm = _groq(settings, handler)
    llm.create(system="s", messages=[{"role": "user", "content": "q"}])
    assert slept == []
    llm.create(system="s", messages=[{"role": "user", "content": "q"}])  # needs ~2000 > 100 remaining
    assert slept and 7.0 <= slept[0] <= 8.5


def test_request_larger_than_tpm_fails_fast(settings):
    sent = []
    llm = _groq(settings, lambda r: sent.append(r) or httpx.Response(200, json={}))
    with pytest.raises(LLMUnavailable, match="tokens per minute"):
        llm.create(system="s", messages=[{"role": "user", "content": "x " * 30000}])
    assert sent == []
