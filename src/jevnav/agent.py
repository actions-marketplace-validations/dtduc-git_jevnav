"""Goal-driven loop: Jev decides every step, jevnav executes, gates and records.

One request per step answers four questions together: is the goal already
achieved (status), what to do (action), on what (target) and with which context
value (value_key). The loop stops on `done`, on a gate verdict that is not
`auto`, when the page stops changing, or at `max_steps`.

The model's `done` is a claim, not evidence: with ``success`` (a selector) the
claim is verified against the page, and the result says so either way.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import page as page_module
from .decide import DEFAULT_MODEL, INPUT_USD_PER_MTOK, failed_decision
from .flow import recorded_url
from .gates import verdict
from .trace import NullWriter, TraceWriter, describe_options, dom_hash

STATUSES = ("in_progress", "done", "stuck")
ACTIONS = ("click", "fill", "select", "check", "hover", "press")
FILLABLE = {"textbox", "searchbox", "combobox", "spinbutton"}
ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

TEXT_JS = """() => {
  const main = document.querySelector('main') || document.body;
  const lines = (main.innerText || '').split('\\n')
    .map((line) => line.split(/\\s+/).filter(Boolean).join(' '))
    .filter(Boolean);
  return lines.join('\\n');
}"""


def text_digest(page: Any, limit: int = 700) -> str:
    """Visible text, collapsed — the state the model needs to judge 'done'."""
    try:
        text = page.evaluate(TEXT_JS)
    except Exception:
        return ""
    return text[:limit]


def context_value(raw: str) -> tuple[str | None, str | None]:
    """``${VAR}`` is resolved from the environment and recorded as a name only."""
    match = ENV_PATTERN.match(str(raw))
    if match:
        name = match.group(1)
        if name not in os.environ:
            raise KeyError(f"environment variable {name} is not set (needed by the goal context)")
        return os.environ[name], name
    return str(raw), None


def build_state(
    *,
    goal: str,
    context_keys: list[str],
    page: Any,
    candidates: list[dict[str, Any]],
    history: list[str],
    step: int,
    max_steps: int,
) -> str:
    return (
        f"Goal: {goal}\n"
        f"Step {step} of {max_steps}.\n"
        f"Context values available: {', '.join(context_keys) or '(none)'}\n"
        f"Current page: {page.title()!r} — {page.url}\n"
        f"Visible text on the page:\n{text_digest(page)}\n"
        f"{len(candidates)} interactive elements are listed as options of the target question.\n"
        "Steps so far:\n" + ("\n".join(f"  {line}" for line in history) if history else "  (none)")
    )


def build_questions(
    goal: str, context_keys: list[str], candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    options = describe_options(candidates)
    options["none"] = "No visible element fits."
    questions: dict[str, Any] = {
        "status": {
            "type": "choice",
            "instructions": (
                f"The goal is: {goal}. Decide from the current page state and the steps already "
                "taken: is the goal already achieved (done), is there still a sensible next step "
                "(in_progress), or can it not be reached from here (stuck)? Answer done only if "
                "the page itself shows it; answer stuck if no listed element can make progress."
            ),
            "criteria": {
                "in_progress": "A sensible next step exists.",
                "done": "The page already shows the goal is achieved.",
                "stuck": "No listed element can make progress towards the goal.",
            },
        },
        "action": {
            "type": "choice",
            "instructions": (
                "Which action should be performed next? click presses buttons and links, fill "
                "types a context value into a text input, select picks a dropdown option, check "
                "ticks a checkbox, press sends a key, hover moves the pointer."
            ),
            "criteria": {
                "click": "Press the element (buttons, links).",
                "fill": "Type a context value into a text input (textbox, searchbox, combobox).",
                "select": "Pick an option in a dropdown.",
                "check": "Tick a checkbox or radio button.",
                "hover": "Move the pointer over the element.",
                "press": "Send a keyboard key to the element.",
            },
        },
        "target": {
            "type": "choice",
            "instructions": (
                "Which single listed element should the chosen action be performed on? Pick "
                "'none' if no element fits the action and the goal."
            ),
            "criteria": options,
        },
    }
    if context_keys:
        questions["value_key"] = {
            "type": "choice",
            "instructions": (
                "If the action you chose is fill or select, you MUST pick the context value that "
                "belongs in the target element here — never 'none' for a fill. Pick 'none' only "
                "when the chosen action does not need a value (click, check, hover, press)."
            ),
            "criteria": {
                **{key: f"the value of {key}" for key in context_keys},
                "none": "No value is needed.",
            },
        }
    return questions


def alternatives(
    candidates: list[dict[str, Any]], decision: dict[str, Any], limit: int = 3
) -> list[dict[str, Any]]:
    """The runners-up, so a caller can resolve a review without guessing.

    Surfaced when the gate wants a human: a more specific `browse` intent scores
    much higher on the same page (measured on Wikipedia: p 0.44 -> 0.93).
    """
    probabilities = decision.get("probabilities") or {}
    ranked = [
        {
            "name": candidate["name"],
            "role": candidate["role"],
            "confidence": round(probabilities.get(candidate["cid"], 0.0), 3),
        }
        for candidate in candidates
        if candidate["cid"] != decision.get("choice") and probabilities.get(candidate["cid"])
    ]
    ranked.sort(key=lambda item: item["confidence"], reverse=True)
    return ranked[:limit]


def resolve_value_key(
    answer_key: str | None, target_name: str | None, context: dict[str, str]
) -> tuple[str | None, str | None]:
    """The model's choice, or a deterministic match of the field name to a context key."""
    if answer_key in context:
        return answer_key, "model"
    if target_name:
        for key in context:
            if key.casefold() in target_name.casefold():
                return key, "name-match"
    return None, None


