"""The pytest plugin: intent actions, gating and a trace, inside an ordinary test."""

import json

import pytest
from helpers import FakeJev

from jevnav.gates import default_gates
from jevnav.pytest_plugin import JevNavigator
from jevnav.trace import TraceWriter, read_trace


@pytest.fixture
def navigator(page, tmp_path):
    fake = FakeJev(
        {"sign in": "sign in", "email": "email", "password": "Password", "delete": "delete renew"}
    )
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


def test_an_env_value_is_traced_by_name_only_and_replays(navigator, app_url, page, monkeypatch):
    from jevnav.replay import replay_trace

    monkeypatch.setenv("DEMO_PASSWORD", "hunter2-super-secret")
    jev, writer = navigator
    jev.goto(app_url)
    jev.fill("the password field", "${DEMO_PASSWORD}")
    assert jev.page.locator("#login-password").input_value() == "hunter2-super-secret"
    writer.close()
    assert "hunter2-super-secret" not in writer.path.read_text()
    _, steps = read_trace(writer.path)
    assert steps[0]["action"] == {"type": "fill", "value_from_env": "DEMO_PASSWORD"}
    result = replay_trace(writer.path, page=page, execute=True)
    assert result["failed"] == []


def test_an_env_literal_is_traced_by_name_with_a_warning(navigator, app_url, monkeypatch):
    """os.environ[...] is the idiomatic reflex; it must not leak the value."""
    monkeypatch.setenv("DEMO_PASSWORD", "hunter2-super-secret")
    jev, writer = navigator
    jev.goto(app_url)
    with pytest.warns(UserWarning, match="DEMO_PASSWORD"):
        jev.fill("the password field", "hunter2-super-secret")
    assert jev.page.locator("#login-password").input_value() == "hunter2-super-secret"
    writer.close()
    assert "hunter2-super-secret" not in writer.path.read_text()
    _, steps = read_trace(writer.path)
    assert steps[0]["action"] == {"type": "fill", "value_from_env": "DEMO_PASSWORD"}


def test_the_secret_name_pattern_anchors_on_tokens():
    from jevnav.pytest_plugin import secret_env_name

    caught = [
        "ACME_API_TOKEN",
        "AWS_SECRET_KEY",
        "PASSWORD123",
        "DB_PASSWORD",
        "API_TOKEN",
        "CLIENT_SECRET",
        "SERVICE_CREDENTIALS",
        "TOKEN",
        "APIKEY",
        "SECRETKEY",
        "AUTHTOKEN",
        "GITHUB_APIKEY",
        "STRIPE_SECRETKEY",
    ]
    spared = [
        "PROJECT_DIR",
        "ZZZ_OLD_COPY",
        "COMPASS_DIR",
        "MONKEY_PATCH_DIR",
        "KEYCHAIN_PATH",
        "PASSENGER_ROOT",
        "AUTHOR_NAME",
        "BYPASS_CACHE",
        "OAUTH_REDIRECT_URI",
        "PATH",
    ]
    assert all(secret_env_name([name]) == name for name in caught), caught
    assert all(secret_env_name([name]) is None for name in spared), spared


def test_a_path_that_matches_an_env_value_stays_a_literal(navigator, app_url, monkeypatch):
    """$PWD is not a secret; rewriting it would replay as another machine's path."""
    monkeypatch.setenv("PROJECT_DIR", "/Users/demo/work/acme-console")
    jev, writer = navigator
    jev.goto(app_url)
    with pytest.warns(UserWarning, match="kept as a literal"):
        jev.fill("the email address", "/Users/demo/work/acme-console")
    writer.close()
    _, steps = read_trace(writer.path)
    assert steps[0]["action"] == {"type": "fill", "value": "/Users/demo/work/acme-console"}


def test_a_missing_env_value_fails_before_the_paid_call(page, tmp_path, app_url, monkeypatch):
    monkeypatch.delenv("ACME_PASSWORD", raising=False)
    fake = FakeJev({"password": "Password"})
    writer = TraceWriter(tmp_path / "missing.trace.jsonl", flow="pytest:missing")
    jev = JevNavigator(page, fake.client(), writer, gates=default_gates())
    jev.goto(app_url)
    with pytest.raises(KeyError, match="ACME_PASSWORD"):
        jev.fill("the password field", "${ACME_PASSWORD}")
    assert fake.calls == []  # the decision was never requested
    writer.close()


def test_clearing_a_field_is_recorded_and_replays(navigator, app_url, page):
    from jevnav.replay import replay_trace

    jev, writer = navigator
    jev.goto(app_url)
    jev.fill("the email address", "demo@example.com")
    jev.fill("the email address", "")
    assert jev.page.locator("#login-email").input_value() == ""
    writer.close()
    _, steps = read_trace(writer.path)
    assert steps[1]["action"] == {"type": "fill", "value": ""}
    result = replay_trace(writer.path, page=page, execute=True)
    assert result["failed"] == []
    assert page.locator("#login-email").input_value() == ""


def test_a_press_records_its_key_and_replays(navigator, app_url, page):
    from jevnav.replay import replay_trace

    jev, writer = navigator
    jev.goto(app_url)
    jev.press("the email address", "Enter")
    writer.close()
    _, steps = read_trace(writer.path)
    assert steps[0]["action"] == {"type": "press", "key": "Enter"}
    result = replay_trace(writer.path, page=page, execute=True)
    assert result["failed"] == []


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
