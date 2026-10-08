"""Voice search race: one caller question, four search APIs, one stopwatch.

Forked from ``demos/web_voice_search`` (Gradium voice + Keenable realtime).
Two ways in, one set of lanes:

* Tap the mic and ask. The agent's ``web_search`` tool fans the query out to
  Keenable, Perplexity, Tavily and Exa at the same instant. Keenable's results
  (``ACTIVE_PROVIDER``) go back to the LLM the moment they land, so the caller
  hears the answer on Keenable's clock; the other lanes keep filling in on screen.
* Pick a support-agent phrase from the list (or type one). The browser runs the
  same race over REST, no microphone or Gradium key needed, and reads each
  lane's one-sentence answer with the measured silence in front of it.

Run locally:
    uv sync
    cp .env.example .env
    uv run uvicorn main:app --reload --port 8070
Then open http://localhost:8070/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
from datetime import datetime

import fastapi
import httpx
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv(pathlib.Path(__file__).parent / ".env")  # before importing gradbot

import gradbot  # noqa: E402

import providers  # noqa: E402
from answer import spoken_answer  # noqa: E402

gradbot.init_logging()
logger = logging.getLogger(__name__)

app = fastapi.FastAPI(title="Voice Search Race")
cfg = gradbot.config.from_env()

DEFAULT_VOICE_ID = "YTpq7expH9539ERJ"  # Emma
ACTIVE_PROVIDER = (os.environ.get("ACTIVE_PROVIDER") or "keenable").lower()  # the lane the voice agent answers from
RACE_TIER = "standard" if (os.environ.get("RACE_TIER") or "fast") == "standard" else "fast"

SYSTEM_PROMPT_TEMPLATE = """You are the voice of a customer-support phone agent with live \
access to the web through a `web_search` tool. You are on a phone call, so keep \
everything short and conversational.

Today's date is {today}. When a question is about something recent, "today", \
"this week" or "at the moment", put the date context in your query.

