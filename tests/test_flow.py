import pytest
from helpers import FakeJev

from jevnav.flow import FlowError, action_public, load_flow, run_flow, summarize_run
from jevnav.gates import AUTO, BLOCKED, REVIEW, default_gates
from jevnav.trace import TraceWriter, read_trace

ANSWERS = {
    "Sign in to the existing account": "sign in",
    "Type the password": "password",
    "Submit the login form": "sign in",
    "Delete the task about shipping release notes": "delete ship release notes",
    "Permanently delete the whole account": "none",
}


def write_flow(tmp_path, body: str) -> str:
    path = tmp_path / "flow.yaml"
    path.write_text(body)
    return str(path)


def demo_flow(tmp_path, app_url, extra: str = "") -> str:
    return write_flow(
        tmp_path,
        f"""id: demo
start: {app_url}
{extra}steps:
  - intent: "Sign in to the existing account"
    action: click
    expect: "#login-submit"
  - intent: "Type the password"
    action: fill
    value: "${{DEMO_PASSWORD}}"
  - intent: "Submit the login form"
    action: click
""",
    )


def test_flow_requires_intent(tmp_path):
    path = write_flow(tmp_path, "id: x\nstart: https://x.test\nsteps:\n  - action: click\n")
    with pytest.raises(FlowError, match="missing intent"):
        load_flow(path)


def test_flow_rejects_unknown_action(tmp_path):
    path = write_flow(
        tmp_path, "id: x\nstart: https://x.test\nsteps:\n  - intent: go\n    action: teleport\n"
    )
    with pytest.raises(FlowError, match="unknown action"):
        load_flow(path)


def test_fill_needs_a_value(tmp_path):
    path = write_flow(
        tmp_path, "id: x\nstart: https://x.test\nsteps:\n  - intent: type\n    action: fill\n"
    )
    with pytest.raises(FlowError, match="needs a value"):
        load_flow(path)


def test_relative_start_becomes_a_file_url(tmp_path, app_url):
    path = write_flow(tmp_path, "id: x\nstart: app.html\nsteps:\n  - intent: go\n")
    (tmp_path / "app.html").write_text("<html><body><button>Go</button></body></html>")
    assert load_flow(path)["start"] == (tmp_path / "app.html").resolve().as_uri()


def test_a_local_url_keeps_its_query_string(tmp_path):
    from jevnav.flow import resolve_url

    (tmp_path / "game.html").write_text("<html></html>")
    url = resolve_url("game.html?seed=11", tmp_path)
    assert url.endswith("game.html?seed=11")
    assert "%3F" not in url


def test_action_public_references_env_instead_of_copying_it():
    assert action_public({"action": "fill", "value": "${PW}"}) == {
        "type": "fill",
        "value_from_env": "PW",
    }
    assert action_public({"action": "fill", "value": "plain"}) == {"type": "fill", "value": "plain"}


def test_run_flow_decides_acts_and_records(tmp_path, page, app_url, monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "hunter2")
    fake = FakeJev(ANSWERS)
    flow = load_flow(demo_flow(tmp_path, app_url))
    trace_path = tmp_path / "run.trace.jsonl"
    with TraceWriter(trace_path, flow="demo", model="jev-latest") as writer:
        records = run_flow(
            flow,
            page=page,
            client=fake.client(),
            gates=default_gates(),
            writer=writer,
            model="jev-latest",
        )
    summary = summarize_run(records)
    assert summary["steps"] == 3
    assert summary["auto"] == 3
    assert summary["correct"] == 1 and summary["scored"] == 1
    assert page.input_value("#login-password") == "hunter2"
    assert records[0]["result"]["executed"] is True
    assert records[0]["locator"] == {"selector": 'role=button[name="Sign in"]', "unique": True}
    run, steps = read_trace(trace_path)
    assert run["flow"] == "demo"
    assert len(steps) == 3
    assert steps[1]["action"] == {"type": "fill", "value_from_env": "DEMO_PASSWORD"}
    assert steps[1]["decision"]["model"] == "jev-fake-1"
    assert steps[1]["decision"]["cost_usd"] > 0


