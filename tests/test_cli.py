import json
from contextlib import contextmanager

import pytest
from helpers import FakeJev

from jevnav import cli

ANSWERS = {
    "Sign in to the existing account": "sign in",
    "Type the password": "password",
    "Submit the login form": "sign in",
}

FLOW = """id: demo
start: {app}
steps:
  - intent: "Sign in to the existing account"
    action: click
    expect: "#login-submit"
  - intent: "Type the password"
    action: fill
    value: "plain-text-password"
  - intent: "Submit the login form"
    action: click
"""


@pytest.fixture
def flow_file(tmp_path, app_url):
    path = tmp_path / "flow.yaml"
    path.write_text(FLOW.format(app=app_url))
    return str(path)


@pytest.fixture(autouse=True)
def offline_jev(monkeypatch, page):
    monkeypatch.setattr(cli, "_client", lambda: FakeJev(ANSWERS).client())
    monkeypatch.setattr(cli, "_api_key", lambda: "test-key")

    @contextmanager
    def session(headed=False):
        yield page

    monkeypatch.setattr(cli, "_session", session)


def test_run_writes_a_trace_and_a_report(flow_file, tmp_path, capsys):
    trace = tmp_path / "run.trace.jsonl"
    report = tmp_path / "run.md"
    code = cli.main(["run", flow_file, "--trace", str(trace), "--report", str(report)])
    assert code == 0
    assert trace.exists() and report.exists()
    out = capsys.readouterr().out
    assert "jevnav run — demo" in out
    assert "auto **3**" in out
    assert "accuracy where ground truth was given: 1/1" in out


def test_run_json_output_is_machine_readable(flow_file, tmp_path, capsys):
    trace = tmp_path / "run.trace.jsonl"
    assert cli.main(["run", flow_file, "--trace", str(trace), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["steps"] == 3
    assert payload["steps"][0]["gate"]["verdict"] == "auto"


def test_run_dry_run_decides_without_acting(flow_file, tmp_path, capsys):
    trace = tmp_path / "dry.trace.jsonl"
    assert cli.main(["run", flow_file, "--trace", str(trace), "--dry-run"]) == 0
    _, steps = __import__("jevnav.trace", fromlist=["read_trace"]).read_trace(trace)
    assert all(step["result"]["executed"] is False for step in steps)


def test_replay_of_a_fresh_trace_passes(flow_file, tmp_path, capsys):
    trace = tmp_path / "run.trace.jsonl"
    cli.main(["run", flow_file, "--trace", str(trace)])
    capsys.readouterr()
    assert cli.main(["replay", str(trace)]) == 0
    assert "ok **3**" in capsys.readouterr().out


def test_replay_of_a_mutated_page_fails(flow_file, tmp_path, mutated_url, capsys):
    trace = tmp_path / "run.trace.jsonl"
    cli.main(["run", flow_file, "--trace", str(trace)])
    capsys.readouterr()
    assert cli.main(["replay", str(trace), "--swap", mutated_url]) == 1
    out = capsys.readouterr().out
    assert "changed **2**" in out
    assert "| 1 | Sign in to the existing account | changed |" in out


def test_replay_report_file(flow_file, tmp_path):
    trace = tmp_path / "run.trace.jsonl"
    report = tmp_path / "replay.md"
    cli.main(["run", flow_file, "--trace", str(trace)])
    assert cli.main(["replay", str(trace), "--report", str(report)]) == 0
    assert "jevnav replay" in report.read_text()


def test_run_without_an_api_key_is_a_usage_error(monkeypatch, flow_file, tmp_path, capsys):
    monkeypatch.setattr(cli, "_api_key", lambda: None)
    code = cli.main(["run", flow_file, "--trace", str(tmp_path / "x.jsonl")])
    assert code == 2
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err


def test_missing_flow_file_is_a_usage_error(tmp_path, capsys):
    code = cli.main(["run", str(tmp_path / "nope.yaml")])
    assert code == 2
    assert "missing file" in capsys.readouterr().err


def test_bad_flow_is_a_usage_error(tmp_path, capsys):
    path = tmp_path / "flow.yaml"
    path.write_text("id: x\nstart: https://x.test\nsteps:\n  - intent: go\n    action: teleport\n")
    code = cli.main(["run", str(path)])
    assert code == 2
    assert "unknown action" in capsys.readouterr().err


def test_replay_does_not_need_an_api_key(flow_file, tmp_path, monkeypatch):
    trace = tmp_path / "run.trace.jsonl"
    cli.main(["run", flow_file, "--trace", str(trace)])
    monkeypatch.setattr(cli, "_api_key", lambda: None)
    assert cli.main(["replay", str(trace)]) == 0
