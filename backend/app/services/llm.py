"""Minimal Groq (OpenAI-compatible) chat client with rate-limit handling."""
from __future__ import annotations

import time

import httpx

from app.config import settings


class LLMError(RuntimeError):
    pass


def available() -> bool:
    return bool(settings.groq_api_key)


def chat(messages: list[dict], *, tools: list[dict] | None = None, max_tokens: int = 1500, temperature: float = 0.2) -> dict:
    """Returns the assistant message dict (may contain tool_calls) plus usage under '_usage'."""
    if not available():
        raise LLMError("GROQ_API_KEY is not set - AI narratives are unavailable; the analytics above are still exact")
    body = {"model": settings.groq_model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if "gpt-oss" in settings.groq_model:
        body["reasoning_effort"] = "low"
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    for attempt in range(3):
        try:
            r = httpx.post(f"{settings.groq_base_url}/chat/completions", json=body, timeout=90,
                           headers={"Authorization": f"Bearer {settings.groq_api_key}"})
        except httpx.HTTPError as ex:
            raise LLMError(f"LLM request failed: {ex}") from ex
        if r.status_code == 429 and attempt < 2:
            wait = float(r.headers.get("retry-after", "5") or 5)
            time.sleep(min(wait, 20))
            continue
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                msg = r.text
            raise LLMError(f"LLM error {r.status_code}: {msg[:300]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        msg["_usage"] = data.get("usage", {})
        return msg
    raise LLMError("LLM rate limit - try again in a minute")
