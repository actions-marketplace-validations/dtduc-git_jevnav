import json

import pytest
from helpers import FakeJev

from jevnav import cli

ANSWERS = {
    "Sign in to the existing account": "sign in",
    "Type the password": "password",
    "Delete the task about shipping release notes": "delete ship release notes",
    "Permanently delete the whole account": "none",
}


@pytest.fixture(autouse=True)
def offline_jev(monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda: FakeJev(ANSWERS).client())


@pytest.fixture
def session(app_url, tmp_path, page):
    from jevnav.mcp import Session

    instance = Session(
        start=app_url,
        trace=str(tmp_path / "session.trace.jsonl"),
        page=page,
        client=FakeJev(ANSWERS).client(),
    )
    yield instance
    instance.close()


def test_browse_executes_an_auto_decision(session):
    out = session.browse("Sign in to the existing account", "click", None)
    assert out["status"] == "auto"
    assert out["confidence"] > 0.9
    assert out["target"]["selector"] == 'role=button[name="Sign in"]'
    assert session.steps[0]["result"]["executed"] is True


def test_browse_never_executes_a_risky_decision(session):
    out = session.browse("Delete the task about shipping release notes", "click", None)
    assert out["status"] == "review"
    assert "risky" in out["reason"]
    assert session.steps[0]["result"]["executed"] is False


def test_browse_can_use_the_callers_own_confidence_bar(session):
    from helpers import fixture_url

    session.page.goto(fixture_url("loop-app.html"))
    session.client = FakeJev({"Sign in": ("sign in", 0.72)}).client()
    refused = session.browse("Sign in to the account", "click", None)
    assert refused["status"] == "review"
    allowed = session.browse("Sign in to the account", "click", None, min_confidence=0.7)
    assert allowed["status"] == "auto"


def test_browse_returns_alternatives_when_it_wants_a_human(session):
    out = session.browse("Delete the task about shipping release notes", "click", None)
    assert out["status"] == "review"
    assert out["alternatives"]
    assert "browse again" in out["hint"]


def test_browse_reports_a_blocked_decision(session):
    out = session.browse("Permanently delete the whole account and all of its data", "click", None)
    assert out["status"] == "blocked"
    assert out["target"]["selector"] is None


def test_browse_fills_when_allowed(session):
    out = session.browse("Type the password", "fill", "hunter2")
    assert out["status"] in {"auto", "review"}
    if out["status"] == "auto":
        assert session.page.input_value("#login-password") == "hunter2"


def test_page_state_lists_candidates(session):
    state = session.page_state()
    assert state["title"].startswith("Acme Console")
    assert state["elements"] > 10
    assert any(c["name"] == "Sign in" for c in state["candidates"])


def test_summary_counts_gates(session):
    session.browse("Sign in to the existing account", "click", None)
    session.browse("Delete the task about shipping release notes", "click", None)
    summary = session.summary()
    assert summary["steps"] == 2
    assert summary["auto"] == 1 and summary["review"] == 1


def test_session_trace_is_replayable(session, tmp_path):
    from jevnav.replay import replay_trace
    from jevnav.trace import read_trace

    session.browse("Sign in to the existing account", "click", None)
    path = tmp_path / "session.trace.jsonl"
    run, steps = read_trace(path)
    assert run["flow"] == "mcp-session"
    assert len(steps) == 1
    result = replay_trace(path, page=session.page)
    assert result["failed"] == []


def test_a_second_session_archives_the_previous_trace(app_url, tmp_path):
    from jevnav.mcp import Session

    trace = tmp_path / "session.trace.jsonl"
    trace.write_text('{"kind": "run"}\n{"kind": "step", "step": 1}\n')
    instance = Session(start=app_url, trace=str(trace), page=None, client=FakeJev({}).client())
    try:
        assert trace.exists()
        assert len(trace.read_text().splitlines()) == 1  # the fresh header only
        archived = list(tmp_path.glob("session.trace.*.jsonl"))
        assert len(archived) == 1
        assert "kind" in archived[0].read_text()
    finally:
        instance.close()


def test_browse_rejects_unknown_actions(session):
    out = session.browse("Sign in", "teleport", None)
    assert out["status"] == "error"
    assert "unknown action" in out["error"]


def test_tools_are_registered(monkeypatch):
    pytest.importorskip("mcp")
    import jevnav.mcp as mcp_module

    captured = {}

    class FakeServer:
        def __init__(self, name):
            captured["name"] = name
            captured["tools"] = []

        def tool(self):
            def register(fn):
                captured["tools"].append(fn.__name__)
                return fn

            return register

        def run(self):
            captured["ran"] = True

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def close(self):
            pass

        def browse(self, *args):
            return {}

        def goto(self, url):
            return {"url": url}

        def page_state(self):
            return {}

        def summary(self):
            return {}

    monkeypatch.setattr(mcp_module, "server_class", lambda: FakeServer)
    monkeypatch.setattr(mcp_module, "Session", FakeSession)
    assert mcp_module.serve(start=None, trace=None, gates=None) == 0
    assert captured["name"] == "jevnav"
    assert captured["tools"] == ["browse", "goto", "goal", "page_state", "summary"]
    assert captured["ran"] is True


def test_mcp_returns_json(session, monkeypatch):
    pytest.importorskip("mcp")
    payload = json.loads(
        json.dumps(session.browse("Sign in to the existing account", "click", None))
    )
    assert payload["status"] == "auto"


def test_goal_tool_drives_the_browser(session):
    from helpers import fixture_url

    session.page.goto(fixture_url("loop-app.html"))
    session.client = FakeJev(
        script=[
            {"action": "fill", "target": "Email", "value_key": "email"},
            {"action": "click", "target": "Sign in"},
        ]
    ).client()
    from helpers import fixture_url

    outcome = session.goal(
        "sign in with the demo account",
        {"email": "demo@example.com"},
        success="#sign-out",
    )
    assert outcome["status"] == "done"
    assert outcome["verified"] is True
    assert outcome["steps"] == 3
    assert session.page.is_visible("#signed-in-as")
    assert session.summary()["steps"] == 3


def test_goal_tool_never_acts_on_a_review(session):
    from helpers import fixture_url

    session.page.goto(fixture_url("loop-app.html"))
    session.client = FakeJev(script=[{"action": "click", "target": "Home"}]).client()
    outcome = session.goal("delete my account", {})
    assert outcome["status"] == "review"
    assert session.page.locator("#nav-home").is_visible()
