# Keenable × Decagon voice search demo (Gradium + Keenable vs Tavily, Exa)

One caller question, four search APIs, one stopwatch. A fork of
[`web_voice_search`](../web_voice_search) that keeps the Gradium voice agent and
replaces the single Keenable lookup with a four-way race, so a voice-agent
buyer can see (and hear) how long the agent goes silent on each search API.

## The user journey

1. **Say it or pick it.** Tap the mic and ask, or choose a support-agent phrase
   from the list ("Are there any changes to today's flights out of Heathrow?"),
   or type your own.
2. **Four lanes race.** Keenable, Tavily and Exa get the identical
   query at the same instant. Each lane shows a live stopwatch, a bar that goes
   amber at 300 ms and red at 800 ms, the top results, and the one spoken
   sentence the caller would hear from that lane.
3. **Hear the difference.** With the mic, the agent speaks from the Keenable
   lane the moment its results land (the other lanes keep filling in). "Hear it"
   on any lane plays that lane's measured silence, then its sentence.
4. **Scoreboard** accumulates median / best / worst / wins / cost per 1K across
   the meeting, with a Reset.

Each vendor runs its voice-grade tier by default (Keenable `realtime`,
Perplexity `fast`, Tavily `ultra-fast`, Exa `instant`); a switch flips all four
to standard tiers (`pro` / `web` / `basic` / `auto`).

## Run locally

```bash
cd demos/voice_search_race
uv sync
cp .env.example .env    # add keys, see below
uv run uvicorn main:app --reload --port 8070
```

Open http://localhost:8070/. Phrases work with only search keys; the mic also
needs `GRADIUM_API_KEY`.

## Configuration (`.env`)

| Variable | Needed for | Notes |
| --- | --- | --- |
| `KEENABLE_API_KEY` | Keenable lane | Optional. Without it the keyless `/v1/search/public` endpoint is used (rate limited per IP). Use a key in a meeting. |
| `PERPLEXITY_API_KEY` | Perplexity lane | perplexity.ai/settings/api, needs a small credit top-up. |
| `TAVILY_API_KEY` | Tavily lane | app.tavily.com, free monthly credits. |
| `EXA_API_KEY` | Exa lane | dashboard.exa.ai, free starter credits. |
| `ACTIVE_PROVIDER` | voice agent | Lane the agent answers from. Default `keenable`. Set `perplexity` to show the agent on the incumbent's clock. |
| `RACE_TIER` | both | `fast` (default) or `standard`. The page's radio switch only affects REST races; the voice agent uses this value. |
| `GRADIUM_API_KEY` | mic | Gradium STT + TTS. |
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | voice agent and per-lane sentence | OpenAI-compatible, must support tool calling. Defaults to OpenRouter + `openai/gpt-4o-mini`. |

## How it's built

- `providers.py`: the four vendors behind one `search(provider, query, tier)`,
  each timed around its own HTTP call. `race()` fires them all with
  `asyncio.gather` and awaits an `on_lane` callback as each finishes.
- `main.py`: the Gradbot voice session from the original demo. Its
  `web_search` tool now starts the race, sends each lane to the browser as it
  completes, and hands the `ACTIVE_PROVIDER` results back to the LLM the moment
  they land, so the spoken answer is on that lane's clock. Also exposes
  `GET /api/search`, `POST /api/answer` and `GET /api/race-config` so the page
  can run the same race without a microphone.
- `answer.py`: one spoken sentence per lane from the same model and prompt.
- `static/index.html`: orb + phrase picker on top, four lanes, transcript,
  scoreboard. Reuses Gradbot's bundled audio JS. Falls back to clearly labelled
  simulated latencies if the backend is unreachable.

## Before a meeting

- Run every phrase in the bank once and swap out any where the Keenable lane
  loses on relevance. The bank is the first thing in the page's `<script>`.
- Perplexity `fast` is priced at $1/1K and quotes ~160 ms p50, so it ties
  Keenable on speed and price. Keep the lane in; the clean wins are against
  Tavily and Exa, and on index independence. Hiding it would be noticed.
- Cost per 1K uses vendor-reported cost where the API returns it (Exa, Tavily)
  and `LIST_PRICE` in `providers.py` otherwise. Check against each pricing page.
- Press "Again" once before the audience watches; first calls after a cold
  start are slow for everyone.
- Record a 60-second screen video of three races as a backup for bad Wi-Fi.

## Logos

Drop `decagon.svg` and `keenable.svg` into `static/logos/` and the header picks them up
automatically (text wordmarks are shown until then).

## Scorecard

Two views, toggled next to the heading:

- **This session**: every race since Reset (voice calls included), kept in the
  browser so it survives a reload.
- **Full set**: the baked benchmark in `static/set_results.json`, produced by
  `bench.py`. Median, p95, cost per 1K and dead-air count across the 30-question
  set in `static/questions.json`. Not run yet → the view says so.

To bake the numbers (about 2 minutes on a normal connection):

```bash
cd demos/voice_search_race
uv run python bench.py            # 2 passes x 30 questions x 3 lanes
git add static/set_results.json && git commit -m "bench: <date>" && git push
```

Run it from the laptop you'll present from, not through a VPN, and ideally at
two different times of day; `--passes 3` tightens the p95. The file also keeps
each lane's top five URLs per question, so answer quality can be judged later
and the placeholder NDCG row replaced.

Answer quality is a placeholder estimated from NEEDLE until the set has been
judged; edit the three numbers in the scorecard table in `static/index.html`.
