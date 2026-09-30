"""Groq adapter (OpenAI-compatible chat completions) with the same interface as agent.llm.LLM.

The agent core speaks Anthropic-style messages (content blocks, tool_use / tool_result); this adapter translates
them to OpenAI chat format and back, so agent_core, capabilities and the eval harness run unchanged.
Groq does not enforce strict tool schemas, so malformed tool arguments are turned into a text note instead of
a tool call (the agent loop then asks the model to try again)."""
from __future__ import annotations

import json
import math
import re
import time
from types import SimpleNamespace

import httpx

from agent.config import Settings
from agent.llm import LLMRefusal, LLMUnavailable, Usage

_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
_STOP = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4


def estimate_tokens(body: dict) -> int:
    """Conservative size of a request as Groq's per-minute limit counts it: prompt + reserved completion."""
    return math.ceil(len(json.dumps(body, default=str)) / 3.5) + int(body.get("max_completion_tokens") or 0)


def _seconds(value: str | None) -> float:
    """Groq reset headers look like '7.66s', '1m2.5s' or '120ms'."""
    if not value:
        return 0.0
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|m|s|h)", value):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total


def _get(block, name):
    return block.get(name) if isinstance(block, dict) else getattr(block, name, None)


def _to_openai_messages(system: str, messages: list) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = "".join(_get(b, "text") or "" for b in content if _get(b, "type") == "text")
            calls = [{"id": _get(b, "id"), "type": "function",
                      "function": {"name": _get(b, "name"), "arguments": json.dumps(_get(b, "input"))}}
                     for b in content if _get(b, "type") == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        for b in content:  # user turn made of tool results (and possibly text)
            if _get(b, "type") == "tool_result":
                body = _get(b, "content")
                body = body if isinstance(body, str) else json.dumps(body)
                out.append({"role": "tool", "tool_call_id": _get(b, "tool_use_id"),
                            "content": ("ERROR: " + body) if _get(b, "is_error") else body})
            elif _get(b, "type") == "text":
                out.append({"role": "user", "content": _get(b, "text")})
    return out


def _to_openai_tools(tools: list) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t["input_schema"]}} for t in tools]


class GroqLLM:
    def __init__(self, settings: Settings, http_client: httpx.Client | None = None):
        self.model = settings.llm_model
        self.max_tokens = settings.llm_max_tokens
        self.url = settings.groq_base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
        self.http = http_client or httpx.Client(timeout=180)
        pin, pout = settings.llm_price_input_per_mtok, settings.llm_price_output_per_mtok
        self.price = (pin, pout) if pin is not None and pout is not None else None
        self.usage = Usage()
        self.tpm = settings.llm_tokens_per_minute
        self._remaining: int | None = None
        self._reset_at = 0.0

    def _pace(self, body: dict) -> None:
        need = estimate_tokens(body)
        if self.tpm and need > self.tpm:
            raise LLMUnavailable(f"request of ~{need} tokens exceeds the {self.tpm} tokens per minute limit; "
                                 f"set LLM_COMPACT=1 or lower LLM_MAX_TOKENS")
        wait = self._reset_at - time.monotonic()
        if self._remaining is not None and need > self._remaining and wait > 0:
            time.sleep(wait + 0.5)

    def _note_limits(self, r: httpx.Response) -> None:
        remaining = r.headers.get("x-ratelimit-remaining-tokens")
        if remaining is not None:
            self._remaining = int(float(remaining))
            self._reset_at = time.monotonic() + _seconds(r.headers.get("x-ratelimit-reset-tokens"))

    def _post(self, body: dict) -> dict:
        self._pace(body)
        for attempt in range(MAX_RETRIES + 1):
            r = self.http.post(self.url, json=body, headers=self.headers)
            self._note_limits(r)
            if r.status_code < 400:
                return r.json()
            if r.status_code not in RETRY_STATUSES or attempt == MAX_RETRIES:
                try:
                    detail = r.json().get("error", {}).get("message") or r.text
                except ValueError:
                    detail = r.text
                raise LLMUnavailable(f"Groq {r.status_code}: {detail[:400]}")
            wait = float(r.headers.get("retry-after") or 2 ** attempt)
            time.sleep(min(wait, 30))
        raise RuntimeError("unreachable")

    def create(self, *, system: str, messages: list, tools: list | None = None, effort: str = "low",
               temperature: float | None = None):
        body = {"model": self.model, "messages": _to_openai_messages(system, messages),
                "max_completion_tokens": self.max_tokens}
        if tools:
            body["tools"] = _to_openai_tools(tools)
        if self.model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = _EFFORT.get(effort, "medium")
        if temperature is not None:
            body["temperature"] = temperature
        data = self._post(body)
        choice = data["choices"][0]
        msg = choice["message"]
        blocks = []
        if msg.get("content"):
            blocks.append(SimpleNamespace(type="text", text=msg["content"]))
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                blocks.append(SimpleNamespace(type="text", text=f"(tool call {fn.get('name')} dropped: invalid JSON "
                                                                f"arguments)"))
                continue
            blocks.append(SimpleNamespace(type="tool_use", id=tc["id"], name=fn["name"], input=args))
        u = data.get("usage") or {}
        usage = SimpleNamespace(input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0),
                                cache_read_input_tokens=0, cache_creation_input_tokens=0)
        self.usage.add(usage, self.model, price=self.price)
        stop = _STOP.get(choice.get("finish_reason"), "end_turn")
        if stop == "tool_use" and not any(b.type == "tool_use" for b in blocks):
            stop = "end_turn"
        resp = SimpleNamespace(content=blocks, stop_reason=stop, usage=usage)
        if stop == "refusal":
            raise LLMRefusal("model output was blocked by the provider's content filter")
        if stop == "max_tokens":
            raise RuntimeError("response hit max_tokens; raise LLM_MAX_TOKENS")
        return resp
