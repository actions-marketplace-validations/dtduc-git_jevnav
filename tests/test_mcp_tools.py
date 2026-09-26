"""The observability side: console, network, dialogs, read_js, tabs, scroll."""

import json
from pathlib import Path

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
        file_root=tmp_path,
    )
    yield instance
    instance.close()


def make_session(page, tmp_path, **kwargs):
    kwargs.setdefault("file_root", tmp_path)
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
    # The MCP layer json-dumps this: Playwright objects must not leak, and the
    # id must be the one network_detail accepts.
    json.dumps(requests)
    entry = next(r for r in requests if "loop-app.html" in r["url"])
    detail = session.network_detail(id=entry["id"])
    assert detail["url"] == entry["url"]
    session.close()


def fake_request(recorder, url: str, status: int | None) -> dict:
    entry = {
        "id": recorder._next_network_id(),
        "method": "GET",
        "url": url,
        "status": status,
        "resource": "document",
        "request": None,
        "response": None,
    }
    recorder.network.append(entry)
    return entry


def test_network_ids_survive_only_failed_filtering(page, tmp_path):
    session = make_session(page, tmp_path)
    recorder = session._recorder()
    ok = fake_request(recorder, "https://example.test/ok", 200)
    broken = fake_request(recorder, "https://example.test/broken", 500)
    failed = session.network(limit=5, only_failed=True)["requests"]
    assert [entry["id"] for entry in failed] == [broken["id"]]
    detail = session.network_detail(id=broken["id"])
    assert detail["url"] == "https://example.test/broken"
    assert session.network_detail(id=ok["id"])["status"] == 200
    session.close()


def test_network_ids_stay_monotonic_when_the_ring_drops_old_entries(page, tmp_path):
    session = make_session(page, tmp_path)
    recorder = session._recorder()
    first = fake_request(recorder, "https://example.test/first", 200)
    for i in range(250):  # RING is 200: the first entry is long gone
        fake_request(recorder, f"https://example.test/{i}", 200)
    tail = session.network(limit=5)["requests"]
    assert [entry["url"] for entry in tail] == [
        f"https://example.test/{i}" for i in range(245, 250)
    ]
    assert all(entry["id"] > first["id"] for entry in tail)
    with pytest.raises(RuntimeError, match="no recorded request with id"):
        session.network_detail(id=first["id"])
    session.close()


