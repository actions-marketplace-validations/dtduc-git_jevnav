"""Real-time play: a user-provided state probe, a small action set, a fixed rate.

This is the Doom shape, not the web-page shape. A game has no candidate list to
extract — the caller supplies a JS probe that turns `window` into a compact text
state, and a small set of actions. The loop asks Jev "which action?" at a fixed
rate, holds movement keys between decisions (so the last answer keeps applying
while the model thinks) and records every decision into the usual trace.

No gates here: playing a game has no blast radius, and a review queue at 5Hz is
a queue nobody reads. The trace is the evidence instead.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

from .decide import DEFAULT_MODEL, INPUT_USD_PER_MTOK, failed_decision
from .trace import NullWriter, TraceWriter, dom_hash

MAX_STATE_CHARS = 1200


def parse_actions(spec: list[str]) -> dict[str, str]:
    """``left=ArrowLeft`` or a bare key (``Space``) -> {label: key}."""
    actions: dict[str, str] = {}
    for item in spec:
        for part in str(item).split(","):
            part = part.strip()
            if not part:
                continue
            label, _, key = part.partition("=")
            label = label.strip()
            actions[label] = key.strip() or label
    if not actions:
        raise ValueError("no actions given (try --actions 'left=ArrowLeft,right=ArrowRight')")
    return actions


def build_question(goal: str, state: str, actions: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": (
            f"A browser game is being played. Goal: {goal}. Current state: {state}. "
            "Which single action should be taken now? The action stays held until the "
            "next decision, so keep moving when nothing needs changing."
        ),
        "criteria": {label: f"press {key}" for label, key in actions.items()},
    }


def play(
    *,
    goal: str,
    page: Any,
    client: Any,
    writer: TraceWriter | NullWriter,
    state_js: str,
    actions: dict[str, str],
    model: str = DEFAULT_MODEL,
    rate_hz: float = 5.0,
    seconds: float = 20.0,
    max_steps: int = 0,
    score_js: str | None = None,
    policy: str = "jev",
    settle_ms: int = 0,
    seed: int | None = None,
) -> dict[str, Any]:
    """Play until the clock runs out (or max_steps), one JeV decision per tick."""
    rng = random.Random(seed)
    interval = 1.0 / rate_hz if rate_hz > 0 else 0.0
    held: str | None = None
    records: list[dict[str, Any]] = []
    started = time.perf_counter()
    step = 0
    while True:
        elapsed = time.perf_counter() - started
        if seconds and elapsed >= seconds:
            break
        if max_steps and step >= max_steps:
            break
        tick = time.perf_counter()
        step += 1
        state = page.evaluate(state_js)
        state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        state_text = " ".join(state_text.split())[:MAX_STATE_CHARS]
        decision: dict[str, Any]
        if policy == "jev":
            question = build_question(goal, state_text, actions)
            try:
                response, latency_ms = client.system_one(
                    {"game": goal, "state": state_text}, {"action": question}, model=model
                )
                answer = (response.get("answers") or {}).get("action") or {}
                usage = response.get("usage") or {}
                decision = {
                    "choice": answer.get("choice"),
                    "confidence": answer.get("confidence"),
                    "probabilities": answer.get("probabilities") or {},
                    "model": response.get("model") or model,
                    "latency_ms": round(latency_ms, 1),
                    "usage": usage,
                    "cost_usd": (usage.get("input_tokens") or 0) * INPUT_USD_PER_MTOK / 1_000_000,
                    "error": None,
                }
            except Exception as error:
                decision = failed_decision(error)
        else:
            decision = {
                "choice": rng.choice(list(actions)),
                "confidence": None,
                "probabilities": {},
                "model": f"policy:{policy}",
                "latency_ms": 0.0,
                "usage": None,
                "cost_usd": 0.0,
                "error": None,
            }
        chosen = decision.get("choice") if decision.get("choice") in actions else None
        key = actions.get(chosen) if chosen else None
        if key is not None and key != held:
            if held:
                page.keyboard.up(held)
            page.keyboard.down(key)
            held = key
        if settle_ms:
            page.wait_for_timeout(settle_ms)
        step_ms = (time.perf_counter() - tick) * 1000
        records.append(
            writer.step(
                step=step,
                intent=goal,
                action={"type": "hold", "key": key or ""},
                state=state_text,
                state_hash=dom_hash([{"fp": state_text, "value": None}]),
                decision=decision,
                gate={"verdict": "n/a", "reason": "play mode has no gate"},
                result={
                    "correct": None,
                    "executed": key is not None,
                    "error": decision.get("error"),
                },
                elapsed_ms=round(step_ms, 1),
            )
        )
        if key is None:
            break  # blocked or an unknown action: stop rather than spin
        if interval:
            remaining = interval - (time.perf_counter() - tick)
            if remaining > 0:
                page.wait_for_timeout(remaining * 1000)
    if held:
        page.keyboard.up(held)
    wall = time.perf_counter() - started
    score = None
    if score_js:
        try:
            score = page.evaluate(score_js)
        except Exception:
            score = None
    latencies = [
        r["decision"]["latency_ms"] for r in records if r["decision"].get("latency_ms") is not None
    ]
    return {
        "goal": goal,
        "policy": policy,
        "steps": len(records),
        "seconds": round(wall, 2),
        "rate_hz": round(len(records) / wall, 2) if wall else 0.0,
        "requested_rate_hz": rate_hz,
        "score": score,
        "errors": sum(1 for r in records if r["decision"].get("error")),
        "cost_usd": round(sum(r["decision"].get("cost_usd") or 0 for r in records), 6),
        "latency_p50_ms": sorted(latencies)[len(latencies) // 2] if latencies else None,
        "latency_p95_ms": sorted(latencies)[min(len(latencies) - 1, int(0.95 * len(latencies)))]
        if latencies
        else None,
        "actions": {
            label: sum(1 for r in records if r["decision"].get("choice") == label)
            for label in actions
        },
        "steps_detail": records,
    }


__all__ = ["build_question", "parse_actions", "play"]
