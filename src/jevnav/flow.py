"""flow.yaml: the scripted path through a site, decided by Jev one step at a time.

    id: acme-login
    start: https://app.example.com/login
    steps:
      - intent: "Sign in to the existing account"
        action: click
      - intent: "Type the password"
        action: fill
        value: "${ACME_PASSWORD}"     # read from the environment, never traced

Actions: click, fill, select, check, hover, press, none. A step may also
``goto`` a URL. ``expect`` (a selector, or ``none``) is optional ground truth
used to score the run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from . import page as page_module
from .decide import ask, failed_decision
from .gates import AUTO, verdict
from .page import by_cid
from .trace import TraceWriter, dom_hash

ACTION_TYPES = {"click", "fill", "select", "check", "hover", "press", "none"}
ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class FlowError(RuntimeError):
    """The flow file is wrong, or the page no longer matches its ground truth."""


def resolve_url(value: str, base_dir: Path) -> str:
    """URLs pass through; a relative path becomes a file:// URL next to the flow.

    A query string survives (``game.html?seed=7``), which is how a local fixture
    gets configured without a server.
    """
    if "://" in value:
        return value
    path_part, _, fragment = value.partition("#")
    path_part, _, query = path_part.partition("?")
    url = (base_dir / path_part).resolve().as_uri()
    if query:
        url += f"?{query}"
    if fragment:
        url += f"#{fragment}"
    return url


def load_flow(path: str | Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        raise FlowError(f"{path}: flow must be a mapping")
    unknown = set(raw) - {"id", "start", "settle_ms", "steps", "notes", "_path"}
    if unknown:
        raise FlowError(f"{path}: unknown keys {sorted(unknown)}")
    if not raw.get("id"):
        raise FlowError(f"{path}: missing id")
    if not raw.get("start"):
        raise FlowError(f"{path}: missing start URL")
    base = Path(path).resolve().parent
    raw["start"] = resolve_url(str(raw["start"]), base)
    raw["_path"] = str(Path(path))
    for step in raw.get("steps") or []:
        if step.get("goto"):
            step["goto"] = resolve_url(str(step["goto"]), base)
    steps = raw.get("steps") or []
    if not steps:
        raise FlowError(f"{path}: no steps")
    for index, step in enumerate(steps, 1):
        where = f"{path}: step {index}"
        unknown = set(step) - {"intent", "action", "value", "key", "expect", "goto", "settle_ms"}
        if unknown:
            raise FlowError(f"{where}: unknown keys {sorted(unknown)}")
        if not step.get("intent"):
            raise FlowError(f"{where}: missing intent")
        action = step.get("action", "click")
        if action not in ACTION_TYPES:
            raise FlowError(
                f"{where}: unknown action {action!r} (expected one of {sorted(ACTION_TYPES)})"
            )
        if action in {"fill", "select"} and not step.get("value"):
            raise FlowError(f"{where}: action {action!r} needs a value")
        if action == "press" and not step.get("key"):
            raise FlowError(f"{where}: action 'press' needs a key")
    return raw


def recorded_url(page_url: str, base_dir: Path) -> str:
    """Store a file:// URL under the flow's directory as ``file:<relative>``.

    That keeps a committed trace portable: replay resolves it against the
    trace's own directory, so an example works on any machine.
    """
    if not page_url.startswith("file://"):
        return page_url
    from urllib.parse import unquote, urlparse

    path = Path(unquote(urlparse(page_url).path))
    try:
        relative = path.relative_to(base_dir)
    except ValueError:
        return page_url
    return "file:" + relative.as_posix()


def action_public(step: dict[str, Any]) -> dict[str, Any]:
    """The action as written to the trace: env values are referenced, never copied."""
    action: dict[str, Any] = {"type": step.get("action", "click")}
    for key in ("value", "key"):
        if key not in step:
            continue
        match = ENV_PATTERN.match(str(step[key]))
        if match:
            action[f"{key}_from_env"] = match.group(1)
        else:
            action[key] = step[key]
    return action


def action_runtime(step: dict[str, Any]) -> dict[str, Any]:
    action: dict[str, Any] = {"type": step.get("action", "click")}
    if "value" in step:
        match = ENV_PATTERN.match(str(step["value"]))
        if match:
            name = match.group(1)
            if name not in os.environ:
                raise FlowError(
                    f"step intent {step['intent']!r}: environment variable {name} is not set"
                )
            action["value"] = os.environ[name]
        else:
            action["value"] = str(step["value"])
    if "key" in step:
        action["key"] = step["key"]
    return action


def expected_cid(page: Any, expect: str | None) -> str | None:
    if not expect:
        return None
    if expect == "none":
        return "none"
    locator = page.locator(expect)
    if locator.count() == 0:
        raise FlowError(f"expect selector {expect!r} matched no element on {page.url}")
    js = (
        "el => { const c = el.closest('[data-jevcid]');"
        " return c ? c.getAttribute('data-jevcid') : null; }"
    )
    return locator.first.evaluate(js)


def run_flow(
    flow: dict[str, Any],
    *,
    page: Any,
    client: Any,
    gates: dict[str, Any],
    writer: TraceWriter,
    model: str,
    max_candidates: int | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Walk the flow: extract, ask, gate, act, record. Returns the step records."""
    default_settle = int(flow.get("settle_ms", 300))
    base_dir = Path(flow.get("_path", ".")).resolve().parent
    page.goto(flow["start"], wait_until="domcontentloaded")
    records: list[dict[str, Any]] = []
    for index, step in enumerate(flow["steps"], 1):
        if step.get("goto"):
            page.goto(step["goto"], wait_until="domcontentloaded")
        settle = int(step.get("settle_ms", default_settle))
        if settle:
            page.wait_for_timeout(settle)
        candidates, total, dropped = page_module.extract(page, max_candidates)
        expect = expected_cid(page, step.get("expect"))
        decision = failed_decision(RuntimeError("not attempted"))
        record: dict[str, Any] = {
            "step": index,
            "intent": step["intent"],
            "action": action_public(step),
            "url": recorded_url(page.url, base_dir),
            "title": page.title(),
            "total_on_page": total,
            "dropped": dropped,
            "dom_hash": dom_hash(candidates),
            "candidates": candidates,
            "expected_cid": expect,
        }
        if candidates:
            try:
                decision = ask(
                    client,
                    url=page.url,
                    title=record["title"],
                    intent=step["intent"],
                    candidates=candidates,
                    model=model,
                    total_on_page=total,
                    dropped=dropped,
                )
            except Exception as error:  # recorded, then surfaced by the gate
                decision = failed_decision(error)
        chosen = by_cid(candidates, decision.get("choice") or "")
        gate, reason = verdict(
            decision, intent=step["intent"], candidate=chosen, dropped=dropped, gates=gates
        )
        record["decision"] = decision
        record["gate"] = {"verdict": gate, "reason": reason}
        record["locator"] = None
        if chosen is not None:
            selector, unique = page_module.locator_for(page, chosen)
            record["locator"] = {"selector": selector, "unique": unique}
        record["result"] = {
            "correct": None if expect is None else decision.get("choice") == expect,
            "executed": False,
            "error": None,
        }
        if gate == AUTO and not dry_run:
            try:
                page_module.execute(
                    page, chosen, action_runtime(step), candidates=candidates, settle_ms=settle
                )
                record["result"]["executed"] = True
            except Exception as error:
                record["result"]["error"] = f"{type(error).__name__}: {error}"
        records.append(writer.step(**record))
    return records


def summarize_run(records: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in records if r["result"]["correct"] is not None]
    gates = [r["gate"]["verdict"] for r in records]
    latencies = [
        r["decision"]["latency_ms"] for r in records if r["decision"].get("latency_ms") is not None
    ]
    return {
        "steps": len(records),
        "auto": gates.count("auto"),
        "review": gates.count("review"),
        "blocked": gates.count("blocked"),
        "correct": sum(1 for r in scored if r["result"]["correct"]),
        "scored": len(scored),
        "errors": sum(1 for r in records if r["result"]["error"] or r["decision"].get("error")),
        "cost_usd": round(sum(r["decision"].get("cost_usd") or 0 for r in records), 6),
        "latency_p50_ms": sorted(latencies)[len(latencies) // 2] if latencies else None,
        "latency_p95_ms": sorted(latencies)[min(len(latencies) - 1, int(0.95 * len(latencies)))]
        if latencies
        else None,
    }
