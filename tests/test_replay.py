import pytest
from helpers import FakeJev

from jevnav.flow import load_flow, run_flow
from jevnav.gates import default_gates
from jevnav.page import extract
from jevnav.replay import action_for_replay, replay_step, replay_trace
from jevnav.trace import TraceWriter, read_trace

ANSWERS = {
    "Sign in to the existing account": "sign in",
    "Type the password": "password",
    "Submit the login form": "sign in",
}


@pytest.fixture
def trace_path(tmp_path, page, app_url):
    path = tmp_path / "demo.trace.jsonl"
    flow = load_flow(
        _write(
            tmp_path,
            f"""id: demo
start: {app_url}
steps:
  - intent: "Sign in to the existing account"
    action: click
  - intent: "Type the password"
    action: fill
    value: "${{DEMO_PASSWORD}}"
  - intent: "Submit the login form"
    action: click
""",
        )
    )
    with TraceWriter(path, flow="demo", model="m") as writer:
        run_flow(
            flow,
            page=page,
            client=FakeJev(ANSWERS).client(),
            gates=default_gates(),
            writer=writer,
            model="m",
        )
    return path


def _write(tmp_path, body: str) -> str:
    path = tmp_path / "flow.yaml"
    path.write_text(body)
    return str(path)


def test_replay_of_an_unchanged_page_is_all_ok(trace_path, page):
    result = replay_trace(trace_path, page=page)
    assert result["failed"] == []
    assert result["counts"] == {"ok": 3}
    assert all(step["page_identical"] for step in result["results"])


def test_replay_flags_the_changed_targets_only(trace_path, page, mutated_url):
    result = replay_trace(trace_path, page=page, swap=mutated_url)
    verdicts = {step["step"]: step["verdict"] for step in result["results"]}
    assert verdicts == {1: "changed", 2: "ok", 3: "changed"}
    assert result["failed"] == [1, 3]
    first = result["results"][0]
    assert "button|sign in" in first["reason"]
    assert first["drift"]["missing"] > 0


def test_replay_reports_drift_without_failing_on_other_changes(trace_path, page, mutated_url):
    result = replay_trace(trace_path, page=page, swap=mutated_url)
    password_step = result["results"][1]
    assert password_step["page_identical"] is False
    assert password_step["drift"]["new"] + password_step["drift"]["missing"] > 0


def test_an_identical_page_is_not_moved_by_a_newer_shortlist(tmp_path, page):
    """A 0.1.0 trace replayed by a later extractor: re-ranking is not movement."""
    site = tmp_path / "two.html"
    site.write_text(
        "<html><body><button id=a>Alpha</button><button id=b>Beta</button></body></html>"
    )
    page.goto(site.as_uri())
    candidates, _, _ = extract(page)
    chosen = candidates[0]
    path = tmp_path / "old-order.trace.jsonl"
    with TraceWriter(path, flow="x") as writer:
        writer.step(
            step=1,
            intent="click alpha",
            url=page.url,
            title="t",
            candidates=list(reversed(candidates)),  # an older extractor's order
            decision={"choice": chosen["cid"], "confidence": 0.99},
        )
    result = replay_trace(path, page=page)
    assert result["results"][0]["page_identical"] is True
    assert result["results"][0]["verdict"] == "ok"
    assert "re-ranked" in result["results"][0]["reason"]


def test_a_real_position_change_is_still_moved(tmp_path, page):
    site = tmp_path / "one.html"
    site.write_text("<html><body><button id=a>Alpha</button></body></html>")
    page.goto(site.as_uri())
    candidates, _, _ = extract(page)
    path = tmp_path / "one.trace.jsonl"
    with TraceWriter(path, flow="x") as writer:
        writer.step(
            step=1,
            intent="click alpha",
            url=page.url,
            title="t",
            candidates=candidates,
            decision={"choice": candidates[0]["cid"], "confidence": 0.99},
        )
    site.write_text(
        "<html><body><button id=z>Zeta</button><button id=a>Alpha</button></body></html>"
    )
    result = replay_trace(path, page=page)
    assert result["results"][0]["drift"]["new"] == 1
    assert result["results"][0]["verdict"] == "moved"


def test_replay_does_not_execute_by_default(trace_path, page):
    replay_trace(trace_path, page=page)
    assert page.input_value("#login-password") == ""


def test_replay_execute_re_runs_the_recorded_actions(trace_path, page, monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "hunter2")
    result = replay_trace(trace_path, page=page, execute=True, settle_ms=0)
    assert result["failed"] == []
    assert all(step.get("executed") for step in result["results"])
    assert page.input_value("#login-password") == "hunter2"


def test_replay_execute_needs_the_env_value(trace_path, page, monkeypatch):
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    result = replay_trace(trace_path, page=page, execute=True, settle_ms=0)
    assert 2 in result["failed"]
    assert "DEMO_PASSWORD" in result["results"][1]["reason"]


def test_a_none_decision_replays_as_ok_with_a_reason(tmp_path, page, app_url):
    path = tmp_path / "none.trace.jsonl"
    with TraceWriter(path, flow="x", model="m") as writer:
        writer.step(
            step=1,
            intent="Permanently delete the whole account",
            url=app_url,
            title="t",
            candidates=[],
            decision={"choice": "none", "confidence": 1.0},
        )
    result = replay_trace(path, page=page)
    assert result["counts"] == {"ok": 1}
    assert "no-match" in result["results"][0]["reason"]


def test_replay_reports_a_page_that_will_not_load(page):
    step = {
        "step": 1,
        "intent": "go somewhere",
        "url": "http://127.0.0.1:9/nope",
        "candidates": [],
        "decision": {"choice": "none", "confidence": 1.0},
    }
    result = replay_step(page, step)
    assert result["verdict"] == "error"
    assert result["reason"]


