"""End-to-end MCP test: a real client, a real stdio server, no network.

`jevnav mcp` is started as a subprocess (the way an LLM client starts it) and
pointed at a local stand-in for the Jev endpoint, so the whole path — handshake,
tool listing, tool call, browser action, trace — runs offline.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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


def server_params(fake_endpoint: str, trace: Path):
    from mcp import StdioServerParameters

    env = {
        **os.environ,
        "TYPESAFE_BASE_URL": fake_endpoint,
        "TYPESAFE_API_KEY": "test-key",
    }
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "jevnav", "mcp", "--trace", str(trace)],
        env=env,
        cwd=str(REPO),
    )


async def list_tool_annotations(fake_endpoint: str, trace: Path) -> dict[str, Any]:
    from mcp import ClientSession, stdio_client

    async with stdio_client(server_params(fake_endpoint, trace)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
    return {tool.name: tool.annotations for tool in tools}


async def drive(fake_endpoint: str, trace: Path, calls: list[tuple[str, dict]]) -> list[str]:
    from mcp import ClientSession, stdio_client

    params = server_params(fake_endpoint, trace)
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


# tool name -> (read_only, destructive, idempotent, open_world)
# The whole table is pinned: a mapping change must be deliberate.
ANNOTATIONS: dict[str, tuple[bool, bool, bool, bool]] = {
    "browse": (False, True, False, True),
    "close_page": (False, True, False, True),
    "console": (True, False, True, False),
    "dialog_policy": (False, False, False, True),
    "dialogs": (True, False, True, False),
    "drag": (False, True, False, True),
    "emulate": (False, False, True, True),
    "fill_form": (False, True, False, True),
    "goal": (False, True, False, True),
    "goto": (False, False, True, True),
    "heap_snapshot": (False, False, False, True),
    "lighthouse": (False, False, True, True),
    "network": (True, False, True, False),
    "network_detail": (True, False, True, False),
    "new_page": (False, False, False, True),
    "outline": (True, False, True, True),
    "page_state": (True, False, True, True),
    "perf_metrics": (True, False, True, True),
    "press_key": (False, True, False, True),
    "read_js": (False, True, False, True),
    "resize": (False, False, True, True),
    "route": (False, False, False, True),
    "screenshot": (False, False, False, True),
    "scroll": (False, False, False, True),
    "select_page": (False, False, True, True),
    "styles": (True, False, True, True),
    "summary": (True, False, True, False),
    "tabs": (True, False, True, True),
    "trace_start": (False, False, False, True),
    "trace_stop": (False, False, False, True),
    "unroute": (False, False, True, True),
    "upload_files": (False, True, False, True),
    "wait_for": (True, False, True, True),
}


def hint(ann: Any, key: str) -> bool | None:
    """Read a ToolAnnotations field, whichever casing this mcp version uses."""
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
    for name in (key, snake):
        if hasattr(ann, name):
            return getattr(ann, name)
    raise AssertionError(f"no {key} on {ann!r}")


def test_every_tool_declares_mcp_annotations(fake_endpoint, tmp_path):
    """TDQS reads the schema: every tool must say what it does to its world."""
    annotations = run_in_thread(
        list_tool_annotations(fake_endpoint, tmp_path / "session.trace.jsonl")
    )
    assert set(annotations) == set(ANNOTATIONS)
    found = {}
    for name, ann in annotations.items():
        assert ann is not None, f"{name} has no annotations"
        found[name] = (
            hint(ann, "readOnlyHint"),
            hint(ann, "destructiveHint"),
            hint(ann, "idempotentHint"),
            hint(ann, "openWorldHint"),
        )
    assert found == ANNOTATIONS


async def drive_network_by_id(fake_endpoint: str, trace: Path) -> tuple[dict, dict]:
    from mcp import ClientSession, stdio_client

    async with stdio_client(server_params(fake_endpoint, trace)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("goto", {"url": fixture_url("loop-app.html")})
            net = json.loads((await session.call_tool("network", {"limit": 5})).content[0].text)
            entry = next(r for r in net["requests"] if "loop-app.html" in r["url"])
            raw = await session.call_tool("network_detail", {"id": entry["id"]})
            detail = json.loads(raw.content[0].text)
    return entry, detail


def test_network_detail_by_id_over_mcp(fake_endpoint, tmp_path):
    """The id network returns must resolve through a second MCP call."""
    entry, detail = run_in_thread(
        drive_network_by_id(fake_endpoint, tmp_path / "session.trace.jsonl")
    )
    assert detail["url"] == entry["url"]


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
    names = outcomes[0].split(",")
    assert names[0] == "browse"
    assert {"goto", "goal", "console", "network", "read_js", "tabs", "wait_for"} <= set(names)
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
