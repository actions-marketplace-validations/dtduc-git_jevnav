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
from .trace import ORDER_SPEC, read_trace

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


def order_spec_of_tool(tool: str | None) -> int | None:
    """The ordering version of a trace that predates ``run.order_spec``.

    0.1.0 had no shortlist sort, 0.1.1–0.1.4 sorted within a frame, 0.1.5+ sorts
    across frames. ``None`` means unknown, and unknown never excuses movement.
    """
    if not isinstance(tool, str) or not tool.startswith("jevnav/"):
        return None
    try:
        version = tuple(int(part) for part in tool.removeprefix("jevnav/").split(".")[:3])
    except ValueError:
        return None
    version += (0,) * (3 - len(version))
    if version < (0, 1, 1):
        return 0
    if version < (0, 1, 5):
        return 1
    return 2


def order_spec_of_run(run: dict[str, Any]) -> int | None:
    """The ordering version that produced a trace: the field, else mapped from ``tool``."""
    spec = run.get("order_spec")
    if spec is not None:
        return spec
    return order_spec_of_tool(run.get("tool"))


def replay_step(
    page: Any,
    step: dict[str, Any],
    *,
    url: str | None = None,
    force_navigate: bool = False,
    normalize: list[str] | None = None,
    max_candidates: int | None = None,
    recorded_order_spec: int | None = None,
) -> dict[str, Any]:
    """Re-resolve one recorded decision against the page as it is now.

    ``recorded_order_spec`` is the trace's shortlist ordering version; a position
    difference with an identical candidate set is only explained as re-ranking
    across *different* ordering versions. Same version + same set means the page
    itself reordered.
    """
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
        current, _, _ = page_module.extract(page, max_candidates)
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
    result["chosen_frame"] = chosen.get("frame", 0) if chosen else 0
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
        # With an identical candidate set, a position difference can only be a
        # re-ranking — and only *different* ordering logic may re-rank. The same
        # ordering + same set means the page itself reordered.
        same_order = recorded_order_spec == ORDER_SPEC
        reranked = result["page_identical"] and recorded_order_spec is not None and not same_order
        result["moved"] = index != recorded_index and not reranked
        result["verdict"] = MOVED if result["moved"] else OK
        result["reason"] = f"resolved to {current[index]['name']!r} at position {index}"
        if index != recorded_index and reranked:
            result["reason"] += (
                f" (the shortlist was re-ranked: trace ordering v{recorded_order_spec}, "
                f"now v{ORDER_SPEC})"
            )
    return result


def replay_trace(
    trace_path: str | Path,
    *,
    page: Any,
    swap: str | Path | None = None,
    execute: bool = False,
    settle_ms: int = 300,
    normalize: list[str] | None = None,
    max_candidates: int | None = None,
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
        result = replay_step(
            page,
            step,
            url=target,
            force_navigate=index == 0,
            normalize=normalize,
            recorded_order_spec=order_spec_of_run(run),
        )
        if execute and result["verdict"] in {OK, MOVED}:
            try:
                action = action_for_replay(step["action"])
                if action["type"] != "none" and result.get("chosen_fp"):
                    page_module.execute_fp(
                        page,
                        result["chosen_fp"],
                        action,
                        frame_index=result.get("chosen_frame", 0),
                        limit=max_candidates,
                        settle_ms=settle_ms,
                    )
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
