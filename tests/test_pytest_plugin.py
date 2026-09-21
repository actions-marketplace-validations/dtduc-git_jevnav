"""The pytest plugin: intent actions, gating and a trace, inside an ordinary test."""

import json

import pytest
from helpers import FakeJev

from jevnav.gates import default_gates
from jevnav.pytest_plugin import JevNavigator
from jevnav.trace import TraceWriter, read_trace


@pytest.fixture
def navigator(page, tmp_path):
    fake = FakeJev({"sign in": "sign in", "email": "email", "delete": "delete renew"})
    writer = TraceWriter(tmp_path / "test_login.trace.jsonl", flow="pytest:test_login")
    instance = JevNavigator(page, fake.client(), writer, gates=default_gates())
    yield instance, writer
    writer.close()


def test_actions_are_intents_and_the_trace_is_replayable(navigator, tmp_path, app_url):
    jev, writer = navigator
    jev.goto(app_url)
    result = jev.click("sign in to the account")
    assert result["target"] == "Sign in"
    jev.fill("the email address", "demo@example.com")
    jev.expect("#login-submit")
    _, steps = read_trace(writer.path)
    assert [step["intent"] for step in steps] == [
        "sign in to the account",
        "the email address",
    ]
    assert all(step["gate"]["verdict"] == "auto" for step in steps)
    assert steps[0]["locator"]["selector"].startswith("role=")
    assert steps[1]["action"] == {"type": "fill", "value": "demo@example.com"}


def test_a_risky_intent_fails_the_test_before_it_acts(navigator, app_url):
    jev, writer = navigator
    jev.goto(app_url)
    with pytest.raises(BaseException) as failure:
        jev.click("delete the task about renewing the TLS certificate")
    assert "review" in str(failure.value)
    _, steps = read_trace(writer.path)
    assert steps[0]["gate"]["verdict"] == "review"
    assert steps[0]["result"]["executed"] is False
    assert "Renew TLS" in jev.page.content()  # nothing was clicked


def test_expect_fails_on_the_page_not_on_the_model(navigator, app_url):
    jev, _ = navigator
    jev.goto(app_url)
    with pytest.raises(BaseException) as failure:
        jev.expect("#not-here", timeout_ms=200)
    assert "not-here" in str(failure.value)


def test_the_plugin_registers_its_options():
    from jevnav import pytest_plugin

    class FakeParser:
        def __init__(self):
            self.options = []

        def getgroup(self, name):
            parser_self = self

            class Group:
                def addoption(self, *args, **kwargs):
                    parser_self.options.append(args[0])

            return Group()

    parser = FakeParser()
    pytest_plugin.pytest_addoption(parser)
    assert "--jev-trace-dir" in parser.options
    assert "--jev-gates" in parser.options


def test_the_plugin_is_discoverable_as_an_entry_point():
    from importlib.metadata import entry_points

    names = {point.name: point.value for point in entry_points(group="pytest11")}
    assert names.get("jevnav") == "jevnav.pytest_plugin"


def test_the_trace_it_writes_replays_offline(navigator, app_url, page):
    from jevnav.replay import replay_trace

    jev, writer = navigator
    jev.goto(app_url)
    jev.click("sign in to the account")
    writer.close()
    result = replay_trace(writer.path, page=page)
    assert result["failed"] == []
    assert result["results"][0]["verdict"] in {"ok", "moved"}


def test_the_trace_file_names_follow_the_test():
    # the fixture names files after the test node, so CI can map a failure to a trace
    name = "test_login_with_a_long_name"
    import re

    assert re.sub(r"[^A-Za-z0-9_.-]+", "_", name) == name
    payload = json.loads(json.dumps({"flow": f"pytest:{name}"}))
    assert payload["flow"] == "pytest:test_login_with_a_long_name"


def test_the_fixture_body_yields_a_navigator_with_a_real_browser(tmp_path, monkeypatch, app_url):
    """Exercise the fixture itself (browser + trace) in its own thread."""
    import threading

    from jevnav import pytest_plugin

    monkeypatch.setattr(pytest_plugin, "_client", lambda: FakeJev({"sign in": "sign in"}).client())
    box: dict = {}

    class Config:
        def getoption(self, name):
            return {
                "--jev-trace-dir": str(tmp_path),
                "--jev-gates": None,
                "--jev-headed": False,
            }[name]

    class Request:
        config = Config()

        class node:
            name = "test_fixture_body"

    def run() -> None:
        generator = pytest_plugin.jev.__wrapped__(Request())
        navigator = next(generator)
        try:
            navigator.goto(app_url)
            navigator.click("sign in to the account")
            box["trace"] = navigator.trace_path()
            box["target"] = navigator.page.locator("#login-submit").count()
        finally:
            generator.close()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=120)
    assert not thread.is_alive()
    assert box["trace"].name == "test_fixture_body.trace.jsonl"
    assert box["target"] == 1
    _, steps = read_trace(box["trace"])
    assert steps[0]["gate"]["verdict"] == "auto"