def run_goal(
    goal: str,
    *,
    page: Any,
    client: Any,
    gates: dict[str, Any],
    writer: TraceWriter | NullWriter,
    model: str = DEFAULT_MODEL,
    context: dict[str, str] | None = None,
    start: str | None = None,
    success: str | None = None,
    max_steps: int = 8,
    min_confidence: float | None = None,
    max_candidates: int | None = None,
    dry_run: bool = False,
    allow_risky: bool = False,
    settle_ms: int = 300,
) -> dict[str, Any]:
    """Walk towards the goal until it is done, blocked, or out of steps."""
    context = context or {}
    if start:
        page.goto(start, wait_until="domcontentloaded")
    history: list[str] = []
    records: list[dict[str, Any]] = []
    status = "max_steps"
    reason: str | None = None
    unchanged = 0
    finished = False
    for step in range(1, max_steps + 1):
        if settle_ms:
            page.wait_for_timeout(settle_ms)
        candidates, total, dropped = page_module.extract(page, max_candidates)
        state = build_state(
            goal=goal,
            context_keys=list(context),
            page=page,
            candidates=candidates,
            history=history,
            step=step,
            max_steps=max_steps,
        )
        questions = build_questions(goal, list(context), candidates)
        try:
            response, latency_ms = client.system_one(state, questions, model=model)
            answers = response.get("answers") or {}
            usage = response.get("usage") or {}
            decision = {
                "status": (answers.get("status") or {}).get("choice"),
                "status_confidence": (answers.get("status") or {}).get("confidence"),
                "action": (answers.get("action") or {}).get("choice"),
                "action_confidence": (answers.get("action") or {}).get("confidence"),
                "choice": (answers.get("target") or {}).get("choice"),
                "confidence": (answers.get("target") or {}).get("confidence"),
                "value_key": (answers.get("value_key") or {}).get("choice"),
                "probabilities": (answers.get("target") or {}).get("probabilities") or {},
                "model": response.get("model") or model,
                "latency_ms": round(latency_ms, 1),
                "usage": usage,
                "cost_usd": (usage.get("input_tokens") or 0) * INPUT_USD_PER_MTOK / 1_000_000,
                "error": None,
            }
        except Exception as error:
            decision = failed_decision(error) | {"status": None, "action": None, "value_key": None}
        chosen = page_module.by_cid(candidates, decision.get("choice") or "")
        if chosen is not None:
            decision["chosen_fp"] = chosen["fp"]
            decision["chosen_name"] = chosen["name"]
        else:
            decision["chosen_fp"] = None
            decision["chosen_name"] = None
        decision["alternatives"] = alternatives(candidates, decision)
        gate, gate_reason = verdict(
            decision,
            intent=goal,
            candidate=chosen,
            dropped=dropped,
            gates=gates,
            default_key="loop_min_confidence",
            min_confidence=min_confidence,
        )
        record: dict[str, Any] = {
            "step": step,
            "intent": goal,
            "action": {"type": decision.get("action") or "none"},
            "url": recorded_url(page.url, _base_dir(writer)),
            "title": page.title(),
            "total_on_page": total,
            "dropped": dropped,
            "dom_hash": dom_hash(candidates),
            "candidates": candidates,
            "expected_cid": None,
            "decision": decision,
            "gate": {"verdict": gate, "reason": gate_reason},
            "locator": None,
            "result": {"correct": None, "executed": False, "error": None},
        }
        if chosen is not None:
            selector, unique = page_module.locator_for(page, chosen)
            record["locator"] = {"selector": selector, "unique": unique}
        status = decision.get("status") or "error"
        if status == "done":
            reason = f"model says the goal is achieved (p={decision.get('status_confidence')})"
            verified = None
            if success:
                try:
                    verified = page.locator(success).first.is_visible()
                except Exception:
                    verified = False
                if not verified:
                    status = "unverified"
                    reason = f"the model says done but {success!r} is not visible on the page"
            record["verify"] = {"selector": success, "verified": verified}
            record["gate"] = {"verdict": "n/a", "reason": "goal achieved, no action needed"}
            records.append(writer.step(**record))
            finished = True
            break
        if status == "stuck" or chosen is None or gate == "blocked":
            status = "stuck" if status != "error" else "error"
            reason = gate_reason or "no element can make progress"
            records.append(writer.step(**record))
            finished = True
            break
        if gate == "review" and not allow_risky:
            status = "review"
            reason = f"{gate_reason} — the loop stopped before acting"
            records.append(writer.step(**record))
            finished = True
            break
        action = decision.get("action")
        if action not in ACTIONS:
            status, reason = "error", f"model chose an unknown action {action!r}"
            records.append(writer.step(**record))
            finished = True
            break
        if action == "fill" and chosen["role"] not in FILLABLE:
            status, reason = "blocked", f"fill does not apply to role {chosen['role']!r}"
            records.append(writer.step(**record))
            finished = True
            break
        action_dict: dict[str, Any] = {"type": action}
        if action in {"fill", "select"}:
            key, source = resolve_value_key(decision.get("value_key"), chosen["name"], context)
            if key is None:
                status, reason = "blocked", f"no context value fits the field {chosen['name']!r}"
                records.append(writer.step(**record))
                finished = True
                break
            raw = context[key]
            value, env_name = context_value(raw)
            action_dict["value"] = value
            record["action"] = (
                {"type": action, "value_from_env": env_name}
                if env_name
                else {"type": action, "value": value}
            )
            record["value_source"] = source
        if action == "press":
            action_dict["key"] = "Enter"
        if not dry_run:
            try:
                page_module.execute(
                    page, chosen, action_dict, candidates=candidates, settle_ms=settle_ms
                )
                record["result"]["executed"] = True
            except Exception as error:
                record["result"]["error"] = f"{type(error).__name__}: {error}"
        after, _, _ = page_module.extract(page)
        changed = dom_hash(after) != record["dom_hash"]
        unchanged = 0 if changed else unchanged + 1
        history.append(
            f"step {step}: {action} on {chosen['name']!r}"
            + (f" with {decision.get('value_key')}" if action in {"fill", "select"} else "")
            + (
                f" -> ERROR {record['result']['error']}"
                if record["result"]["error"]
                else f" -> page {'changed' if changed else 'unchanged'}"
            )
        )
        records.append(writer.step(**record))
        if unchanged >= 2:
            status, reason = "no_progress", "two steps in a row changed nothing on the page"
            finished = True
            break
    if not finished and status != "max_steps":
        status, reason = "max_steps", f"{max_steps} steps without reaching the goal"
    verified = (records[-1].get("verify") or {}).get("verified") if records else None
    if status == "done" and not success:
        reason = (reason or "") + " — pass --success <selector> to verify the outcome"
    return {
        "goal": goal,
        "status": status,
        "reason": reason,
        "verified": verified,
        "success": success,
        "steps": records,
        "context_keys": list(context),
        "history": history,
    }


