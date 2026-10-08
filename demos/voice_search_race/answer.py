"""Turn one lane's search results into one spoken sentence.

Uses the same OpenAI-compatible endpoint and key the voice agent uses
(LLM_API_KEY / LLM_BASE_URL / LLM_MODEL), so the four lanes are judged by the
same model with the same prompt. The latency is returned so the page can show
it separately: the LLM is not part of the search race.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

SYSTEM = (
    "You are the voice of a customer-service phone agent. Answer the caller's question in ONE short "
    "spoken sentence (max 30 words), using only the search results provided. If you name a source, it must be "
    "the site shown next to the result you used, said the way a person would say it; never invent one and never "
    "read a URL. This will be read aloud: plain words only, "
    "no lists, dashes, symbols, codes or long strings of numbers; say times and figures the way a person "
    "would on the phone. If the results do not contain the answer, say exactly: "
    "\"I couldn't find that just now.\" No preamble, no markdown."
)


async def spoken_answer(query: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    key = (os.environ.get("LLM_API_KEY") or "").strip()
    base = (os.environ.get("LLM_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL") or "openai/gpt-4o-mini"
    if not key:
        return {"answer": None, "llm_ms": 0, "error": "LLM_API_KEY not set"}

    context = "\n\n".join(
        f"[{i + 1}] {r.get('title', '')}\n{r.get('source', '')}\n{(r.get('snippet') or '')[:400]}"
        for i, r in enumerate(results[:5])
    ) or "(none)"

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 80,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": f'Caller asked: "{query}"\n\nSearch results:\n{context}'},
                    ],
                },
            )
        j = r.json()
        ms = round((time.perf_counter() - t0) * 1000)
        if r.status_code >= 400 or "choices" not in j:
            err = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error")
            return {"answer": None, "llm_ms": ms, "model": model, "error": f"LLM {r.status_code}: {(err or r.text)[:140]}"}
        text = (j["choices"][0].get("message") or {}).get("content", "").strip()
        return {"answer": text or None, "llm_ms": ms, "model": model}
    except Exception as e:
        return {"answer": None, "llm_ms": round((time.perf_counter() - t0) * 1000), "error": str(e)[:200]}