def test_run_flow_never_acts_on_a_risky_decision(tmp_path, page, app_url):
    fake = FakeJev(ANSWERS)
    flow = load_flow(
        write_flow(
            tmp_path,
            f"""id: risky
start: {app_url}
steps:
  - intent: "Delete the task about shipping release notes"
    action: click
""",
        )
    )
    with TraceWriter(tmp_path / "r.trace.jsonl", flow="risky", model="m") as writer:
        records = run_flow(
            flow, page=page, client=fake.client(), gates=default_gates(), writer=writer, model="m"
        )
    assert records[0]["gate"]["verdict"] == REVIEW
    assert records[0]["result"]["executed"] is False
    assert page.locator("li", has_text="Ship release notes").count() == 1


def test_run_flow_blocks_when_no_element_matches(tmp_path, page, app_url):
    fake = FakeJev(ANSWERS)
    flow = load_flow(
        write_flow(
            tmp_path,
            f"""id: blocked
start: {app_url}
steps:
  - intent: "Permanently delete the whole account and all of its data"
    action: click
    expect: none
""",
        )
    )
    with TraceWriter(tmp_path / "b.trace.jsonl", flow="blocked", model="m") as writer:
        records = run_flow(
            flow, page=page, client=fake.client(), gates=default_gates(), writer=writer, model="m"
        )
    assert records[0]["gate"]["verdict"] == BLOCKED
    assert records[0]["result"]["correct"] is True
    assert records[0]["result"]["executed"] is False


def test_run_flow_surfaces_a_missing_env_var(tmp_path, page, app_url, monkeypatch):
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    flow = load_flow(demo_flow(tmp_path, app_url))
    with TraceWriter(tmp_path / "e.trace.jsonl", flow="demo", model="m") as writer:
        records = run_flow(
            flow,
            page=page,
            client=FakeJev(ANSWERS).client(),
            gates=default_gates(),
            writer=writer,
            model="m",
        )
    assert records[1]["result"]["executed"] is False
    assert "DEMO_PASSWORD" in records[1]["result"]["error"]


def test_run_flow_fails_loudly_on_a_bad_expect_selector(tmp_path, page, app_url):
    flow = load_flow(
        write_flow(
            tmp_path,
            f"""id: bad
start: {app_url}
steps:
  - intent: "Sign in to the existing account"
    expect: "#does-not-exist"
""",
        )
    )
    with TraceWriter(tmp_path / "x.trace.jsonl", flow="bad", model="m") as writer:
        with pytest.raises(FlowError, match="matched no element"):
            run_flow(
                flow,
                page=page,
                client=FakeJev(ANSWERS).client(),
                gates=default_gates(),
                writer=writer,
                model="m",
            )


def test_gate_verdicts_are_auto_for_a_clean_step(tmp_path, page, app_url):
    flow = load_flow(demo_flow(tmp_path, app_url))
    with TraceWriter(tmp_path / "a.trace.jsonl", flow="demo", model="m") as writer:
        records = run_flow(
            flow,
            page=page,
            client=FakeJev(ANSWERS).client(),
            gates=default_gates(),
            writer=writer,
            model="m",
        )
    assert all(r["gate"]["verdict"] == AUTO for r in records)


def test_trace_stores_a_portable_url_for_a_fixture_next_to_the_flow(tmp_path, page):
    (tmp_path / "app.html").write_text("<html><body><button>Sign in</button></body></html>")
    path = write_flow(
        tmp_path,
        """id: portable
start: app.html
steps:
  - intent: "Sign in"
    action: click
""",
    )
    flow = load_flow(path)
    with TraceWriter(tmp_path / "p.trace.jsonl", flow="portable", model="m") as writer:
        run_flow(
            flow,
            page=page,
            client=FakeJev({"Sign in": "sign in"}).client(),
            gates=default_gates(),
            writer=writer,
            model="m",
        )
    _, steps = read_trace(tmp_path / "p.trace.jsonl")
    assert steps[0]["url"] == "file:app.html"
