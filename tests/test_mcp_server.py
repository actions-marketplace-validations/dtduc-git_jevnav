"""End-to-end MCP test: a real client, a real stdio server, no network.

`jevnav mcp` is started as a subprocess (the way an LLM client starts it) and
pointed at a local stand-in for the Jev endpoint, so the whole path — handshake,
tool listing, tool call, browser action, trace — runs offline.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from helpers import FakeJev, fixture_url

mcp = pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parent.parent

SCRIPT = [
    {"action": "fill", "target": "Email", "value_key": "email"},
    {"action": "click", "target": "Sign in"},
]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server naming
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        fake: FakeJev = self.server.fake  # type: ignore[attr-defined]
        questions = body["questions"]
        answers = (
            fake._loop_answers(questions)
            if "status" in questions
            else fake._single_answers(questions)
        )
        payload = json.dumps(
            {
                "model": "jev-fake-1",
                "usage": {"input_tokens": 100, "output_tokens": 10},
                "answers": answers,
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep the test output clean
        pass


@pytest.fixture
def fake_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.fake = FakeJev({"Sign in to the account": "Sign in"}, script=SCRIPT)  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run_in_thread(coroutine) -> Any:
    """The sync Playwright fixture owns the main thread's event loop, so the
    MCP client gets a thread of its own."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coroutine)
        except BaseException as error:  # re-raised in the test thread
            box["error"] = error

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


async def drive(fake_endpoint: str, trace: Path, calls: list[tuple[str, dict]]) -> list[str]:
    from mcp import ClientSession, StdioServerParameters, stdio_client

    env = {
        **os.environ,
        "TYPESAFE_BASE_URL": fake_endpoint,
        "TYPESAFE_API_KEY": "test-key",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jevnav", "mcp", "--trace", str(trace)],
        env=env,
        cwd=str(REPO),
    )
    outcomes: list[str] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            outcomes.append(",".join(sorted(tool.name for tool in tools.tools)))
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                outcomes.append(result.content[0].text)
    return outcomes


def test_mcp_server_lists_tools_and_drives_a_goal(fake_endpoint, tmp_path):
    trace = tmp_path / "session.trace.jsonl"
    outcomes = run_in_thread(
        drive(
            fake_endpoint,
            trace,
            [
                ("goto", {"url": fixture_url("loop-app.html")}),
                (
                    "goal",
                    {
                        "goal": "sign in with the demo account",
                        "context_json": '{"email": "demo@example.com"}',
                    },
                ),
                ("page_state", {}),
                ("summary", {}),
            ],
        )
    )
    assert outcomes[0] == "browse,goal,goto,page_state,summary"
    opened = json.loads(outcomes[1])
    assert opened["url"].endswith("loop-app.html")
    goal = json.loads(outcomes[2])
    assert goal["status"] == "done"
    assert goal["steps"] == 3
    state = json.loads(outcomes[3])
    assert state["url"].endswith("loop-app.html")
    # the browser really acted: the login form is gone, the sign-out button is there
    names = [element["name"] for element in state["candidates"]]
    assert "Sign out" in names and "Sign in" not in names
    summary = json.loads(outcomes[4])
    assert summary["steps"] == 3
    assert summary["auto"] == 2
    run, steps = json.loads(trace.read_text().splitlines()[0]), trace.read_text().splitlines()[1:]
    assert run["flow"] == "mcp-session"
    assert len(steps) == 3


def test_mcp_browse_returns_a_playwright_selector(fake_endpoint, tmp_path):
    outcomes = run_in_thread(
        drive(
            fake_endpoint,
            tmp_path / "b.trace.jsonl",
            [
                ("goto", {"url": fixture_url("loop-app.html")}),
                ("browse", {"intent": "Sign in to the account"}),
            ],
        )
    )
    browse = json.loads(outcomes[2])
    assert browse["target"]["selector"] == 'role=button[name="Sign in"]'
    assert browse["status"] in {"auto", "review"}
