"""Optional --llm-narratives: provider-agnostic, cached, resumable rewriting of
template docs into more natural prose. Facts are locked: a rewrite is accepted
only if every ID, date and number of the template survives; otherwise the
template text is kept. Cache: <output>/_cache/llm/<sha256>.json (resumable).
Providers: anthropic | openai (API key from ANTHROPIC_API_KEY / OPENAI_API_KEY).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TOKENS = re.compile(r"\b(?:[A-Z]{1,4}\d{2,}|\d{4}-\d{2}-\d{2}|\d[\d,]*\.?\d*)\b")

PROMPT = """Rewrite this internal supply-chain note so it reads like a real {doc_type} written by a busy professional.
Rules: keep EVERY identifier, date and number exactly as written; do not add facts, names of people or companies;
60-250 words; plain text; keep the same meaning.

NOTE:
{content}"""


class Provider:
    def __init__(self, cfg: dict):
        self.name, self.model = cfg["provider"], cfg["model"]

    def complete(self, prompt: str) -> str:
        import httpx
        if self.name == "anthropic":
            r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60,
                           headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                           json={"model": self.model, "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json()["content"])
        if self.name == "openai":
            r = httpx.post("https://api.openai.com/v1/chat/completions", timeout=60, headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                           json={"model": self.model, "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        raise ValueError(f"unknown provider {self.name}")


def rewrite_docs(ctx, docs: list[dict]) -> None:
    lcfg = ctx.cfg["narratives"]["llm"]
    if lcfg["provider"] == "none":
        return
    prov = Provider(lcfg)
    cache = ctx.out / lcfg["cache_dir"]
    cache.mkdir(parents=True, exist_ok=True)

    def one(d):
        prompt = PROMPT.format(doc_type=d["doc_type"].replace("_", " "), content=d["content"])
        h = hashlib.sha256((lcfg["model"] + prompt).encode()).hexdigest()
        p = cache / f"{h}.json"
        if p.exists():
            out = json.loads(p.read_text())["text"]
        else:
            try:
                out = prov.complete(prompt)
            except Exception as ex:  # keep template on any provider error; rerun resumes
                return f"error: {ex}"
            p.write_text(json.dumps({"doc_id": d["doc_id"], "text": out}))
        need = set(TOKENS.findall(d["content"]))
        if need <= set(TOKENS.findall(out)) and 40 <= len(out.split()) <= 300:
            d["content"] = out.strip()
            return "ok"
        return "rejected"

    with ThreadPoolExecutor(max_workers=lcfg.get("max_concurrency", 4)) as ex:
        res = list(ex.map(one, [d for d in docs if not d["is_noise"]]))
    print(f"LLM rewrite: {res.count('ok')} accepted, {res.count('rejected')} kept template (fact check), {sum(r.startswith('error') for r in res)} errors")
