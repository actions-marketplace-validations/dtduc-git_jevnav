"""jevnav as an MCP server: the agent asks for an intent, jevnav decides, gates and acts.

Every call is recorded to the same trace format as ``jevnav run``, so an MCP
session is replayable and auditable afterwards. Actions that the gate marks
``review`` are never executed — the tool returns the decision and the reason,
and the agent (or the human behind it) decides what to do.

    jevnav mcp --start https://app.example.com --trace session.trace.jsonl
"""

from __future__ import annotations

import json
from typing import Any

from . import __version__
from . import page as page_module
from .agent import run_goal, summarize_goal
from .decide import ask, failed_decision
from .flow import action_runtime, summarize_run
from .gates import AUTO, load_gates, verdict
from .trace import TraceWriter

ACTION_TYPES = {"click", "fill", "select", "check", "hover", "press"}


class Session:
    """One browser, one client, one trace — shared by every MCP tool call."""

    def __init__(
        self,
        *,
        start: str | None,
        trace: str | None = None,
        gates: str | None = None,
        model: str = "jev-latest",
        headed: bool = False,
        page: Any = None,
        client: Any = None,
    ) -> None:
        from .cli import _client

        self.gates = load_gates(gates)
        self.model = model
        self.client = client or _client()
        self.manager = None
        self.browser = None
        if page is None:
            from playwright.sync_api import sync_playwright

            self.manager = sync_playwright()
            self.playwright = self.manager.__enter__()
            self.browser = self.playwright.chromium.launch(headless=not headed)
            page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.page = page
        self.writer = (
            TraceWriter(trace, flow="mcp-session", model=model, tool=f"jevnav/{__version__}")
            if trace
            else None
        )
        self.steps: list[dict[str, Any]] = []
        if start:
            self.page.goto(start, wait_until="domcontentloaded")

    def close(self) -> None:
        if self.writer:
            self.writer.close()
        if self.browser:
            self.browser.close()
        if self.manager:
            self.manager.__exit__(None, None, None)
        self.client.close()

    def browse(self, intent: str, action: str, value: str | None) -> dict[str, Any]:
        if action not in ACTION_TYPES:
            return {
                "status": "error",
                "error": f"unknown action {action!r} (expected one of {sorted(ACTION_TYPES)})",
            }
        candidates, total, dropped = page_module.extract(self.page)
        step = {
            "step": len(self.steps) + 1,
            "intent": intent,
            "action": {"type": action, **({"value": value} if value is not None else {})},
            "url": self.page.url,
            "title": self.page.title(),
            "total_on_page": total,
            "dropped": dropped,
            "candidates": candidates,
            "expected_cid": None,
        }
        if not candidates:
            decision = failed_decision(RuntimeError("no visible interactive elements on the page"))
        else:
            try:
                decision = ask(
                    self.client,
                    url=self.page.url,
                    title=step["title"],
                    intent=intent,
                    candidates=candidates,
                    model=self.model,
                    total_on_page=total,
                    dropped=dropped,
                )
            except Exception as error:
                decision = failed_decision(error)
        chosen = page_module.by_cid(candidates, decision.get("choice") or "")
        gate, reason = verdict(
            decision, intent=intent, candidate=chosen, dropped=dropped, gates=self.gates
        )
        selector = None
        if chosen is not None:
            selector, unique = page_module.locator_for(self.page, chosen)
            selector = selector if unique else None
        step |= {
            "decision": decision,
            "gate": {"verdict": gate, "reason": reason},
            "locator": {"selector": selector, "unique": bool(selector)},
            "result": {"correct": None, "executed": False, "error": None},
        }
        out: dict[str, Any] = {
            "status": gate,
            "confidence": decision.get("confidence"),
            "model": decision.get("model"),
            "reason": reason,
            "target": {"name": decision.get("chosen_name"), "selector": selector},
        }
        if gate == AUTO:
            try:
                page_module.execute(
                    self.page,
                    chosen["cid"],
                    action_runtime(
                        {"action": action, **({"value": value} if value is not None else {})}
                    ),
                )
                step["result"]["executed"] = True
                out |= {"url": self.page.url, "title": self.page.title()}
            except Exception as error:
                step["result"]["error"] = f"{type(error).__name__}: {error}"
                out |= {"status": "error", "error": step["result"]["error"]}
        if self.writer:
            self.writer.step(**step)
        self.steps.append(step)
        return out

    def goal(
        self, goal: str, context: dict[str, str] | None = None, max_steps: int = 8
    ) -> dict[str, Any]:
        """Drive the browser towards a goal, one gated Jev decision per step."""
        from .trace import NullWriter

        result = run_goal(
            goal,
            page=self.page,
            client=self.client,
            gates=self.gates,
            writer=self.writer or NullWriter(),
            model=self.model,
            context=context or {},
            max_steps=max_steps,
        )
        self.steps.extend(result["steps"])
        return summarize_goal(result)

    def page_state(self) -> dict[str, Any]:
        candidates, total, dropped = page_module.extract(self.page)
        return {
            "url": self.page.url,
            "title": self.page.title(),
            "elements": total,
            "listed": len(candidates),
            "dropped": dropped,
            "candidates": [
                {"name": c["name"], "role": c["role"], "scope": c["scope"]} for c in candidates[:80]
            ],
        }

    def summary(self) -> dict[str, Any]:
        return summarize_run(self.steps) if self.steps else {"steps": 0}


def server_class() -> Any:
    """The MCP server class, whichever name this SDK version uses (FastMCP in 1.x)."""
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def serve(
    *,
    start: str | None = None,
    trace: str | None = None,
    gates: str | None = None,
    model: str = "jev-latest",
    headed: bool = False,
) -> int:
    try:
        server_class()
    except ImportError:
        print("the MCP server needs the optional dependency: pip install 'jevnav[mcp]'")
        return 2

    session = Session(start=start, trace=trace, gates=gates, model=model, headed=headed)
    mcp = server_class()("jevnav")

    @mcp.tool()
    def browse(intent: str, action: str = "click", value: str | None = None) -> str:
        """Find the element matching an intent and, if the gate allows it, act on it.

        Returns the gate verdict (auto / review / blocked), the confidence, the
        target element and its Playwright selector. Only ``auto`` decisions are
        executed; ``review`` means a human should confirm first.
        """
        return json.dumps(session.browse(intent, action, value), ensure_ascii=False)

    @mcp.tool()
    def goal(goal: str, context_json: str = "{}", max_steps: int = 8) -> str:
        """Drive the browser towards a goal: Jev decides every step, jevnav acts.

        ``context_json`` is a JSON object of values the goal may need, e.g.
        {"email": "a@b.c", "password": "${PW}"}. Returns the outcome (done,
        stuck, review, ...), the steps taken, cost and whether the outcome was
        verified. Risky steps stop the loop and come back unexecuted.
        """
        try:
            context = json.loads(context_json or "{}")
        except json.JSONDecodeError as error:
            return json.dumps({"status": "error", "error": f"context_json is not JSON: {error}"})
        return json.dumps(session.goal(goal, context, max_steps), ensure_ascii=False)

    @mcp.tool()
    def page_state() -> str:
        """Current URL, title and the interactive elements jevnav can see."""
        return json.dumps(session.page_state(), ensure_ascii=False)

    @mcp.tool()
    def summary() -> str:
        """This session so far: steps, auto/review/blocked counts, cost, latency."""
        return json.dumps(session.summary(), ensure_ascii=False)

    try:
        mcp.run()
    finally:
        session.close()
    return 0
