"""The observability side: console, network, dialogs, read_js, tabs, scroll."""

import pytest
from helpers import FakeJev, fixture_url

from jevnav.mcp import Session


@pytest.fixture
def live_session(page, app_url, tmp_path):
    """A session over the shared test browser, with the recorder attached."""
    instance = Session(
        start=None,
        trace=str(tmp_path / "session.trace.jsonl"),
        client=FakeJev({}).client(),
        page=page,
    )
    yield instance
    instance.close()


def make_session(page, tmp_path, **kwargs):
    return Session(
        start=None,
        trace=str(tmp_path / "s.trace.jsonl"),
        client=FakeJev({}).client(),
        page=page,
        **kwargs,
    )


def test_the_browser_is_not_launched_until_a_page_tool_is_used(app_url, tmp_path):
    session = Session(
        start=None, trace=str(tmp_path / "s.trace.jsonl"), client=FakeJev({}).client(), model="m"
    )
    try:
        assert session.browser is None
        assert session.summary() == {"steps": 0}  # no browser needed
        state = session.page_state()  # this one needs it
        assert session.browser is not None
        assert state["url"] == "about:blank"
    finally:
        session.close()


def test_console_collects_messages_and_page_errors(page, tmp_path):
    session = make_session(page, tmp_path)
    page.goto(fixture_url("loop-app.html"))
    page.evaluate("console.error('boom'); setTimeout(() => { throw new Error('later') }, 0)")
    page.wait_for_timeout(200)
    messages = session.console(limit=10, only_errors=True)["messages"]
    assert any("boom" in m["text"] for m in messages)
    assert any(m["type"] == "pageerror" for m in messages)
    session.close()


def test_network_collects_requests(page, tmp_path):
    session = make_session(page, tmp_path)
    page.goto(fixture_url("loop-app.html"))
    requests = session.network(limit=20)["requests"]
    assert any("loop-app.html" in r["url"] for r in requests)
    session.close()


def test_a_dialog_is_recorded_not_swallowed(page, tmp_path):
    session = make_session(page, tmp_path)
    page.goto(fixture_url("loop-app.html"))
    confirmed = session.read_js("confirm('really delete?')")
    assert confirmed is False  # dismissed by the default policy...
    dialogs = session.dialogs()["dialogs"]
    assert dialogs and dialogs[-1]["message"] == "really delete?"  # ...and recorded
    session.close()


def test_the_dialog_policy_can_accept(page, tmp_path):
    session = make_session(page, tmp_path, dialog_policy="accept")
    page.goto(fixture_url("loop-app.html"))
    assert session.read_js("confirm('sure?')") is True
    assert session.dialogs()["dialogs"][-1]["action"] == "accept"
    session.close()


def test_read_js_returns_page_values(page, tmp_path):
    session = make_session(page, tmp_path)
    page.goto(fixture_url("loop-app.html"))
    assert session.read_js("document.title") == "Acme Console"
    session.close()


def test_scroll_and_wait_for(page, tmp_path):
    session = make_session(page, tmp_path)
    page.goto(fixture_url("loop-app.html"))
    assert "elements" in session.scroll("down", 400)
    assert session.wait_for(text="Sign in")["title"] == "Acme Console"
    session.close()


def test_tabs_need_jevnavs_own_browser(page, tmp_path):
    session = make_session(page, tmp_path)
    with pytest.raises(RuntimeError, match="own browser"):
        session.new_page()
    session.close()
