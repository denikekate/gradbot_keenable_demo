"""Run the whole question set against every lane and bake the numbers into the page.

    uv run python bench.py              # 2 passes over all 30 questions, all lanes
    uv run python bench.py --passes 3   # more passes, tighter p95
    uv run python bench.py --providers keenable,exa

Writes static/set_results.json, which the page's "Full set" view reads. Commit
that file and the numbers travel with the repo. Each question is sent to all
lanes at the same instant, exactly as the page does, and only the search call
is timed (no LLM, no voice). Run it from a normal network, not through a VPN,
and ideally twice at different times of day.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

HERE = pathlib.Path(__file__).parent
load_dotenv(HERE / ".env")

import providers  # noqa: E402

DEAD_AIR_MS = 800


def pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    arr = sorted(values)
    return arr[min(len(arr) - 1, max(0, int(round(q * len(arr) + 0.5)) - 1))]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--providers", default=",".join(providers.PROVIDERS))
    ap.add_argument("--pause", type=float, default=0.6, help="seconds between questions, to stay under free-tier rate limits")
    args = ap.parse_args()
    lanes = [p.strip() for p in args.providers.split(",") if p.strip()]

    bank = json.loads((HERE / "static" / "questions.json").read_text())
    questions = [q for qs in bank.values() for q in qs]
    print(f"{len(questions)} questions x {args.passes} passes x {len(lanes)} lanes")

    runs: list[dict] = []
    t_start = time.time()
    for n in range(args.passes):
        for i, q in enumerate(questions, 1):
            res = await providers.race(q, "fast", providers=lanes)
            row = {"pass": n + 1, "q": q}
            cells = []
            for p in lanes:
                r = res[p]
                row[p] = {
                    "api_ms": r["api_ms"],
                    "cost_usd": r.get("cost_usd"),
                    "error": r.get("error"),
                    "top": [{"title": x["title"], "url": x["url"]} for x in r["results"][:5]],
                }
                cells.append(f"{p[:4]} {('ERR' if r.get('error') else str(r['api_ms']) + 'ms'):>7}")
            runs.append(row)
            print(f"[{n + 1}/{args.passes}] {i:2d}/{len(questions)}  " + "  ".join(cells) + f"   {q[:48]}")
            await asyncio.sleep(args.pause)

    summary = {}
    for p in lanes:
        ok = [r[p]["api_ms"] for r in runs if not r[p]["error"]]
        costs = [r[p]["cost_usd"] for r in runs if not r[p]["error"] and isinstance(r[p]["cost_usd"], (int, float))]
        summary[p] = {
            "label": providers.LABEL[p]["fast"],
            "races": len(runs),
            "errors": sum(1 for r in runs if r[p]["error"]),
            "median_ms": int(statistics.median(ok)) if ok else None,
            "p95_ms": pct(ok, 0.95),
            "mean_ms": int(statistics.mean(ok)) if ok else None,
            "cost_per_1k": round(statistics.mean(costs) * 1000, 2) if costs else None,
            "dead_air": sum(1 for v in ok if v >= DEAD_AIR_MS),
        }

    out = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": int(time.time() - t_start),
        "passes": args.passes,
        "questions": len(questions),
        "dead_air_ms": DEAD_AIR_MS,
        "tier": "fast",
        "summary": summary,
        "runs": runs,
    }
    path = HERE / "static" / "set_results.json"
    path.write_text(json.dumps(out, indent=1))

    print("\nlane        median    p95   mean  dead-air  errors  $/1k")
    for p, s in summary.items():
        print(f"{p:10s} {s['median_ms'] or '-':>7} {s['p95_ms'] or '-':>6} {s['mean_ms'] or '-':>6} {s['dead_air']:>9} {s['errors']:>7}  {s['cost_per_1k'] or '-'}")
    print(f"\nwrote {path.relative_to(HERE)}  ({len(runs)} races). Commit it so the page's Full set view ships with the numbers.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
