"""Replay: re-resolve recorded decisions against the current page, offline.

No model call, no judgement — just the question "is the element this decision
pointed at still on the page, with the same role and name?". That is what makes
a trace a regression test: a site change that breaks the target fails CI, and
everything else is reported as drift instead of noise.

Verdicts per step: ``ok`` | ``moved`` | ``changed`` | ``ambiguous`` | ``error``.
``changed``, ``ambiguous`` and ``error`` fail the replay (exit 1).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import page as page_module
from .trace import read_trace

OK = "ok"
MOVED = "moved"
CHANGED = "changed"
AMBIGUOUS = "ambiguous"
ERROR = "error"
FAILING = {CHANGED, AMBIGUOUS, ERROR}


def normalize_fp(fp: str, patterns: list[str] | None) -> str:
    """Relax a fingerprint for matching only: the trace keeps the recorded one.

    Opt-in, because stripping digits also merges elements the strict comparison
    would keep apart ("Cart (3)"/"Cart (4)" is churn; "item 3"/"item 4" is not).
    """
    for pattern in patterns or []:
        fp = re.sub(pattern, "", fp)
    return " ".join(fp.split())


def replay_step(
    page: Any,
    step: dict[str, Any],
    *,
    url: str | None = None,
    force_navigate: bool = False,
    normalize: list[str] | None = None,
) -> dict[str, Any]:
    """Re-resolve one recorded decision against the page as it is now."""
    target = url or step["url"]
    recorded = {c["cid"]: c for c in step["candidates"]}
    choice = (step.get("decision") or {}).get("choice")
    result: dict[str, Any] = {
        "step": step["step"],
        "intent": step["intent"],
        "url": target,
        "recorded_url": step["url"],
        "choice": choice,
        "verdict": OK,
        "reason": None,
        "moved": False,
        "page_identical": False,
        "navigated": False,
        "drift": {"new": 0, "missing": 0},
    }
    result["navigated"] = False
    try:
        if force_navigate or page.url != target:
            page.goto(target, wait_until="domcontentloaded")
            page.wait_for_timeout(300 if not target.startswith("file:") else 0)
            result["navigated"] = True
        current, _, _ = page_module.extract(page)
    except Exception as error:
        result["verdict"] = ERROR
        result["reason"] = f"{type(error).__name__}: {error}"
        return result

    current_fps = Counter(c["fp"] for c in current)
    recorded_fps = Counter(c["fp"] for c in step["candidates"])
    result["page_identical"] = current_fps == recorded_fps
    result["drift"] = {
        "new": sum((current_fps - recorded_fps).values()),
        "missing": sum((recorded_fps - current_fps).values()),
    }
    if choice in (None, "none"):
        result["reason"] = "recorded as no-match; nothing to resolve"
        return result
    chosen = recorded.get(choice)
    result["chosen_fp"] = chosen["fp"] if chosen else None
    if chosen is None:
        result["verdict"] = ERROR
        result["reason"] = f"trace is inconsistent: choice {choice!r} is not in the candidate list"
        return result
    matches = [
        i
        for i, c in enumerate(current)
        if normalize_fp(c["fp"], normalize) == normalize_fp(chosen["fp"], normalize)
    ]
    if not matches:
        result["verdict"] = CHANGED
        result["reason"] = (
            f"no element now has {chosen['fp']!r} (was {chosen['name']!r} / {chosen['role']})"
        )
    elif len(matches) > 1:
        result["verdict"] = AMBIGUOUS
        result["reason"] = f"{len(matches)} elements now match {chosen['fp']!r}"
    else:
        index = matches[0]
        recorded_index = next(i for i, c in enumerate(step["candidates"]) if c["cid"] == choice)
        result["resolved_index"] = index
        result["moved"] = index != recorded_index
        result["verdict"] = MOVED if result["moved"] else OK
        result["reason"] = f"resolved to {current[index]['name']!r} at position {index}"
    return result


def replay_trace(
    trace_path: str | Path,
    *,
    page: Any,
    swap: str | Path | None = None,
    execute: bool = False,
    settle_ms: int = 300,
    normalize: list[str] | None = None,
) -> dict[str, Any]:
    """Replay every step of a trace. ``swap`` points all steps at one local file."""
    run, steps = read_trace(trace_path)
    swap_url = None
    if swap:
        swap_url = str(swap) if "://" in str(swap) else Path(swap).resolve().as_uri()
    base_dir = Path(trace_path).resolve().parent
    results: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        target = swap_url or portable_url(step["url"], base_dir)
        result = replay_step(page, step, url=target, force_navigate=index == 0, normalize=normalize)
        if execute and result["verdict"] in {OK, MOVED}:
            try:
                action = action_for_replay(step["action"])
                if action["type"] != "none" and result.get("chosen_fp"):
                    page_module.execute_fp(page, result["chosen_fp"], action, settle_ms=settle_ms)
                result["executed"] = True
            except Exception as error:
                result["executed"] = False
                result["verdict"] = ERROR
                result["reason"] = f"action failed: {type(error).__name__}: {error}"
        results.append(result)
    counts = Counter(r["verdict"] for r in results)
    success: dict[str, Any] | None = None
    recorded_success = run.get("success") or next(
        (
            step["verify"]["selector"]
            for step in reversed(steps)
            if step.get("verify", {}).get("selector")
        ),
        None,
    )
    if recorded_success:
        selector = recorded_success
        if execute:
            try:
                verified = page.locator(selector).first.is_visible()
            except Exception:
                verified = False
            success = {"selector": selector, "verified": verified}
        else:
            success = {
                "selector": selector,
                "verified": None,
                "reason": "needs --execute to re-run the actions",
            }
    return {
        "trace": str(trace_path),
        "goal": run.get("goal"),
        "normalize": normalize or [],
        "swapped": str(swap) if swap else None,
        "steps": len(results),
        "counts": dict(counts),
        "failed": [r["step"] for r in results if r["verdict"] in FAILING],
        "success": success,
        "results": results,
    }


def portable_url(url: str, base_dir: Path) -> str:
    """Resolve a ``file:<relative>`` URL against the trace's directory."""
    if url.startswith("file:") and not url.startswith("file://"):
        return (base_dir / url[len("file:") :]).resolve().as_uri()
    return url


def action_for_replay(action: dict[str, Any]) -> dict[str, Any]:
    """Rebuild an executable action from the trace. Env-sourced values come from the env."""
    rebuilt = {"type": action.get("type", "click")}
    if "value" in action:
        rebuilt["value"] = action["value"]
    elif "value_from_env" in action:
        name = action["value_from_env"]
        if name not in os.environ:
            raise RuntimeError(
                f"replay needs environment variable {name} (not set) to re-run this action"
            )
        rebuilt["value"] = os.environ[name]
    if "key" in action:
        rebuilt["key"] = action["key"]
    return rebuilt
