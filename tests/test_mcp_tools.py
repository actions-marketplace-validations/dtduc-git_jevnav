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


def test_screenshot_writes_a_png(page, tmp_path):
    session = make_session(page, tmp_path)
    page.set_content("<h1>hello</h1>")
    out = session.screenshot(str(tmp_path / "shot.png"))
    assert out["bytes"] > 0
    assert (tmp_path / "shot.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    session.close()


def test_upload_files_sets_a_hidden_file_input(page, tmp_path):
    session = make_session(page, tmp_path)
    upload = tmp_path / "avatar.txt"
    upload.write_text("hello")
    page.set_content('<input type="file" aria-label="Avatar" style="display:none">')
    out = session.upload_files([str(upload)], selector="input[type=file]")
    assert out["files"] == [str(upload)]
    assert page.evaluate("document.querySelector('input').files[0].name") == "avatar.txt"
    session.close()


def test_upload_files_can_let_jev_choose_the_input(page, tmp_path):
    session = make_session(page, tmp_path)
    session.client = FakeJev({"upload the avatar": "avatar"}).client()
    upload = tmp_path / "avatar.txt"
    upload.write_text("hello")
    page.set_content(
        '<input type="file" aria-label="Avatar" style="display:none">'
        '<input type="file" aria-label="Resume" style="display:none">'
    )
    out = session.upload_files([str(upload)], intent="upload the avatar")
    assert out["target"] == "Avatar"
    session.close()


def test_upload_files_refuses_a_missing_path(page, tmp_path):
    session = make_session(page, tmp_path)
    with pytest.raises(FileNotFoundError):
        session.upload_files([str(tmp_path / "nope.txt")], selector="input")
    session.close()


def test_drag_moves_an_element(page, tmp_path):
    session = make_session(page, tmp_path)
    page.set_content(
        """
        <div id="src" style="width:40px;height:40px;background:#333"></div>
        <div id="dst" style="width:80px;height:80px;background:#eee;margin-top:40px"></div>
        <script>
          const src = document.getElementById('src');
          src.addEventListener('mousedown', () => { src.dataset.dragging = '1'; });
          document.getElementById('dst').addEventListener('mouseup', () => {
            if (src.dataset.dragging) document.body.dataset.dropped = 'yes';
          });
        </script>
        """
    )
    session.drag(source_selector="#src", target_selector="#dst")
    assert page.evaluate("document.body.dataset.dropped") == "yes"
    session.close()


def test_resize_and_emulate(page, tmp_path):
    session = make_session(page, tmp_path)
    assert session.resize(800, 600) == {"viewport": {"width": 800, "height": 600}}
    page.set_content("<p id=x>hi</p>")
    assert page.evaluate("innerWidth") == 800
    session.emulate(color_scheme="dark")
    assert page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches") is True
    assert session.emulate(geolocation="10.5,106.5")["geolocation"] == "10.5,106.5"
    session.close()


def test_route_stubs_and_unroutes(page, tmp_path):
    session = make_session(page, tmp_path)
    session.route("**/api/data", body='{"stubbed": true}')
    page.goto(fixture_url("loop-app.html"))
    value = page.evaluate("async () => (await fetch('https://x.test/api/data')).json()")
    assert value == {"stubbed": True}
    session.unroute("**/api/data")
    session.close()


def test_route_can_abort(page, tmp_path):
    session = make_session(page, tmp_path)
    session.route("**/blocked", abort=True)
    page.goto(fixture_url("loop-app.html"))
    js = (
        "async () => { try { await fetch('https://x.test/blocked'); return false; }"
        " catch { return true; } }"
    )
    failed = page.evaluate(js)
    assert failed is True
    session.close()


def test_a_playwright_trace_can_be_recorded(page, tmp_path):
    session = make_session(page, tmp_path)
    trace = tmp_path / "pw-trace.zip"
    assert session.trace_start(screenshots=False)["tracing"] is True
    page.goto(fixture_url("loop-app.html"))
    out = session.trace_stop(str(trace))
    assert out["bytes"] > 0
    assert trace.read_bytes()[:2] == b"PK"  # a zip
    assert "show-trace" in out["open_with"]
    session.close()


def test_perf_metrics_returns_chromium_counters(page, tmp_path):
    session = make_session(page, tmp_path)
    page.goto(fixture_url("loop-app.html"))
    metrics = session.perf_metrics()["metrics"]
    assert "JSHeapUsedSize" in metrics
    assert metrics["TaskDuration"] >= 0
    session.close()


def test_heap_snapshot_writes_a_file(page, tmp_path):
    session = make_session(page, tmp_path)
    page.goto(fixture_url("loop-app.html"))
    target = tmp_path / "heap.heapsnapshot"
    out = session.heap_snapshot(str(target))
    assert out["bytes"] > 1000
    assert target.read_text()[:1] == "{"
    session.close()


def test_lighthouse_scores_when_npx_is_available(page, tmp_path):
    import shutil

    if shutil.which("npx") is None:
        pytest.skip("npx is not installed")
    session = make_session(page, tmp_path)
    try:
        scores = session.lighthouse(fixture_url("loop-app.html"), categories="performance")[
            "scores"
        ]
    except RuntimeError as error:  # offline or lighthouse refused
        pytest.skip(f"lighthouse did not run: {error}")
    assert "performance" in scores
    session.close()
