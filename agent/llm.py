"""Thin wrapper over the Anthropic Messages API: model, effort, prompt caching, refusal fallback and
usage / cost accounting in one place."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import anthropic

from agent.config import Settings

# USD per million tokens (input, output), Claude API price table cached 2026-06.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0), "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0), "claude-sonnet-4-6": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_READ_MULT, CACHE_WRITE_MULT = 0.1, 1.25
NO_EFFORT_MODELS = ("claude-haiku-4-5",)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMRefusal(RuntimeError):
    """The model (and any fallback) declined the request."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = 0.0

    def add(self, u, model: str, price: tuple[float, float] | None = None) -> None:
        i = getattr(u, "input_tokens", 0) or 0
        o = getattr(u, "output_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        self.calls += 1
        self.input_tokens += i
        self.output_tokens += o
        self.cache_read_tokens += cr
        self.cache_write_tokens += cw
        price = price or PRICES.get(model)
        if price is None or self.cost_usd is None:
            self.cost_usd = None
        else:
            pin, pout = price
            self.cost_usd += (i * pin + o * pout + cr * pin * CACHE_READ_MULT + cw * pin * CACHE_WRITE_MULT) / 1e6

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["cost_usd"] is not None:
            d["cost_usd"] = round(d["cost_usd"], 6)
        return d


class LLM:
    def __init__(self, settings: Settings, client=None):
        self.model = settings.llm_model
        self.max_tokens = settings.llm_max_tokens
        self.fallback = settings.llm_refusal_fallback
        self.client = client or anthropic.Anthropic()
        self.usage = Usage()

    def create(self, *, system: str, messages: list, tools: list | None = None, effort: str = "low",
               temperature: float | None = None):
        kw = {"model": self.model, "max_tokens": self.max_tokens, "messages": messages,
              "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]}
        if tools:
            kw["tools"] = tools
        if not self.model.startswith(NO_EFFORT_MODELS):
            kw["output_config"] = {"effort": effort}
        if temperature is not None:
            kw["temperature"] = temperature
        if self.fallback:
            resp = self.client.beta.messages.create(**kw, betas=[FALLBACK_BETA], fallbacks=self.fallback)
        else:
            resp = self.client.messages.create(**kw)
        self.usage.add(resp.usage, self.model)
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMRefusal(f"model declined the request ({getattr(details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("response hit max_tokens; raise LLM_MAX_TOKENS")
        return resp


def make_llm(settings: Settings, client=None):
    """LLM_PROVIDER=anthropic (default, Claude via the Anthropic API) or groq (OpenAI-compatible Groq API)."""
    if settings.llm_provider == "groq":
        from agent.llm_groq import GroqLLM

        if not settings.groq_api_key:
            raise ValueError("LLM_PROVIDER=groq but GROQ_API_KEY is empty - set it in .env")
        return GroqLLM(settings, http_client=client)
    return LLM(settings, client=client)
