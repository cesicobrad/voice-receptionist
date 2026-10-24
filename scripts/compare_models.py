"""Run the same scripted calls against several Claude models and compare them.

    python scripts/compare_models.py --runs 2 --out docs/model-comparison.json

Uses the real API (ANTHROPIC_API_KEY from .env). Each scenario gets a fresh in-memory
database and a fixed clock (Saturday 24 October 2026, 12:00), so outcomes are checkable.
Latency is per caller turn: every Claude request and tool call needed before Nora can answer.
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.assistant import ClaudeReceptionist  # noqa: E402
from app.booking import BookingService  # noqa: E402

NOW = datetime(2026, 10, 24, 12, 0)
TUESDAY = date(2026, 10, 27)
# USD per million tokens (input, output), Claude API list prices. Thinking tokens bill as output.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def booked(store, phone="040123456"):
    rows = store.upcoming()
    return any(r["service"] == "cleaning" and r["start"] == "2026-10-27T10:00" and r["phone"] == phone
               and r["name"] == "Luka Horvat" for r in rows)


SCENARIOS = [
    {
        "name": "booking", "language": "en",
        "turns": ["Hi, I'd like to book a cleaning next Tuesday morning, around 10 if that's free.",
                  "Luka Horvat, and my number is 040 123 456.",
                  "Yes, please."],
        "check": lambda store, replies, seed: booked(store),
    },
    {
        "name": "questions", "language": "en",
        "turns": ["Are you open on Friday afternoon? And is there parking?"],
        "check": lambda store, replies, seed: "park" in replies[-1].lower()
        and any(t in replies[-1].lower() for t in ("2 pm", "two", "14", "2pm")) and not store.upcoming(),
    },
    {
        "name": "cancel", "language": "en", "seed": True,
        "turns": ["I need to cancel my appointment, the reference is {ref}.", "040 123 456"],
        "check": lambda store, replies, seed: not store.upcoming(),
    },
    {
        "name": "callback", "language": "en",
        "turns": ["Can someone call me back about whether my insurance covers whitening? I'm Maja Kos, 041 222 333.",
                  "Yes, please."],
        "check": lambda store, replies, seed: any(m["phone"] == "041222333" for m in store.messages()),
    },
    {
        "name": "danger", "language": "en",
        "turns": ["My cheek is swelling up and it's getting hard to swallow."],
        "check": lambda store, replies, seed: "112" in replies[-1],
    },
    {
        "name": "booking (sl)", "language": "sl",
        "turns": ["Pozdravljeni, rad bi se naročil na čiščenje zob prihodnji torek dopoldne, okoli desetih, če je prosto.",
                  "Luka Horvat, moja številka je 040 123 456.",
                  "Da, prosim."],
        "check": lambda store, replies, seed: booked(store),
    },
    {
        "name": "questions (sl)", "language": "sl",
        "turns": ["Ali ste v petek popoldne odprti? In ali imate parkirišče?"],
        "check": lambda store, replies, seed: "parkir" in replies[-1].lower() and not store.upcoming(),
    },
]


class ErrorCounter(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.count = 0

    def emit(self, record):
        self.count += 1


def run_scenario(model: str, scenario: dict, effort: str) -> dict:
    store = BookingService(":memory:", now=lambda: NOW)
    seed = None
    if scenario.get("seed"):
        seed = store.book("cleaning", TUESDAY, "10:00", "Luka Horvat", "040 123 456")
    bot = ClaudeReceptionist(store, model=model, effort=effort, language=scenario["language"])
    errors = ErrorCounter()
    logging.getLogger("receptionist").addHandler(errors)
    transcript, turn_seconds, replies = [], [], []
    try:
        for text in scenario["turns"]:
            text = text.format(ref=" ".join(seed.reference) if seed else "")
            started = time.perf_counter()
            turn = bot.respond(text)
            turn_seconds.append(round(time.perf_counter() - started, 2))
            replies.append(turn.reply)
            transcript.append({"caller": text, "nora": turn.reply, "seconds": turn_seconds[-1],
                               "events": [e["type"] for e in turn.events]})
    finally:
        logging.getLogger("receptionist").removeHandler(errors)
    p_in, p_out = PRICES[model]
    costs = [(c["input_tokens"] * p_in + c["output_tokens"] * p_out) / 1e6 for c in bot.calls]
    return {
        "model": model, "scenario": scenario["name"],
        "ok": errors.count == 0 and bool(scenario["check"](store, replies, seed)),
        "api_errors": errors.count,
        "turn_seconds": turn_seconds,
        "calls": bot.calls,
        "cost_usd": round(sum(costs), 5),
        "transcript": transcript,
    }


def summarize(results: list[dict]) -> list[dict]:
    rows = []
    for model in dict.fromkeys(r["model"] for r in results):
        mine = [r for r in results if r["model"] == model]
        turns = [s for r in mine for s in r["turn_seconds"]]
        calls = [c for r in mine for c in r["calls"]]
        rows.append({
            "model": model,
            "passed": f"{sum(r['ok'] for r in mine)}/{len(mine)}",
            "failed": sorted({r["scenario"] for r in mine if not r["ok"]}),
            "turn_median_s": round(statistics.median(turns), 2),
            "turn_p90_s": round(statistics.quantiles(turns, n=10)[-1], 2) if len(turns) > 1 else turns[0],
            "call_median_s": round(statistics.median(c["seconds"] for c in calls), 2),
            "calls": len(calls),
            "avg_in_tokens": round(statistics.mean(c["input_tokens"] for c in calls)),
            "avg_out_tokens": round(statistics.mean(c["output_tokens"] for c in calls)),
            "cost_per_call_usd": round(sum(r["cost_usd"] for r in mine) / len(calls), 5),
            "cost_per_conversation_usd": round(sum(r["cost_usd"] for r in mine) / len(mine), 4),
            "total_cost_usd": round(sum(r["cost_usd"] for r in mine), 4),
        })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=list(PRICES))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--effort", default="low")
    parser.add_argument("--out", default="docs/model-comparison.json")
    args = parser.parse_args()

    results = []
    for run in range(args.runs):
        for model in args.models:
            for scenario in SCENARIOS:
                r = run_scenario(model, scenario, args.effort)
                r["run"] = run + 1
                results.append(r)
                print(f"run {run + 1} {model:18} {scenario['name']:15} {'ok ' if r['ok'] else 'FAIL'} "
                      f"turns {r['turn_seconds']}  ${r['cost_usd']:.4f}", flush=True)
    summary = summarize(results)
    Path(args.out).write_text(json.dumps({"effort": args.effort, "clock": NOW.isoformat(), "summary": summary,
                                          "results": results}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
