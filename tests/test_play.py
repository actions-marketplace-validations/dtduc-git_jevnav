"""Play mode: a state probe, an action set, a fixed rate — the Doom shape."""

import json

import pytest
from helpers import FakeJev, example_url

from jevnav.play import build_question, parse_actions, play
from jevnav.trace import TraceWriter, read_trace

ACTIONS = {"left": "ArrowLeft", "right": "ArrowRight"}
STATE_JS = "window.jevnavState()"
SCORE_JS = "window.jevnavScore()"


def test_parse_actions_accepts_labels_keys_and_commas():
    assert parse_actions(["left=ArrowLeft,right=ArrowRight"]) == ACTIONS
    assert parse_actions(["Space"]) == {"Space": "Space"}
    assert parse_actions(["fire=Space", "move=ArrowUp"]) == {"fire": "Space", "move": "ArrowUp"}
    with pytest.raises(ValueError, match="no actions"):
        parse_actions(["", "  "])


def test_the_question_offers_the_action_set():
    question = build_question("catch greens", "score 0", ACTIONS)
    assert set(question["criteria"]) == {"left", "right"}
    assert "catch greens" in question["instructions"]
    assert "score 0" in question["instructions"]


def test_play_drives_the_game_with_jev_decisions(tmp_path, page):
    url = example_url("game/index.html") + "?seed=7"
    page.goto(url)
    page.wait_for_function("() => !!window.jevnavState")
    trace_path = tmp_path / "play.trace.jsonl"
    with TraceWriter(trace_path, flow="play", goal="catch greens") as writer:
        summary = play(
            goal="catch the green blocks",
            page=page,
            client=FakeJev(script=["left", "left", "right", "right"]).client(),
            writer=writer,
            state_js=STATE_JS,
            actions=ACTIONS,
            rate_hz=5,
            seconds=10,
            max_steps=4,
            score_js=SCORE_JS,
        )
    assert summary["steps"] == 4
    assert summary["actions"] == {"left": 2, "right": 2}
    assert summary["errors"] == 0
    assert summary["score"] is not None
    _, steps = read_trace(trace_path)
    assert len(steps) == 4
    assert steps[0]["gate"]["verdict"] == "n/a"
    assert "score" in steps[0]["state"]
    assert steps[0]["decision"]["model"] == "jev-fake-1"
    assert steps[0]["state_hash"].startswith("sha256:")


def test_play_moves_the_catcher(page):
    url = example_url("game/index.html") + "?seed=7"
    page.goto(url)
    page.wait_for_function("() => !!window.jevnavState")
    before = page.evaluate("window.jevnavItems()")["catcher"]
    from jevnav.trace import NullWriter

    play(
        goal="move left",
        page=page,
        client=FakeJev(script=["left", "left", "left"]).client(),
        writer=NullWriter(),
        state_js=STATE_JS,
        actions=ACTIONS,
        rate_hz=10,
        seconds=5,
        max_steps=3,
    )
    assert page.evaluate("window.jevnavItems()")["catcher"] < before


def test_play_with_the_random_policy_calls_no_model(tmp_path, page):
    url = example_url("game/index.html") + "?seed=3"
    page.goto(url)
    page.wait_for_function("() => !!window.jevnavState")
    fake = FakeJev(script=[])
    with TraceWriter(tmp_path / "r.trace.jsonl", flow="play") as writer:
        summary = play(
            goal="any",
            page=page,
            client=fake.client(),
            writer=writer,
            state_js=STATE_JS,
            actions=ACTIONS,
            rate_hz=10,
            seconds=1,
            policy="random",
            seed=1,
        )
    assert fake.calls == []
    assert summary["steps"] >= 5
    assert summary["cost_usd"] == 0
    assert summary["policy"] == "random"


def test_play_stops_on_a_failed_decision(tmp_path, page):
    url = example_url("game/index.html") + "?seed=7"
    page.goto(url)
    page.wait_for_function("() => !!window.jevnavState")

    class BrokenJev:
        def system_one(self, *args, **kwargs):
            raise RuntimeError("endpoint down")

    with TraceWriter(tmp_path / "e.trace.jsonl", flow="play") as writer:
        summary = play(
            goal="catch greens",
            page=page,
            client=BrokenJev(),
            writer=writer,
            state_js=STATE_JS,
            actions=ACTIONS,
            rate_hz=5,
            seconds=5,
        )
    assert summary["steps"] == 1
    assert summary["errors"] == 1


def test_play_honours_the_rate(page):
    url = example_url("game/index.html") + "?seed=7"
    page.goto(url)
    page.wait_for_function("() => !!window.jevnavState")
    from jevnav.trace import NullWriter

    summary = play(
        goal="any",
        page=page,
        client=FakeJev(script=[]).client(),
        writer=NullWriter(),
        state_js=STATE_JS,
        actions=ACTIONS,
        rate_hz=2,
        seconds=1.2,
        policy="random",
    )
    assert 2 <= summary["steps"] <= 4  # 1.2s at 2/s, plus jitter
    assert summary["requested_rate_hz"] == 2


def test_the_cli_plays_and_reports(tmp_path, capsys, monkeypatch, page):
    from contextlib import contextmanager

    from jevnav import cli

    monkeypatch.setattr(cli, "_client", lambda: FakeJev(script=["left"] * 20).client())
    monkeypatch.setattr(cli, "_api_key", lambda: "test-key")

    @contextmanager
    def session(headed=False, user_data_dir=None, cdp=None, dialog_policy="dismiss"):
        yield page

    monkeypatch.setattr(cli, "_session", session)
    code = cli.main(
        [
            "play",
            "--goal",
            "catch the green blocks, dodge the red ones",
            "--url",
            "examples/game/index.html?seed=7",
            "--state-js",
            "examples/game/state.js",
            "--actions",
            "left=ArrowLeft",
            "--actions",
            "right=ArrowRight",
            "--rate",
            "5",
            "--seconds",
            "1",
            "--score-js",
            "window.jevnavScore()",
            "--ready-js",
            "() => !!window.jevnavState",
            "--trace",
            str(tmp_path / "play.trace.jsonl"),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["policy"] == "jev"
    assert payload["steps"] >= 1
    assert payload["latency_p50_ms"] is not None
    assert payload["actions"]["left"] >= 1
