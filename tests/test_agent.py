import pytest
from helpers import FakeJev, fixture_url

from jevnav.agent import build_questions, build_state, context_value, run_goal, summarize_goal
from jevnav.gates import default_gates
from jevnav.trace import NullWriter, TraceWriter, read_trace

SIGN_IN = [
    {"action": "fill", "target": "Email", "value_key": "email"},
    {"action": "fill", "target": "Password", "value_key": "password"},
    {"action": "click", "target": "Sign in"},
]
CONTEXT = {"email": "demo@example.com", "password": "hunter2"}


@pytest.fixture
def loop_url() -> str:
    return fixture_url("loop-app.html")


def run(
    page, tmp_path, script, url, *, goal="Sign in with the demo account", context=None, **kwargs
):
    trace_path = tmp_path / "goal.trace.jsonl"
    with TraceWriter(
        trace_path,
        flow="goal",
        goal=goal,
        context_keys=list(context if context is not None else CONTEXT),
    ) as writer:
        result = run_goal(
            goal,
            page=page,
            client=FakeJev(script=script).client(),
            gates=default_gates(),
            writer=writer,
            context=context if context is not None else CONTEXT,
            start=url,
            **kwargs,
        )
    return result, trace_path


def test_the_loop_fills_clicks_and_finishes_verified(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, trace_path = run(page, tmp_path, SIGN_IN, loop_url, success="#signed-in-as")
    assert result["status"] == "done"
    assert result["verified"] is True
    assert len(result["steps"]) == 4  # three actions, then the model says done
    assert page.input_value("#login-email") == "demo@example.com"
    assert page.is_visible("#signed-in-as")
    run_header, steps = read_trace(trace_path)
    assert run_header["goal"] == "Sign in with the demo account"
    assert run_header["context_keys"] == ["email", "password"]
    assert [step["decision"]["status"] for step in steps] == [
        "in_progress",
        "in_progress",
        "in_progress",
        "done",
    ]
    assert [step["gate"]["verdict"] for step in steps] == ["auto", "auto", "auto", "n/a"]
    assert steps[0]["action"] == {"type": "fill", "value": "demo@example.com"}
    assert steps[3]["result"]["executed"] is False


def test_the_loop_stops_on_a_risky_step_without_acting(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(
        page,
        tmp_path,
        [{"action": "click", "target": "Home"}],
        loop_url,
        goal="delete my account",
    )
    assert result["status"] == "review"
    assert "risky" in result["reason"]
    assert result["steps"][0]["result"]["executed"] is False
    assert result["steps"][0]["gate"]["verdict"] == "review"


def test_allow_risky_acts_but_still_records_the_review(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(
        page,
        tmp_path,
        [{"action": "click", "target": "Home"}],
        loop_url,
        goal="delete my account",
        allow_risky=True,
    )
    assert result["steps"][0]["gate"]["verdict"] == "review"
    assert result["steps"][0]["result"]["executed"] is True


def test_the_loop_stops_when_nothing_fits_the_goal(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(
        page,
        tmp_path,
        [{"action": "click", "target": "none"}],
        loop_url,
        goal="close my account permanently",
        context={},
    )
    assert result["status"] == "stuck"
    assert result["steps"][0]["result"]["executed"] is False


def test_the_loop_stops_when_the_page_stops_changing(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(
        page,
        tmp_path,
        [{"action": "click", "target": "Home"}, {"action": "click", "target": "Home"}],
        loop_url,
        goal="open the home page",
        context={},
    )
    assert result["status"] == "no_progress"
    assert len(result["steps"]) == 2


def test_max_steps_is_respected(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(page, tmp_path, SIGN_IN, loop_url, max_steps=1)
    assert result["status"] == "max_steps"
    assert len(result["steps"]) == 1


def test_the_done_step_records_the_verification_in_the_trace(tmp_path, page, loop_url):
    page.goto(loop_url)
    _, trace_path = run(page, tmp_path, SIGN_IN, loop_url, success="#signed-in-as")
    _, steps = read_trace(trace_path)
    assert steps[-1]["verify"] == {"selector": "#signed-in-as", "verified": True}

    page.goto(loop_url)
    _, failed_path = run(page, tmp_path, [], loop_url, context={}, success="#signed-in-as")
    _, failed_steps = read_trace(failed_path)
    assert failed_steps[-1]["verify"] == {"selector": "#signed-in-as", "verified": False}
    assert failed_steps[-1]["decision"]["status"] == "done"


def test_done_without_a_success_selector_is_reported_as_unverified(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(page, tmp_path, [], loop_url, context={})
    assert result["status"] == "done"
    assert result["verified"] is None
    assert "--success" in result["reason"]


def test_a_false_done_claim_is_not_a_pass(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(page, tmp_path, [], loop_url, context={}, success="#signed-in-as")
    assert result["status"] == "unverified"
    assert result["verified"] is False
    assert "not visible" in result["reason"]


def test_fill_on_a_button_is_blocked_before_it_acts(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(
        page,
        tmp_path,
        [{"action": "fill", "target": "Sign in", "value_key": "email"}],
        loop_url,
    )
    assert result["status"] == "blocked"
    assert "does not apply to role" in result["reason"]
    assert result["steps"][0]["result"]["executed"] is False


def test_a_missing_value_key_falls_back_to_the_field_name(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(
        page, tmp_path, [{"action": "fill", "target": "Email", "value_key": "none"}], loop_url
    )
    step = result["steps"][0]
    assert step["value_source"] == "name-match"
    assert page.input_value("#login-email") == "demo@example.com"


def test_a_field_with_no_context_value_is_blocked(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(
        page,
        tmp_path,
        [{"action": "fill", "target": "Email", "value_key": "none"}],
        loop_url,
        context={"phone": "123"},
    )
    assert result["status"] == "blocked"
    assert "no context value fits" in result["reason"]


def test_env_refs_are_resolved_and_recorded_by_name_only(tmp_path, page, loop_url, monkeypatch):
    monkeypatch.setenv("DEMO_PW", "s3cret")
    page.goto(loop_url)
    result, trace_path = run(
        page,
        tmp_path,
        [{"action": "fill", "target": "Password", "value_key": "password"}],
        loop_url,
        context={"password": "${DEMO_PW}"},
    )
    _, steps = read_trace(trace_path)
    assert steps[0]["action"] == {"type": "fill", "value_from_env": "DEMO_PW"}
    assert "s3cret" not in trace_path.read_text()
    assert page.input_value("#login-password") == "s3cret"


def test_a_missing_env_ref_fails_loudly(page, tmp_path, loop_url, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    page.goto(loop_url)
    with pytest.raises(KeyError, match="NOPE"):
        run(
            page,
            tmp_path,
            [{"action": "fill", "target": "Password", "value_key": "password"}],
            loop_url,
            context={"password": "${NOPE}"},
        )


def test_dry_run_decides_without_acting(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(page, tmp_path, SIGN_IN, loop_url, dry_run=True)
    assert all(step["result"]["executed"] is False for step in result["steps"])
    assert page.input_value("#login-email") == ""


def test_the_loop_records_truncation_instead_of_stopping(tmp_path, page, loop_url):
    page.goto("about:blank")
    page.set_content(
        "<html><body>"
        + "".join(f"<button>Button {i}</button>" for i in range(300))
        + '<input id="target" aria-label="Email"></body></html>'
    )
    result, trace_path = run(page, tmp_path, [], None, context={})  # no start: keep set_content
    step = result["steps"][0]
    assert step["dropped"] > 0
    assert step["gate"]["verdict"] == "n/a"  # still reached the model, and it said done
    _, steps = read_trace(trace_path)
    assert steps[0]["dropped"] == step["dropped"]


def test_the_state_carries_the_goal_page_text_and_history(page, loop_url):
    page.goto(loop_url)
    from jevnav import page as page_module

    candidates, _, _ = page_module.extract(page)
    state = build_state(
        goal="sign in",
        context_keys=["email"],
        page=page,
        candidates=candidates,
        history=["step 1: fill on 'Email' with email -> page changed"],
        step=2,
        max_steps=5,
    )
    assert "Goal: sign in" in state
    assert "Step 2 of 5" in state
    assert "Acme Console" in state
    assert "Email" in state  # visible text, so 'done' can be judged
    assert "step 1: fill on 'Email'" in state


def test_questions_offer_the_context_keys_only_when_there_is_context(page, loop_url):
    page.goto(loop_url)
    from jevnav import page as page_module

    candidates, _, _ = page_module.extract(page)
    without = build_questions("go", [], candidates)
    with_context = build_questions("go", ["email", "password"], candidates)
    assert "value_key" not in without
    assert set(with_context["value_key"]["criteria"]) == {"email", "password", "none"}
    assert with_context["status"]["criteria"]["done"]


def test_context_value_handles_refs_and_literals(monkeypatch):
    monkeypatch.setenv("X", "1")
    assert context_value("${X}") == ("1", "X")
    assert context_value("plain") == ("plain", None)


def test_summary_counts_gates_and_cost(tmp_path, page, loop_url):
    page.goto(loop_url)
    result, _ = run(page, tmp_path, SIGN_IN, loop_url, success="#signed-in-as")
    summary = summarize_goal(result)
    assert summary["status"] == "done"
    assert summary["auto"] == 3
    assert summary["stopped"] == 1
    assert summary["cost_usd"] > 0
    assert summary["latency_p50_ms"] >= 0


def test_a_null_writer_keeps_the_loop_working(page, loop_url):
    page.goto(loop_url)
    result = run_goal(
        "Sign in",
        page=page,
        client=FakeJev(script=SIGN_IN).client(),
        gates=default_gates(),
        writer=NullWriter(),
        context=CONTEXT,
        success="#signed-in-as",
    )
    assert result["status"] == "done"
    assert result["steps"][0]["kind"] == "step"
