"""The three browser modes: fresh, a persistent profile, and an attached Chrome.

Sync Playwright cannot start in a thread that already has an event loop (the
session-scoped fixture in the rest of the suite does), so every launch here runs
in its own thread — `in_thread` is that helper.

The profile test is the one that matters for real work: log in once (a cookie
here), and the next run — a separate browser process — still has it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from typing import Any

import pytest

from jevnav.browser import browser_session, pick_attached_page

pytest.importorskip("playwright.sync_api")


def in_thread(function: Any) -> Any:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = function()
        except BaseException as error:  # re-raised in the test thread
            box["error"] = error

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def test_a_fresh_session_starts_clean(tmp_path):
    marker = tmp_path / "marker.html"
    marker.write_text("<html><body><button>Go</button></body></html>")

    def run() -> None:
        with browser_session() as fresh:
            fresh.goto(marker.as_uri())
            fresh.context.add_cookies([{"name": "who", "value": "demo", "url": "http://127.0.0.1"}])
            assert fresh.context.cookies() != []
        with browser_session() as second:
            assert second.context.cookies() == []

    in_thread(run)


def test_a_persistent_profile_keeps_cookies_between_runs(tmp_path):
    profile = tmp_path / "chrome-profile"
    marker = tmp_path / "marker.html"
    marker.write_text("<html><body><button>Go</button></body></html>")

    def run() -> None:
        with browser_session(user_data_dir=str(profile)) as first:
            first.goto(marker.as_uri())
            first.context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": "logged-in",
                        "url": "http://127.0.0.1",
                        "expires": time.time() + 3600,  # session cookies die with the browser
                    }
                ]
            )
        assert profile.exists()
        with browser_session(user_data_dir=str(profile)) as second:
            cookies = {c["name"]: c["value"] for c in second.context.cookies()}
            assert cookies.get("session") == "logged-in"

    in_thread(run)


def test_a_persistent_profile_is_released_for_the_next_run(tmp_path):
    profile = tmp_path / "lock-profile"

    def run() -> None:
        for _ in range(2):
            with browser_session(user_data_dir=str(profile)) as page:
                page.goto("about:blank")
        shutil.rmtree(profile)  # fails if the profile is still locked
        assert not profile.exists()

    in_thread(run)


def test_pick_attached_page_prefers_web_pages(tmp_path):
    marker = tmp_path / "marker.html"
    marker.write_text("<html><body><button>Go</button></body></html>")

    def run() -> None:
        with browser_session() as page:
            page.goto(marker.as_uri())
            chosen = pick_attached_page(page.context)
            assert chosen.url.startswith("file://")

    in_thread(run)


@pytest.fixture
def external_chrome(tmp_path):
    """A Chrome started outside jevnav, listening on CDP — the `--cdp` case."""

    def executable() -> str:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            return playwright.chromium.executable_path

    binary = in_thread(executable)
    process = subprocess.Popen(
        [
            binary,
            "--headless=new",
            "--remote-debugging-port=0",
            f"--user-data-dir={tmp_path / 'external-profile'}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    endpoint = None
    deadline = time.time() + 30
    while time.time() < deadline and endpoint is None:
        line = process.stderr.readline()
        if not line and process.poll() is not None:
            break
        match = re.search(r"DevTools listening on (ws://\S+)", line or "")
        if match:
            endpoint = match.group(1)
    if endpoint is None:
        process.kill()
        pytest.skip("could not start an external Chrome for the CDP test")
    yield endpoint
    process.terminate()
    process.wait(timeout=10)


def test_attach_over_cdp_uses_the_running_browser(external_chrome, tmp_path):
    marker = tmp_path / "marker.html"
    marker.write_text("<html><body><button>Go</button></body></html>")

    def run() -> None:
        from playwright.sync_api import sync_playwright

        # the human's own browser already has a cookie and a page open
        with sync_playwright() as playwright:
            attached = playwright.chromium.connect_over_cdp(external_chrome)
            context = attached.contexts[0]
            context.add_cookies([{"name": "who", "value": "boss", "url": "http://127.0.0.1"}])
            context.new_page().goto(marker.as_uri())
            attached.close()

        with browser_session(cdp=external_chrome) as page:
            assert page.url.startswith("file://")
            cookies = {c["name"]: c["value"] for c in page.context.cookies()}
            assert cookies.get("who") == "boss"  # the session follows the attachment

    in_thread(run)


def test_cdp_attach_rejects_a_dead_endpoint():
    from playwright.sync_api import Error as PlaywrightError

    def run() -> None:
        with pytest.raises(PlaywrightError):
            with browser_session(cdp="http://127.0.0.1:9"):
                pass

    in_thread(run)


def test_attach_does_not_kill_the_browser_you_attached_to(external_chrome):
    def run() -> None:
        with browser_session(cdp=external_chrome) as page:
            page.goto("about:blank")

    in_thread(run)
    assert subprocess.run(["ps", "-p", "1"], capture_output=True).returncode == 0  # sanity
    time.sleep(0.5)
    assert _chrome_alive(external_chrome)


def _chrome_alive(endpoint: str) -> bool:
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    with socket.socket() as sock:
        sock.settimeout(2)
        return sock.connect_ex((parsed.hostname or "127.0.0.1", parsed.port or 9222)) == 0


def test_cli_exposes_the_browser_flags(tmp_path):
    from jevnav.cli import build_parser

    parser = build_parser()
    for command in ("run", "go", "replay"):
        argv = [command]
        if command == "run":
            argv.append(str(tmp_path / "flow.yaml"))
        if command == "go":
            argv += ["--goal", "x"]
        if command == "replay":
            argv.append(str(tmp_path / "t.jsonl"))
        argv += ["--user-data-dir", "/tmp/profile", "--cdp", "http://127.0.0.1:9222"]
        args = parser.parse_args(argv)
        assert args.user_data_dir == "/tmp/profile"
        assert args.cdp == "http://127.0.0.1:9222"
    mcp_args = parser.parse_args(
        ["mcp", "--user-data-dir", "/tmp/p", "--cdp", "http://127.0.0.1:9222"]
    )
    assert mcp_args.user_data_dir == "/tmp/p"
    assert mcp_args.cdp == "http://127.0.0.1:9222"


def test_a_trace_from_a_profile_run_leaks_nothing_about_the_profile(tmp_path):
    from helpers import FakeJev, fixture_url

    from jevnav.agent import run_goal
    from jevnav.gates import default_gates
    from jevnav.trace import TraceWriter

    profile = tmp_path / "profile"
    trace = tmp_path / "goal.trace.jsonl"

    def run() -> None:
        with browser_session(user_data_dir=str(profile)) as page:
            page.goto(fixture_url("loop-app.html"))
            page.context.add_cookies(
                [{"name": "secret", "value": "do-not-trace", "url": "http://127.0.0.1"}]
            )
            with TraceWriter(trace, flow="goal") as writer:
                run_goal(
                    "sign in",
                    page=page,
                    client=FakeJev(script=[]).client(),
                    gates=default_gates(),
                    writer=writer,
                    context={},
                )

    in_thread(run)
    text = trace.read_text()
    assert "do-not-trace" not in text
    assert str(profile) not in text