def test_action_for_replay_reads_env_and_refuses_when_missing(monkeypatch):
    monkeypatch.setenv("TOKEN", "abc")
    assert action_for_replay({"type": "fill", "value_from_env": "TOKEN"}) == {
        "type": "fill",
        "value": "abc",
    }
    assert action_for_replay({"type": "click"}) == {"type": "click"}
    monkeypatch.delenv("TOKEN")
    with pytest.raises(RuntimeError, match="TOKEN"):
        action_for_replay({"type": "fill", "value_from_env": "TOKEN"})


def test_trace_records_what_replay_needs(trace_path):
    _, steps = read_trace(trace_path)
    for step in steps:
        assert step["candidates"] and all("fp" in c for c in step["candidates"])
        assert step["decision"]["choice"] in {c["cid"] for c in step["candidates"]}
        assert step["url"].startswith("file://")


def test_relative_file_urls_resolve_against_the_trace_directory(tmp_path, page):
    from jevnav.replay import portable_url

    (tmp_path / "app.html").write_text("<html><body><button>Go</button></body></html>")
    assert portable_url("file:app.html", tmp_path) == (tmp_path / "app.html").as_uri()
    assert portable_url("https://x.test/a", tmp_path) == "https://x.test/a"
    assert portable_url("file:///tmp/abs.html", tmp_path) == "file:///tmp/abs.html"


def test_a_portable_trace_replays_without_flags(tmp_path, page):
    from jevnav.trace import TraceWriter

    (tmp_path / "app.html").write_text("<html><body><button>Go</button></body></html>")
    path = tmp_path / "demo.trace.jsonl"
    with TraceWriter(path, flow="demo", model="m") as writer:
        writer.step(
            step=1,
            intent="go",
            url="file:app.html",
            title="t",
            candidates=[
                {
                    "cid": "c1",
                    "role": "button",
                    "name": "Go",
                    "fp": "button|go",
                    "tag": "button",
                    "type": None,
                    "href": None,
                    "placeholder": None,
                    "scope": None,
                    "value": None,
                    "disabled": False,
                    "in_viewport": True,
                }
            ],
            decision={"choice": "c1", "confidence": 0.99},
        )
    result = replay_trace(path, page=page)
    assert result["failed"] == []
    assert result["results"][0]["page_identical"] is True


def test_replay_verifies_a_step_level_verify_block(tmp_path, page):
    """MCP sessions record the success selector on the step, not in the header."""
    from helpers import fixture_url

    from jevnav.agent import run_goal
    from jevnav.gates import default_gates

    url = fixture_url("loop-app.html")
    path = tmp_path / "mcp.trace.jsonl"
    with TraceWriter(path, flow="mcp-session") as writer:
        run_goal(
            "sign in",
            page=page,
            client=FakeJev(
                script=[
                    {"action": "fill", "target": "Email", "value_key": "email"},
                    {"action": "click", "target": "Sign in"},
                ]
            ).client(),
            gates=default_gates(),
            writer=writer,
            context={"email": "demo@example.com"},
            start=url,
            success="#signed-in-as",
        )
    result = replay_trace(path, page=page, execute=True, settle_ms=0)
    assert result["success"] == {"selector": "#signed-in-as", "verified": True}
    assert result["failed"] == []


def test_replay_verifies_a_recorded_success_selector(tmp_path, page):
    from helpers import FakeJev, fixture_url

    from jevnav.agent import run_goal
    from jevnav.gates import default_gates

    url = fixture_url("loop-app.html")
    path = tmp_path / "goal.trace.jsonl"
    script = [
        {"action": "fill", "target": "Email", "value_key": "email"},
        {"action": "click", "target": "Sign in"},
    ]
    with TraceWriter(path, flow="goal", goal="sign in", success="#signed-in-as") as writer:
        run_goal(
            "sign in",
            page=page,
            client=FakeJev(script=script).client(),
            gates=default_gates(),
            writer=writer,
            context={"email": "demo@example.com"},
            start=url,
            success="#signed-in-as",
        )
    without = replay_trace(path, page=page)
    assert without["success"]["verified"] is None
    assert without["failed"] == []
    with_execute = replay_trace(path, page=page, execute=True, settle_ms=0)
    assert with_execute["success"] == {"selector": "#signed-in-as", "verified": True}
    assert with_execute["failed"] == []


def test_normalize_relaxes_counter_churn_but_stays_opt_in(tmp_path, page):
    """'Cart (3)' -> 'Cart (4)' after a deploy is churn; only --normalize may ignore it."""
    site = tmp_path / "cart.html"
    site.write_text("<html><body><button id=b>Cart (3)</button></body></html>")
    trace_path = tmp_path / "churn.trace.jsonl"
    page.goto(site.as_uri())
    candidates, _, _ = extract(page)
    with TraceWriter(trace_path, flow="x") as writer:
        writer.step(
            step=1,
            intent="open the cart",
            url=page.url,
            title="t",
            candidates=candidates,
            decision={"choice": candidates[0]["cid"], "confidence": 0.99},
        )
    site.write_text("<html><body><button id=b>Cart (4)</button></body></html>")  # the "deploy"
    strict = replay_trace(trace_path, page=page)
    assert strict["failed"] == [1]  # without the flag, churn is still a change
    relaxed = replay_trace(trace_path, page=page, normalize=[r"\(\d+\)"])
    assert relaxed["failed"] == []
    assert relaxed["results"][0]["verdict"] == "ok"
    assert relaxed["normalize"] == [r"\(\d+\)"]
