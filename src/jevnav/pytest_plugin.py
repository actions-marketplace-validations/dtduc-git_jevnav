"""A pytest fixture: Jev-resolved actions inside an ordinary Playwright test.

    def test_login(jev):
        jev.goto("https://app.example.com/login")
        jev.fill("the email field on the login form", "demo@example.com")
        jev.fill("the password field", "${DEMO_PASSWORD}")
        jev.click("sign in to the existing account")
        jev.expect("#dashboard")

Every action is a Jev decision, gated the same way `jevnav go` gates its own,
and the whole test writes one trace — commit it and `jevnav replay --execute`
re-runs the test in CI without a model call:

    DEMO_PASSWORD=... pytest --jev-trace-dir=traces

A ``${VAR}`` value is read from the environment and the trace records only the
variable name, so a secret never reaches the committed file. Trace files are
named after the test (`traces/test_login.trace.jsonl`).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

from .agent import context_value
from .browser import browser_session
from .gates import AUTO, load_gates, verdict
from .page import by_cid, execute, extract, locator_for
from .trace import TraceWriter

TRACE_DIR_OPTION = "--jev-trace-dir"


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup("jevnav")
    group.addoption(
        TRACE_DIR_OPTION,
        action="store",
        default=os.environ.get("JEVNAV_TRACE_DIR", "jevnav-traces"),
        help="where the jev fixture writes replayable traces (default: ./jevnav-traces)",
    )
    group.addoption(
        "--jev-gates",
        action="store",
        default=None,
        help="gates.yaml for the jev fixture (default: built-in defaults)",
    )
    group.addoption(
        "--jev-headed",
        action="store_true",
        help="show the browser in jev fixtures",
    )


class JevNavigator:
    """Playwright with an intent: Jev picks the element, the gate decides, the trace records."""

    def __init__(
        self,
        page: Any,
        client: Any,
        writer: TraceWriter,
        *,
        gates: dict[str, Any] | None = None,
        model: str = "jev-latest",
    ) -> None:
        self.page = page
        self.client = client
        self.writer = writer
        self.gates = gates or load_gates(None)
        self.model = model
        self.step = 0

    # ---- actions ---------------------------------------------------------
    def goto(self, url: str) -> Any:
        self.page.goto(url, wait_until="domcontentloaded")
        return self.page

    def click(
        self, intent: str, *, action: str = "click", value: str | None = None
    ) -> dict[str, Any]:
        return self._act(intent, action, value)

    def fill(self, intent: str, value: str) -> dict[str, Any]:
        return self._act(intent, "fill", value)

    def select(self, intent: str, value: str) -> dict[str, Any]:
        return self._act(intent, "select", value)

    def check(self, intent: str) -> dict[str, Any]:
        return self._act(intent, "check", None)

    def press(self, intent: str, key: str = "Enter") -> dict[str, Any]:
        return self._act(intent, "press", None, key=key)

    def expect(self, selector: str, *, timeout_ms: int = 5_000) -> None:
        """Assert the outcome the way a test should: against the page, not the model."""
        try:
            self.page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
        except Exception as error:
            pytest.fail(f"expected {selector!r} to be visible: {error}")

    def trace_path(self) -> Path:
        return self.writer.path

    # ---- the decision, gated and recorded --------------------------------
    def _act(
        self, intent: str, action: str, value: str | None, *, key: str | None = None
    ) -> dict[str, Any]:
        from .decide import ask

        self.step += 1
        candidates, total, dropped = extract(self.page)
        decision = ask(
            self.client,
            url=self.page.url,
            title=self.page.title(),
            intent=intent,
            candidates=candidates,
            model=self.model,
            total_on_page=total,
            dropped=dropped,
        )
        chosen = by_cid(candidates, decision.get("choice") or "")
        gate, reason = verdict(
            decision, intent=intent, candidate=chosen, dropped=dropped, gates=self.gates
        )
        payload: dict[str, Any] = {"type": action}
        if value is not None:
            value, env_name = context_value(value)
            payload["value"] = value
            action_record: dict[str, Any] = (
                {"type": action, "value_from_env": env_name}
                if env_name
                else {"type": action, "value": value}
            )
        else:
            action_record = {"type": action}
        if key is not None:
            payload["key"] = key
            action_record["key"] = key
        record: dict[str, Any] = {
            "step": self.step,
            "intent": intent,
            "action": action_record,
            "url": self.page.url,
            "title": self.page.title(),
            "total_on_page": total,
            "dropped": dropped,
            "candidates": candidates,
            "expected_cid": None,
            "decision": decision,
            "gate": {"verdict": gate, "reason": reason},
            "locator": None,
            "result": {"correct": None, "executed": False, "error": None},
        }
        if chosen is not None:
            selector, unique = locator_for(self.page, chosen)
            record["locator"] = {"selector": selector, "unique": unique}
        if gate != AUTO:
            record["gate"]["reason"] = reason or "the gate did not allow the action"
            self.writer.step(**record)
            pytest.fail(
                f"gate verdict {gate!r} for {intent!r}: {record['gate']['reason']}\n"
                f"(the decision is in {self.writer.path})"
            )
        try:
            execute(self.page, chosen, payload)
            record["result"]["executed"] = True
        except Exception as error:
            record["result"]["error"] = f"{type(error).__name__}: {error}"
            self.writer.step(**record)
            raise
        self.writer.step(**record)
        return {"target": decision.get("chosen_name"), "confidence": decision.get("confidence")}


@pytest.fixture
def jev(request: Any):
    """A page where actions are intents, plus a trace that replays in CI."""
    client = _client()
    trace_dir = Path(request.config.getoption(TRACE_DIR_OPTION))
    trace_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)
    trace_path = trace_dir / f"{name}.trace.jsonl"
    headed = bool(request.config.getoption("--jev-headed"))
    gates = load_gates(request.config.getoption("--jev-gates"))

    try:
        with (
            browser_session(headed=headed) as page,
            TraceWriter(trace_path, flow=f"pytest:{name}", model="jev-latest") as writer,
        ):
            yield JevNavigator(page, client, writer, gates=gates)
    finally:
        client.close()


def _client() -> Any:
    from .cli import _client as cli_client

    return cli_client()


__all__ = ["JevNavigator", "jev", "pytest_addoption"]