def _base_dir(writer: TraceWriter | NullWriter) -> Any:
    return writer.path.resolve().parent if writer.path else Path.cwd()


def summarize_goal(result: dict[str, Any]) -> dict[str, Any]:
    gates = [record["gate"]["verdict"] for record in result["steps"]]
    latencies = [
        r["decision"]["latency_ms"]
        for r in result["steps"]
        if r["decision"].get("latency_ms") is not None
    ]
    return {
        "goal": result["goal"],
        "status": result["status"],
        "verified": result["verified"],
        "reason": result["reason"],
        "steps": len(result["steps"]),
        "auto": gates.count("auto"),
        "review": gates.count("review"),
        "blocked": gates.count("blocked"),
        "stopped": gates.count("n/a"),
        "alternatives": (result["steps"][-1]["decision"].get("alternatives") or [])
        if result["steps"] and result["status"] in {"review", "stuck"}
        else [],
        "hint": (
            "Call browse again with a more specific intent (name the element and where it is); "
            "specific intents score much higher than a broad goal."
        )
        if result["status"] in {"review", "stuck"}
        else None,
        "cost_usd": round(sum(r["decision"].get("cost_usd") or 0 for r in result["steps"]), 6),
        "latency_p50_ms": sorted(latencies)[len(latencies) // 2] if latencies else None,
        "latency_p95_ms": sorted(latencies)[min(len(latencies) - 1, int(0.95 * len(latencies)))]
        if latencies
        else None,
    }


__all__ = [
    "ACTIONS",
    "STATUSES",
    "build_questions",
    "build_state",
    "context_value",
    "run_goal",
    "summarize_goal",
    "text_digest",
]
