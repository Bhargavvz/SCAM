from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.llm import LLM, LLMRefusal, Usage


def _resp(stop="end_turn", i=1000, o=100, cr=0, cw=0):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr,
                                                 cache_creation_input_tokens=cw))


class FakeClient:
    def __init__(self, resp):
        self.kw = None
        outer = self

        class _Msgs:
            def create(self, **kw):
                outer.kw, outer.path = kw, "messages"
                return resp

        class _BetaMsgs:
            def create(self, **kw):
                outer.kw, outer.path = kw, "beta"
                return resp

        self.messages = _Msgs()
        self.beta = SimpleNamespace(messages=_BetaMsgs())


def test_request_shape_with_fallback(settings):
    c = FakeClient(_resp())
    llm = LLM(replace(settings, llm_refusal_fallback="default"), client=c)
    llm.create(system="sys", messages=[{"role": "user", "content": "hi"}], tools=[{"name": "t"}], effort="low")
    assert c.path == "beta"
    assert c.kw["betas"] == ["server-side-fallback-2026-07-01"] and c.kw["fallbacks"] == "default"
    assert c.kw["output_config"] == {"effort": "low"}
    assert c.kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in c.kw and c.kw["tools"] == [{"name": "t"}]


def test_no_fallback_and_temperature_when_configured(settings):
    c = FakeClient(_resp())
    llm = LLM(replace(settings, llm_refusal_fallback=""), client=c)
    llm.create(system="s", messages=[], effort="high", temperature=0.2)
    assert c.path == "messages" and c.kw["temperature"] == 0.2 and "tools" not in c.kw


def test_usage_and_cost(settings):
    llm = LLM(replace(settings, llm_model="claude-opus-5"), client=FakeClient(_resp(i=1000, o=100, cr=2000, cw=400)))
    llm.create(system="s", messages=[])
    u = llm.usage.to_dict()
    assert u["calls"] == 1 and u["input_tokens"] == 1000 and u["cache_read_tokens"] == 2000
    # (1000*5 + 100*25 + 2000*5*0.1 + 400*5*1.25) / 1e6
    assert u["cost_usd"] == pytest.approx(0.0110)


def test_unknown_model_cost_is_none():
    u = Usage()
    u.add(SimpleNamespace(input_tokens=1, output_tokens=1), "some-future-model")
    assert u.cost_usd is None


def test_refusal_raises(settings):
    with pytest.raises(LLMRefusal):
        LLM(settings, client=FakeClient(_resp(stop="refusal"))).create(system="s", messages=[])


@pytest.mark.live
def test_live_round_trip(base_settings):
    llm = LLM(base_settings)
    r = llm.create(system="Reply with the single word OK.", messages=[{"role": "user", "content": "ping"}])
    assert any(b.type == "text" for b in r.content) and llm.usage.calls == 1
