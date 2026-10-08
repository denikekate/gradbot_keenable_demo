"""Four search APIs behind one interface, each timed around its own HTTP call.

Providers: keenable, perplexity, tavily, exa. Each has a voice-grade "fast"
tier (what a voice-agent builder would pick) and a "standard" tier:

    keenable    realtime     / pro
    perplexity  fast         / web
    tavily      ultra-fast   / basic
    exa         instant      / auto

``search(provider, query, tier)`` returns a normalised dict:
    {provider, tier, label, api_ms, cost_usd, vendor_ms?, error?, results: [{title,url,source,snippet,published}]}

``api_ms`` is measured here, around the vendor call only, so neither the browser
nor the voice pipeline is included. That is the number the race shows.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

TIMEOUT_S = 10.0
MAX_RESULTS = 5
PROVIDERS = ["keenable", "perplexity", "tavily", "exa"]

# List price per query, USD, used when the vendor does not report cost in the
# response (Exa and Tavily do). Check against each pricing page before a demo.
LIST_PRICE = {
    "keenable": {"fast": 0.001, "standard": 0.001},  # $1 / 1K
    "perplexity": {"fast": 0.001, "standard": 0.005},  # fast $1 / 1K, web $5 / 1K
    "tavily": {"fast": 0.008, "standard": 0.008},  # 1 credit at ~$0.008 pay-as-you-go
    "exa": {"fast": 0.005, "standard": 0.005},  # verify on exa.ai/pricing
}
LABEL = {
    "keenable": {"fast": "realtime", "standard": "pro"},
    "perplexity": {"fast": "fast", "standard": "web"},
    "tavily": {"fast": "ultra-fast", "standard": "basic"},
    "exa": {"fast": "instant", "standard": "auto"},
}


class ProviderError(RuntimeError):
    pass


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _row(title: str, url: str, snippet: str, published: str | None) -> dict[str, Any]:
    return {
        "title": (title or "Untitled").strip(),
        "url": url,
        "source": _domain(url),
        "snippet": (snippet or "").strip(),
        "published": published,
    }


async def _post(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        r = await client.post(url, json=body, headers=headers)
    if r.status_code >= 400:
        raise ProviderError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _key(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    if not v:
        raise ProviderError(f"{name} is not set")
    return v


# ---------- vendors ----------

async def _keenable(q: str, tier: str) -> dict[str, Any]:
    key = (os.environ.get("KEENABLE_API_KEY") or "").strip()
    if key:
        url, headers = "https://api.keenable.ai/v1/search", {"X-API-Key": key}
    else:  # keyless public endpoint, rate limited per IP
        url, headers = "https://api.keenable.ai/v1/search/public", {"X-Keenable-Title": "Voice search race demo"}
    j = await _post(url, headers, {
        "query": q,
        "mode": "realtime" if tier == "fast" else "pro",
        "max_results": MAX_RESULTS,
        "snippet_max_length": 300,
    })
    return {"results": [
        _row(r.get("title"), r.get("url", ""), r.get("snippet") or r.get("description") or "", r.get("published_at"))
        for r in j.get("results", []) if r.get("url")
    ]}


async def _perplexity(q: str, tier: str) -> dict[str, Any]:
    j = await _post("https://api.perplexity.ai/search", {"Authorization": f"Bearer {_key('PERPLEXITY_API_KEY')}"}, {
        "query": q,
        "max_results": MAX_RESULTS,
        "search_type": "fast" if tier == "fast" else "web",
        "max_tokens_per_page": 256,
    })
    return {"results": [
        _row(r.get("title"), r.get("url", ""), r.get("snippet") or "", r.get("date") or r.get("last_updated"))
        for r in j.get("results", []) if r.get("url")
    ]}


async def _tavily(q: str, tier: str) -> dict[str, Any]:
    j = await _post("https://api.tavily.com/search", {"Authorization": f"Bearer {_key('TAVILY_API_KEY')}"}, {
        "query": q,
        "search_depth": "ultra-fast" if tier == "fast" else "basic",
        "max_results": MAX_RESULTS,
        "include_usage": True,
        "include_published_date": True,
    })
    credits = (j.get("usage") or {}).get("credits")
    return {
        "results": [
            _row(r.get("title"), r.get("url", ""), r.get("content") or "", r.get("published_date"))
            for r in j.get("results", []) if r.get("url")
        ],
        "cost_usd": credits * 0.008 if isinstance(credits, (int, float)) else None,
        "vendor_ms": round(j["response_time"] * 1000) if j.get("response_time") else None,
    }


async def _exa(q: str, tier: str) -> dict[str, Any]:
    j = await _post("https://api.exa.ai/search", {"x-api-key": _key("EXA_API_KEY")}, {
        "query": q,
        "type": "instant" if tier == "fast" else "auto",
        "numResults": MAX_RESULTS,
        "contents": {"highlights": {"maxCharacters": 300}},
    })
    cost = (j.get("costDollars") or {}).get("total")
    return {
        "results": [
            _row(r.get("title"), r.get("url", ""), (r.get("highlights") or [r.get("text") or ""])[0], r.get("publishedDate"))
            for r in j.get("results", []) if r.get("url")
        ],
        "cost_usd": cost if isinstance(cost, (int, float)) else None,
        "vendor_ms": round(j["searchTime"]) if j.get("searchTime") else None,
    }


_IMPL: dict[str, Callable[[str, str], Awaitable[dict[str, Any]]]] = {
    "keenable": _keenable,
    "perplexity": _perplexity,
    "tavily": _tavily,
    "exa": _exa,
}


# ---------- public API ----------

async def search(provider: str, query: str, tier: str = "fast") -> dict[str, Any]:
    """Run one provider and time it. Never raises; errors land in ``error``."""
    tier = "standard" if tier == "standard" else "fast"
    base = {"provider": provider, "tier": tier, "label": LABEL[provider][tier], "results": []}
    if provider not in _IMPL:
        return {**base, "api_ms": 0, "error": f"unknown provider '{provider}'"}
    t0 = time.perf_counter()
    try:
        out = await _IMPL[provider](query, tier)
    except httpx.TimeoutException:
        return {**base, "api_ms": round((time.perf_counter() - t0) * 1000), "error": f"timeout after {TIMEOUT_S:.0f}s"}
    except Exception as e:  # ProviderError, network, JSON
        return {**base, "api_ms": round((time.perf_counter() - t0) * 1000), "error": str(e)[:300]}
    api_ms = round((time.perf_counter() - t0) * 1000)
    cost = out.get("cost_usd")
    return {
        **base,
        "api_ms": api_ms,
        "vendor_ms": out.get("vendor_ms"),
        "cost_usd": cost if isinstance(cost, (int, float)) else LIST_PRICE[provider][tier],
        "results": out["results"][:MAX_RESULTS],
    }


async def race(
    query: str,
    tier: str = "fast",
    on_lane: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    providers: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fire every provider at once. ``on_lane`` is awaited as each one finishes."""
    providers = providers or PROVIDERS

    async def one(p: str) -> dict[str, Any]:
        res = await search(p, query, tier)
        if on_lane is not None:
            await on_lane(res)
        return res

    results = await asyncio.gather(*(one(p) for p in providers))
    return {r["provider"]: r for r in results}
