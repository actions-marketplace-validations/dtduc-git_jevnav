"""Head-to-head: jevnav vs chrome-devtools-mcp, measured on the same pages.

What can be measured without an LLM in the loop is what the *caller* pays per
step: tool-call latency, the size of the observation it must read, and (for
jevnav) the real Jev decision. The LLM turn a snapshot-based tool needs is
modelled from the observation size, with the price and turn time stated so you
can plug in your own.

    uv run --with mcp python benchmarks/mcp-compare.py [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client

JEVNAV = StdioServerParameters(
    command="uvx",
    args=[
        "--from",
        "jevnav[mcp] @ git+https://github.com/dtduc-git/jevnav@main",
        "jevnav",
        "mcp",
    ],
    cwd="/tmp",
)
CHROME_DEVTOOLS = StdioServerParameters(
    command="npx", args=["chrome-devtools-mcp@latest"], cwd="/tmp"
)

PAGE = "https://news.ycombinator.com/"
TASK = "open the Newest page"

# What an LLM turn costs the caller, stated so it can be argued with.
LLM_USD_PER_MTOK = 3.0  # a Sonnet-class model
LLM_TURN_SECONDS = 2.0  # one turn reading the observation and choosing
CHARS_PER_TOKEN = 4


async def timed(session: ClientSession, name: str, args: dict[str, Any]) -> tuple[float, str]:
    started = time.perf_counter()
    result = await session.call_tool(name, args)
    elapsed = (time.perf_counter() - started) * 1000
    text = ""
    for block in result.content or []:
        text += getattr(block, "text", "") or ""
    return elapsed, text


async def measure_jevnav() -> dict[str, Any]:
    async with stdio_client(JEVNAV) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            cold, _ = await timed(session, "summary", {})
            goto_ms, _ = await timed(session, "goto", {"url": PAGE})
            state_ms, state = await timed(session, "page_state", {})
            goal_ms, goal_text = await timed(
                session,
                "goal",
                {"goal": TASK, "success": "span.topsel a[href='newest']"},
            )
            goal = json.loads(goal_text)
            return {
                "server": "jevnav",
                "ready_ms": round(cold, 1),
                "observe_ms": round(state_ms, 1),
                "observation_chars": len(state),
                "task_ms": round(goal_ms, 1),
                "task_calls": goal.get("steps", 0) + 2,  # goto + page_state + goal
                "task_model_calls": 2,  # the caller looks once, then delegates
                "task_cost_usd": goal.get("cost_usd"),
                "task_status": goal.get("status"),
                "task_verified": goal.get("verified"),
                "decision_ms_p50": goal.get("latency_p50_ms"),
                "decision_ms_p95": goal.get("latency_p95_ms"),
            }


async def measure_chrome_devtools() -> dict[str, Any]:
    async with stdio_client(CHROME_DEVTOOLS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            cold, listing = await timed(session, "list_pages", {})
            import re

            match = re.search(r'"pageId"\s*:\s*(\d+)', listing)
            page_id = int(match.group(1)) if match else 1
            nav_ms, _ = await timed(session, "navigate_page", {"pageId": page_id, "url": PAGE})
            snap_ms, snapshot = await timed(session, "take_snapshot", {"pageId": page_id})
            # one click step: the caller reads the snapshot, picks the uid, clicks
            uid = None
            for line in snapshot.splitlines():
                if 'link "' in line and "new" in line.lower():
                    uid = line.split("uid=")[1].split()[0] if "uid=" in line else None
                    break
            click_ms = None
            if uid:
                click_ms, _ = await timed(session, "click", {"pageId": page_id, "uid": uid})
            after_ms, after = await timed(session, "take_snapshot", {"pageId": page_id})
            observation_chars = len(snapshot)
            model_calls = 2  # one to choose the link, one to confirm the page
            return {
                "server": "chrome-devtools-mcp",
                "ready_ms": round(cold, 1),
                "observe_ms": round(snap_ms, 1),
                "observation_chars": observation_chars,
                "task_ms": round(nav_ms + snap_ms + (click_ms or 0) + after_ms, 1),
                "task_calls": 4 if click_ms else 3,
                "task_model_calls": model_calls,
                "task_cost_usd": None,
                "task_status": "measured browser-side only",
                "task_verified": None,
                "decision_ms_p50": None,
                "decision_ms_p95": None,
            }


def with_modelled_turn(row: dict[str, Any]) -> dict[str, Any]:
    """Add the LLM-turn time the caller pays, from the observation size."""
    tokens = row["observation_chars"] / CHARS_PER_TOKEN
    row["modelled_llm_seconds"] = round(row["task_model_calls"] * LLM_TURN_SECONDS, 1)
    row["modelled_llm_usd"] = round(
        tokens * row["task_model_calls"] * LLM_USD_PER_MTOK / 1_000_000, 6
    )
    row["modelled_total_usd"] = round((row["task_cost_usd"] or 0) + row["modelled_llm_usd"], 6)
    row["modelled_total_seconds"] = round(row["task_ms"] / 1000 + row["modelled_llm_seconds"], 1)
    return row


async def main() -> dict[str, Any]:
    rows = [
        with_modelled_turn(await measure_jevnav()),
        with_modelled_turn(await measure_chrome_devtools()),
    ]
    print(f"task: {TASK!r} on {PAGE}\n")
    print(f"{'metric':28s} {'jevnav':>16s} {'chrome-devtools':>18s}")
    for key, label in [
        ("ready_ms", "MCP ready (ms)"),
        ("observe_ms", "observation call (ms)"),
        ("observation_chars", "observation (chars)"),
        ("task_ms", "browser-side total (ms)"),
        ("task_calls", "tool calls"),
        ("task_model_calls", "model turns"),
        ("decision_ms_p50", "decision p50 (ms)"),
        ("modelled_total_seconds", "wall clock (s, modelled)"),
        ("modelled_llm_usd", "LLM cost ($, modelled)"),
        ("modelled_total_usd", "total cost ($)"),
        ("task_cost_usd", "decision cost ($, real)"),
        ("task_status", "task status"),
        ("task_verified", "outcome verified"),
    ]:
        left = rows[0].get(key)
        right = rows[1].get(key)
        print(f"{label:28s} {str(left):>16s} {str(right):>18s}")
    print(
        f"\nmodel assumptions for the snapshot side: {LLM_TURN_SECONDS}s and "
        f"${LLM_USD_PER_MTOK}/M tokens (Sonnet-class, {CHARS_PER_TOKEN} chars/token)"
    )
    return {"task": TASK, "page": PAGE, "rows": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", help="also write the numbers here")
    args = parser.parse_args()
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = asyncio.run(main())
        except BaseException as error:  # surfaced in the main thread
            box["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(box["value"], handle, indent=2, default=str)