How you work:
- For ANY question about facts, status, prices, policies, opening hours, \
disruption, or anything you are not certain of, you MUST call `web_search` \
immediately as your FIRST action, before saying anything. No filler, no "let me \
check". Stay silent and call the tool; the search is fast.
- After the tool returns, answer in 1-2 short spoken sentences. If you name a source, it \
must be the site shown with the result you used; never invent one. Never read URLs aloud. \
Say figures, dates and times the way a person would on the phone.
- If the search returns nothing useful, say you couldn't find it just now. Never guess.
- Stay on task; steer small talk back to "what can I look up for you?"."""


def _system_prompt() -> str:
    now = datetime.now()
    return SYSTEM_PROMPT_TEMPLATE.format(today=now.strftime("%A, %B %-d, %Y"))


TOOLS = [
    gradbot.ToolDef(
        name="web_search",
        description=(
            "Search the live web for current or factual information: service status, "
            "disruption, prices, policies, opening hours, news. Returns top results with "
            "title, source site and a snippet."
        ),
        parameters_json=json.dumps({
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to find, as a clear natural-language query."}},
            "required": ["query"],
        }),
    )
]


def make_config(msg: dict) -> gradbot.SessionConfig:
    language = msg.get("language") or "en"
    return gradbot.SessionConfig(
        voice_id=msg.get("voice_id") or DEFAULT_VOICE_ID,
        language=gradbot.LANGUAGES.get(language) if language else None,
        instructions=_system_prompt(),
        tools=TOOLS,
        **({"assistant_speaks_first": True} | cfg.session_kwargs),
    )


def _tool_payload(res: dict) -> str:
    if res.get("error") or not res["results"]:
        return json.dumps({
            "success": False,
            "message": "The web search didn't return anything usable. Tell the caller you couldn't find that just now.",
        })
    summary = "\n".join(f"- {r['title']} ({r['source']}): {r['snippet'][:300]}" for r in res["results"])
    return json.dumps({
        "success": True,
        "query": res.get("query"),
        "results_summary": summary,
        "message": "Answer the caller in 1-2 short spoken sentences using only these results. If you cite a source, use the site name shown in brackets; never invent one. Do not read URLs.",
    })


async def handle_tool_call(handle, input_handle, websocket: fastapi.WebSocket):
    if handle.name != "web_search":
        await handle.send_error(f"Unknown tool: {handle.name}")
        return
    query = (handle.args.get("query") or "").strip()
    if not query:
        await handle.send_error("web_search requires a non-empty query.")
        return

    logger.info("web_search: %r", query)
    await websocket.send_json({"type": "race_started", "query": query, "tier": RACE_TIER, "providers": providers.PROVIDERS, "active": ACTIVE_PROVIDER})

    loop = asyncio.get_running_loop()
    active_done: asyncio.Future = loop.create_future()

    async def on_lane(res: dict) -> None:
        res["query"] = query
        try:
            await websocket.send_json({"type": "race_lane", **res})
        except Exception:
            pass
        if res["provider"] == ACTIVE_PROVIDER and not active_done.done():
            active_done.set_result(res)

    # Fire all four; keep the race running after the active lane returns.
    race_task = asyncio.create_task(providers.race(query, RACE_TIER, on_lane=on_lane))

    try:
        active = await asyncio.wait_for(active_done, timeout=providers.TIMEOUT_S + 1)
    except asyncio.TimeoutError:
        active = {"error": "timeout", "results": []}
    await handle.send(_tool_payload(active))  # the caller hears the answer on the active lane's clock

    try:
        await race_task
    except Exception:
        logger.exception("race task failed")
    await websocket.send_json({"type": "race_done", "query": query})


@app.websocket("/ws/chat")
async def ws_chat(websocket: fastapi.WebSocket):
    await gradbot.websocket.handle_session(websocket, config=cfg, on_start=make_config, on_tool_call=handle_tool_call)


# ---------- REST: the same race without a microphone ----------

@app.get("/api/search")
async def api_search(provider: str, q: str, tier: str = "fast"):
    return await providers.search(provider.lower(), q.strip(), tier)


class AnswerIn(BaseModel):
    query: str
    results: list[dict] = []


@app.post("/api/answer")
async def api_answer(body: AnswerIn):
    return await spoken_answer(body.query, body.results)


class TtsIn(BaseModel):
    text: str
    voice_id: str | None = None


@app.post("/api/tts")
async def api_tts(body: TtsIn):
    """Speak one lane's sentence with the same Gradium voice the agent uses. Returns WAV bytes."""
    key = (os.environ.get("GRADIUM_API_KEY") or "").strip()
    if not key:
        return fastapi.Response(status_code=503, content="GRADIUM_API_KEY not set")
    text = body.text.strip()[:400]
    if not text:
        return fastapi.Response(status_code=400, content="empty text")
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            "https://api.gradium.ai/api/post/speech/tts",
            headers={"x-api-key": key},
            json={"text": text, "voice_id": body.voice_id or DEFAULT_VOICE_ID, "output_format": "wav", "only_audio": True},
        )
    if r.status_code >= 400:
        return fastapi.Response(status_code=502, content=f"Gradium TTS {r.status_code}: {r.text[:200]}")
    return fastapi.Response(content=r.content, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.get("/api/race-config")
async def api_race_config():
    return {
        "providers": providers.PROVIDERS,
        "active": ACTIVE_PROVIDER,
        "tier": RACE_TIER,
        "keys": {p: bool(os.environ.get(k)) for p, k in [
            ("keenable", "KEENABLE_API_KEY"), ("tavily", "TAVILY_API_KEY"), ("exa", "EXA_API_KEY")]},
        "llm": bool(os.environ.get("LLM_API_KEY")),
        "gradium": bool(os.environ.get("GRADIUM_API_KEY")),
    }


gradbot.routes.setup(app, config=cfg, static_dir=pathlib.Path(__file__).parent / "static", with_voices=True)
