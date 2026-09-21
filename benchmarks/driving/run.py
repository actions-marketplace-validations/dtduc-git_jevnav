"""Driving benchmark: the same agent, the same app, two browser MCPs.

Each task is run through `opencode run` with a prompt that allows exactly one
MCP server's tools. The app is local and deterministic, every task ends in a
string the agent has to report (`ANSWER=...`), and success is that string —
checked by this script, not by the agent's opinion of itself.

    uv run python benchmarks/driving/run.py --runs 3 --tasks login,create,payment,archive

Results land in benchmarks/driving/results-<date>.json.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE / "app"

TASKS = {
    "login": {
        "ask": (
            "Sign in with email demo@example.com and password hunter2. "
            "Then report the email address shown in the header."
        ),
        "answer": "demo@example.com",
    },
    "create": {
        "ask": (
            "In the 'New task' form create a task named 'Audit' with priority 'high' "
            "and 'Notify me' ticked, then report the confirmation message exactly."
        ),
        "answer": "Created task Audit (high, notified)",
    },
    "payment": {
        "ask": "Open the Payment tab and report the total amount due shown in the payment widget.",
        "answer": "128.50",
    },
    "archive": {
        "ask": (
            "In the tasks table, archive the task 'Renew TLS certificate' "
            "and report the confirmation message exactly."
        ),
        "answer": "Archived Renew TLS certificate",
    },
}

SERVERS = {
    "jevnav": (
        "Use ONLY the jevnav_* MCP tools (jevnav_goto, jevnav_goal, jevnav_browse, "
        "jevnav_page_state, jevnav_read_js, jevnav_click is not a tool - actions are "
        "jevnav_browse with action=click). Do not use chrome-devtools or playwright tools."
    ),
    "chrome-devtools": (
        "Use ONLY the chrome-devtools_* MCP tools (chrome-devtools_navigate_page, "
        "chrome-devtools_take_snapshot, chrome-devtools_click, chrome-devtools_fill, "
        "chrome-devtools_evaluate_script...). Do not use jevnav or playwright tools."
    ),
}

FOOTER = (
    "\nWhen done, reply with exactly one line: ANSWER=<the answer>  "
    "(if you could not finish, ANSWER=FAILED)"
)


def serve(port: int) -> ThreadingHTTPServer:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(APP), **kwargs)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def matches(reported: str, expected: str) -> bool:
    """The agent may wrap the answer in currency words or punctuation; the value must be there."""
    if not reported:
        return False
    flat = re.sub(r"[^a-z0-9.]+", "", reported.casefold())
    return re.sub(r"[^a-z0-9.]+", "", expected.casefold()) in flat


def run_once(server: str, task: str, url: str, timeout: int = 300) -> dict:
    prompt = f"{SERVERS[server]}\nThe app is at {url}\nTask: {TASKS[task]['ask']}{FOOTER}"
    started = time.perf_counter()
    try:
        process = subprocess.run(
            ["opencode", "run", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd="/tmp",
        )
        output = process.stdout + process.stderr
    except subprocess.TimeoutExpired:
        return {
            "server": server,
            "task": task,
            "ok": False,
            "seconds": timeout,
            "calls": 0,
            "note": "timeout",
        }
    seconds = round(time.perf_counter() - started, 1)
    answer = re.findall(r"ANSWER=([^\n]+)", output)
    reported = answer[-1].strip() if answer else ""
    prefix = "jevnav_" if server == "jevnav" else "chrome-devtools_"
    calls = len(re.findall(rf"⚙\s*{re.escape(prefix)}", output)) or len(
        re.findall(rf"{re.escape(prefix)}\w+", output)
    )
    return {
        "server": server,
        "task": task,
        "ok": matches(reported, TASKS[task]["answer"]),
        "strict": reported.casefold().strip() == TASKS[task]["answer"].casefold(),
        "reported": reported[:80],
        "expected": TASKS[task]["answer"],
        "seconds": seconds,
        "calls": calls,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--tasks", default="login,create,payment,archive")
    parser.add_argument("--port", type=int, default=8811)
    args = parser.parse_args()

    server = serve(args.port)
    url = f"http://127.0.0.1:{args.port}/"
    tasks = [task for task in args.tasks.split(",") if task]
    rows: list[dict] = []
    for task in tasks:
        for name in ("jevnav", "chrome-devtools"):
            for run in range(1, args.runs + 1):
                row = run_once(name, task, url)
                row["run"] = run
                rows.append(row)
                print(
                    f"[{'ok  ' if row['ok'] else 'FAIL'}] {name:16s} {task:8s} run{run} "
                    f"{row['seconds']:6.1f}s calls={row['calls']:2d} "
                    f"said {row.get('reported', '')!r}",
                    flush=True,
                )
    server.shutdown()

    print("\n=== summary ===")
    print(f"{'task':9s} {'server':17s} {'success':>8s} {'wall (s)':>9s} {'calls':>6s}")
    for task in tasks:
        for name in ("jevnav", "chrome-devtools"):
            subset = [row for row in rows if row["task"] == task and row["server"] == name]
            if not subset:
                continue
            wins = sum(row["ok"] for row in subset)
            print(
                f"{task:9s} {name:17s} {wins}/{len(subset):>6} "
                f"{sum(row['seconds'] for row in subset) / len(subset):9.1f} "
                f"{sum(row['calls'] for row in subset) / len(subset):6.1f}"
            )
    out = HERE / f"results-{time.strftime('%Y-%m-%d')}.json"
    out.write_text(json.dumps(rows, indent=2) + "\n")
    print(f"\nresults: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
