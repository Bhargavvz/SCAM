import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from agent.llm import LLM, LLMRefusal, make_llm
from agent.llm_groq import GroqLLM


def _groq_settings(settings, **kw):
    return replace(settings, llm_provider="groq", llm_model="openai/gpt-oss-120b", groq_api_key="gsk_test",
                   groq_base_url="https://api.groq.test/openai/v1", **kw)


def _client(responses, seen):
    it = iter(responses)

    def handler(request: httpx.Request):
        seen.append(json.loads(request.content))
        status, body = next(it)
        return httpx.Response(status, json=body, headers={"retry-after": "0"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(message, finish="tool_calls", pt=100, ct=20):
    return 200, {"choices": [{"message": message, "finish_reason": finish}],
                 "usage": {"prompt_tokens": pt, "completion_tokens": ct}}


TOOL = {"name": "sql_query", "description": "d", "strict": True,
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                         "additionalProperties": False}}


def test_request_translation_and_tool_call_parsing(settings):
    seen = []
    llm = GroqLLM(_groq_settings(settings), http_client=_client([_ok(
        {"content": None, "tool_calls": [{"id": "call_1", "type": "function",
                                          "function": {"name": "sql_query", "arguments": "{\"query\": \"SELECT 1\"}"}}]})],
        seen))
    prior_assistant = [SimpleNamespace(type="text", text="thinking"),
                       SimpleNamespace(type="tool_use", id="call_0", name="sql_query", input={"query": "SELECT 2"})]
    messages = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": prior_assistant},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_0", "content": "{}",
                                              "is_error": True}]}]
    resp = llm.create(system="sys", messages=messages, tools=[TOOL], effort="xhigh", temperature=None)
    body = seen[0]
    assert body["model"] == "openai/gpt-oss-120b" and body["reasoning_effort"] == "high"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][2]["tool_calls"][0]["function"] == {"name": "sql_query", "arguments": "{\"query\": \"SELECT 2\"}"}
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "call_0", "content": "ERROR: {}"}
    assert body["tools"][0] == {"type": "function", "function": {"name": "sql_query", "description": "d",
                                                                  "parameters": TOOL["input_schema"]}}
    assert "temperature" not in body
    assert resp.stop_reason == "tool_use"
    [block] = resp.content
    assert (block.type, block.id, block.name, block.input) == ("tool_use", "call_1", "sql_query", {"query": "SELECT 1"})
    assert llm.usage.calls == 1 and llm.usage.input_tokens == 100 and llm.usage.cost_usd is None


def test_invalid_tool_arguments_are_dropped_not_crashing(settings):
    seen = []
    llm = GroqLLM(_groq_settings(settings), http_client=_client([_ok(
        {"content": "ok", "tool_calls": [{"id": "c", "type": "function",
                                          "function": {"name": "sql_query", "arguments": "{not json"}}]})], seen))
    resp = llm.create(system="s", messages=[{"role": "user", "content": "q"}], tools=[TOOL])
    assert [b.type for b in resp.content] == ["text", "text"]
    assert "invalid JSON" in resp.content[1].text


def test_retry_on_429_then_cost_with_configured_price(settings):
    seen = []
    llm = GroqLLM(_groq_settings(settings, llm_price_input_per_mtok=0.15, llm_price_output_per_mtok=0.6),
                  http_client=_client([(429, {"error": "rate"}), _ok({"content": "hi"}, finish="stop",
                                                                     pt=1_000_000, ct=1_000_000)], seen))
    resp = llm.create(system="s", messages=[{"role": "user", "content": "q"}], effort="low", temperature=0.2)
    assert len(seen) == 2 and seen[1]["temperature"] == 0.2 and seen[1]["reasoning_effort"] == "low"
    assert resp.stop_reason == "end_turn" and resp.content[0].text == "hi"
    assert llm.usage.cost_usd == pytest.approx(0.75)


def test_content_filter_is_refusal(settings):
    llm = GroqLLM(_groq_settings(settings), http_client=_client([_ok({"content": ""}, finish="content_filter")], []))
    with pytest.raises(LLMRefusal):
        llm.create(system="s", messages=[{"role": "user", "content": "q"}])


def test_make_llm_picks_provider(settings):
    assert isinstance(make_llm(_groq_settings(settings)), GroqLLM)
    assert isinstance(make_llm(replace(settings, llm_provider="anthropic"), client=object()), LLM)
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        make_llm(replace(_groq_settings(settings), groq_api_key=None))


def test_build_agent_uses_configured_provider(settings):
    from agent.agent_core import build_agent
    from tests.fakes import FakeMemory

    agent = build_agent(_groq_settings(settings), memory=FakeMemory())
    assert isinstance(agent.llm, GroqLLM)