def test_recorder_follows_the_active_tab(tmp_path):
    """A tab switch must not keep reading the previous tab's buffers."""
    session = Session(
        start=None,
        trace=str(tmp_path / "s.trace.jsonl"),
        client=FakeJev({}).client(),
        file_root=tmp_path,
        allow_file_urls=True,
    )
    try:
        session.goto(fixture_url("loop-app.html"))
        first = session._recorder()
        first_ids = [entry["id"] for entry in first.network]
        assert first_ids, "the first tab recorded its page load"
        session.new_page()
        second = session._recorder()
        assert second is not first
        assert session.network(limit=5)["requests"] == []  # fresh tab, fresh buffer
        session.goto(fixture_url("loop-app.html"))
        second_ids = [entry["id"] for entry in second.network]
        assert second_ids, "the second tab recorded its own page load"
        assert min(second_ids) > max(first_ids)  # ids keep counting across tabs
        session.select_page(0)
        assert session._recorder() is first  # reused, not re-attached
        assert [entry["id"] for entry in first.network] == first_ids  # history intact
        session.goto(fixture_url("loop-app.html"))
        added = len(first.network) - len(first_ids)
        # one entry per request: double listeners would record each request twice
        assert added == len(second_ids)
    finally:
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
    # geolocation needs a secure context, so read it from a localhost page
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(SimpleHTTPRequestHandler, directory=str(Path(__file__).parent / "fixtures")),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page.goto(f"http://127.0.0.1:{server.server_port}/loop-app.html")
        assert session.emulate(geolocation="10.5,106.5")["geolocation"] == "10.5,106.5"
        coords = page.evaluate(
            "() => new Promise((resolve) => navigator.geolocation.getCurrentPosition("
            "(p) => resolve([p.coords.latitude, p.coords.longitude]), () => resolve(null)))"
        )
        assert coords == [10.5, 106.5]
    finally:
        server.shutdown()
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
    assert metrics["Nodes"] > 0
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
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    if shutil.which("npx") is None:
        pytest.skip("npx is not installed")
    handler = partial(SimpleHTTPRequestHandler, directory=str(Path(__file__).parent / "fixtures"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        session = make_session(page, tmp_path)
        try:
            scores = session.lighthouse(
                f"http://127.0.0.1:{server.server_port}/loop-app.html", categories="performance"
            )["scores"]
        except RuntimeError as error:  # offline, no chrome, lighthouse refused
            pytest.skip(f"lighthouse did not run: {error}")
        assert "performance" in scores
        session.close()
    finally:
        server.shutdown()


def test_press_key_with_a_modifier(page, tmp_path):
    session = make_session(page, tmp_path)
    record_key = (
        'addEventListener("keydown", (e) => {'
        " window.last = [e.key, e.ctrlKey || e.metaKey, e.shiftKey]; })"
    )
    page.set_content(f"<input id=in><script>window.last = null;{record_key}</script>")
    page.click("#in")
    session.press_key("Control+Shift+K")
    assert page.evaluate("window.last") == ["K", True, True]
    session.close()


def test_fill_form_fills_several_fields_at_once(page, tmp_path):
    session = make_session(page, tmp_path)
    page.set_content(
        "<input id=a><input id=b><input id=c type=checkbox>"
        "<select id=d><option>one</option><option>two</option></select>"
    )
    out = session.fill_form(
        [
            {"selector": "#a", "value": "first"},
            {"selector": "#b", "value": "second"},
            {"selector": "#c", "action": "check", "value": True},
            {"selector": "#d", "action": "select", "value": "two"},
        ]
    )
    assert len(out["filled"]) == 4
    assert page.input_value("#a") == "first"
    assert page.input_value("#b") == "second"
    assert page.is_checked("#c") is True
    assert page.input_value("#d") == "two"
    # check accepts the true-like strings a model actually sends; anything else clears
    page.set_content("<input id=x type=checkbox>")
    for truthy in ("yes", "1", "on", True, None):
        page.uncheck("#x")
        session.fill_form([{"selector": "#x", "action": "check", "value": truthy}])
        assert page.is_checked("#x") is True, truthy
    session.fill_form([{"selector": "#x", "action": "check", "value": "off"}])
    assert page.is_checked("#x") is False
    session.close()


def test_fill_form_can_resolve_an_intent(page, tmp_path):
    session = make_session(page, tmp_path)
    session.client = FakeJev({"email address": "work email"}).client()
    page.set_content(
        "<label for=w>Work email</label><input id=w><label for=p>Phone</label><input id=p>"
    )
    session.fill_form([{"intent": "type the email address", "value": "a@b.c"}])
    assert page.input_value("#w") == "a@b.c"
    session.close()


def test_cpu_and_network_throttling_are_applied(page, tmp_path):
    session = make_session(page, tmp_path)
    applied = session.emulate(cpu_throttle=4, network_conditions="Slow 3G")
    assert applied["cpu_throttle"] == 4
    assert applied["network"]["download_kbps"] == 400
    assert applied["network"]["preset"] == "Slow 3G"
    with pytest.raises(ValueError, match="unknown network preset"):
        session.emulate(network_conditions="Dial-up")
    session.close()


def test_network_detail_returns_headers_and_body(page, tmp_path):
    session = make_session(page, tmp_path)
    session.route("**/api/thing", body='{"n": 1}', content_type="application/json")
    page.goto(fixture_url("loop-app.html"))
    page.evaluate("async () => await fetch('https://x.test/api/thing')")
    detail = session.network_detail(url_contains="/api/thing")
    assert detail["status"] == 200
    assert "application/json" in detail["response_headers"].get("content-type", "")
    assert detail["body"] == '{"n": 1}'
    with pytest.raises(RuntimeError, match="no recorded request matches"):
        session.network_detail(url_contains="/nothing-like-this")
    session.close()


def test_a_dialog_is_answered_and_recorded(page, tmp_path):
    session = make_session(page, tmp_path)  # default policy: dismiss
    page.set_content("<p>ready</p>")
    page.evaluate("setTimeout(() => { window.answer = confirm('delete it?') }, 30)")
    page.wait_for_timeout(200)
    assert page.evaluate("window.answer") is False
    dialogs = session.dialogs()["dialogs"]
    assert dialogs and dialogs[-1]["message"] == "delete it?"
    assert dialogs[-1]["action"] == "dismiss"
    session.close()


def test_a_dialog_rule_matches_the_message_text(page, tmp_path):
    session = make_session(page, tmp_path)
    session.dialog_policy("accept", match="delete")
    page.set_content("<p>ready</p>")
    page.evaluate("setTimeout(() => { window.answer = confirm('delete it?') }, 30)")
    page.wait_for_timeout(200)
    assert page.evaluate("window.answer") is True
    assert session.dialogs()["dialogs"][-1]["source"] == "rule:delete"
    session.close()


def test_the_policy_can_change_between_dialogs(page, tmp_path):
    session = make_session(page, tmp_path)
    page.set_content("<p>ready</p>")
    page.evaluate("setTimeout(() => { window.first = confirm('one?') }, 30)")
    page.wait_for_timeout(200)
    assert page.evaluate("window.first") is False
    assert session.dialog_policy("accept") == {"policy": "accept"}
    page.evaluate("setTimeout(() => { window.second = confirm('two?') }, 30)")
    page.wait_for_timeout(200)
    assert page.evaluate("window.second") is True
    session.close()


def test_dialog_policy_rejects_nonsense(page, tmp_path):
    session = make_session(page, tmp_path)
    with pytest.raises(ValueError, match="accept or dismiss"):
        session.dialog_policy("maybe")
    session.close()


def test_outline_describes_the_structure(page, tmp_path):
    session = make_session(page, tmp_path)
    page.goto(fixture_url("loop-app.html"))
    out = session.outline("main", limit=50)
    tags = [item["tag"] for item in out["elements"]]
    assert "h1" in tags and "button" in tags and "input" in tags
    heading = next(item for item in out["elements"] if item["tag"] == "h1")
    assert heading["level"] == 1
    assert "Acme Console" in heading["text"]
    button = next(item for item in out["elements"] if item["tag"] == "button")
    assert button["box"][2] > 0
    session.close()


def test_styles_returns_computed_values(page, tmp_path):
    session = make_session(page, tmp_path)
    page.set_content('<button id=go style="font-size:19px;border-radius:7px">Go</button>')
    out = session.styles("#go", ["font-size", "border-radius", "display"])
    element = out["elements"][0]
    assert element["element"] == "button#go"
    assert element["styles"]["font-size"] == "19px"
    assert element["styles"]["border-radius"] == "7px"
    assert element["styles"]["display"] == "inline-block"
    session.close()


def test_the_default_style_props_cover_the_visual_basics(page, tmp_path):
    session = make_session(page, tmp_path)
    page.set_content("<p>hi</p>")
    styles = session.styles("p")["elements"][0]["styles"]
    assert {"display", "font-size", "color", "background-color"} <= set(styles)
    session.close()


def test_no_eval_closes_the_read_js_channel(page, tmp_path):
    session = make_session(page, tmp_path, allow_eval=False)
    page.set_content("<p>hi</p>")
    with pytest.raises(RuntimeError, match="no-eval"):
        session.read_js("document.title")
    session.close()


def test_fill_form_by_intent_hands_back_a_stable_selector(page, tmp_path):
    """The selector a caller may reuse later must not be a position-based cid."""
    session = make_session(page, tmp_path)
    session.client = FakeJev({"work email": "work email"}).client()
    page.set_content("<label for=w>Work email</label><input id=w>")
    out = session.fill_form([{"intent": "type the work email", "value": "a@b.c"}])
    selector = out["filled"][0]["selector"]
    assert selector == 'role=textbox[name="Work email"]'
    assert "data-jevcid" not in selector
    assert page.input_value("#w") == "a@b.c"
    session.close()


def test_max_candidates_limits_what_the_model_sees(page, tmp_path):
    session = make_session(page, tmp_path, max_candidates=2)
    page.set_content("<button>one</button><button>two</button><button>three</button>")
    fake = FakeJev({"click one": "one"})
    session.client = fake.client()
    session.browse("click one of them", "click", None)
    criteria = fake.calls[0]["questions"]["target"]["criteria"]
    assert len(criteria) == 3  # two candidates + none
    session.close()


def test_goto_rejects_file_urls_without_the_opt_in(page, tmp_path):
    session = make_session(page, tmp_path)
    with pytest.raises(ValueError, match="allow-file-urls"):
        session.goto(fixture_url("loop-app.html"))
    session.close()


def test_upload_files_refuses_paths_outside_the_file_root(page, tmp_path):
    session = make_session(page, tmp_path)
    page.set_content("<input id=f type=file>")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the file root"):
        session.upload_files([str(outside)])
    session.close()


def test_screenshot_refuses_a_path_outside_the_file_root(page, tmp_path):
    session = make_session(page, tmp_path)
    page.set_content("<h1>x</h1>")
    with pytest.raises(ValueError, match="outside the file root"):
        session.screenshot(str(tmp_path.parent / "shot.png"))
    session.close()


def test_lighthouse_rejects_a_non_http_url(page, tmp_path):
    session = make_session(page, tmp_path)
    with pytest.raises(ValueError, match="http"):
        session.lighthouse("file:///tmp/audit.html")
    session.close()


def test_fill_form_intent_goes_through_the_gate(page, tmp_path):
    session = make_session(page, tmp_path)
    session.client = FakeJev({"work email": "work email"}, confidence=0.4).client()
    page.set_content("<label for=w>Work email</label><input id=w>")
    out = session.fill_form([{"intent": "type the work email", "value": "a@b.c"}])
    entry = out["filled"][0]
    assert entry["status"] == "review" and entry["executed"] is False
    assert page.input_value("#w") == ""  # nothing was typed
    session.close()


def test_fill_form_intent_risky_pattern_is_reviewed(page, tmp_path):
    session = make_session(page, tmp_path)
    session.client = FakeJev({"delete": "Delete account"}).client()
    page.set_content("<label for=w>Delete account</label><input id=w>")
    out = session.fill_form([{"intent": "delete the account", "value": "x"}])
    assert out["filled"][0]["status"] == "review"
    assert out["filled"][0]["executed"] is False
    session.close()


def test_new_page_rejects_file_urls_without_the_opt_in(page, tmp_path):
    session = make_session(page, tmp_path)
    with pytest.raises(ValueError, match="allow-file-urls"):
        session.new_page("file:///tmp/page.html")
    session.close()


def test_upload_files_resolves_relative_paths_under_the_root(page, tmp_path):
    session = make_session(page, tmp_path)
    page.set_content("<input id=f type=file>")
    (tmp_path / "cv.pdf").write_text("pdf", encoding="utf-8")
    out = session.upload_files(["cv.pdf"])
    assert out["files"] == [str((tmp_path / "cv.pdf").resolve())]
    session.close()


def test_upload_files_refuses_a_symlink_out_of_the_root(page, tmp_path):
    session = make_session(page, tmp_path)
    page.set_content("<input id=f type=file>")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="outside the file root"):
        session.upload_files(["link.txt"])
    session.close()


def test_upload_files_refuses_an_unbounded_default_root(page, tmp_path, monkeypatch):
    monkeypatch.chdir(Path.home())
    session = make_session(page, tmp_path)
    session.file_root = Path.home().resolve()
    session._file_root_unbounded = True
    page.set_content("<input id=f type=file>")
    (tmp_path / "cv.pdf").write_text("pdf", encoding="utf-8")
    with pytest.raises(ValueError, match="explicit --file-root"):
        session.upload_files([str(tmp_path / "cv.pdf")])
    session.close()


def test_screenshot_refuses_an_unbounded_default_root(page, tmp_path):
    session = make_session(page, tmp_path)
    session._file_root_unbounded = True
    page.set_content("<h1>x</h1>")
    with pytest.raises(ValueError, match="explicit --file-root"):
        session.screenshot()
    session.close()


def test_upload_files_intent_is_gated_and_stepped(page, tmp_path):
    session = make_session(page, tmp_path)
    session.client = FakeJev({"the resume": "Resume"}, confidence=0.4).client()
    page.set_content(
        '<input id=a type=file aria-label="Resume"><input id=b type=file aria-label="Cover">'
    )
    (tmp_path / "cv.pdf").write_text("pdf", encoding="utf-8")
    out = session.upload_files([str(tmp_path / "cv.pdf")], intent="attach the resume")
    assert out["status"] == "review" and out["executed"] is False
    assert len(session.steps) == 1
    step = session.steps[0]
    assert step["intent"] == "attach the resume"
    assert step["gate"]["verdict"] == "review"
    assert step["result"]["executed"] is False
    session.close()


def test_replay_execute_handles_an_intent_resolve_step(page, tmp_path):
    """An intent-gate decision must not break `replay --execute`."""
    from jevnav.replay import replay_trace

    session = make_session(page, tmp_path, allow_file_urls=True)
    session.client = FakeJev({"you@example.com": "Email"}).client()
    session.goto(fixture_url("loop-app.html"))
    out = session.fill_form([{"intent": "type into the you@example.com field", "value": "a@b.c"}])
    assert out["filled"][0]["executed"] is True
    session.close()

    result = replay_trace(str(tmp_path / "s.trace.jsonl"), page=page, execute=True)
    assert result["failed"] == []
    assert result["counts"].get("error", 0) == 0


def test_trace_stop_without_start_is_an_error(page, tmp_path):
    session = make_session(page, tmp_path)
    with pytest.raises(Exception, match="start tracing"):
        session.trace_stop()
    session.close()
