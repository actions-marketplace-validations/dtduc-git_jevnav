"""Ask Jev which candidate to act on, and turn the answer into a record.

One step = one choice question over the candidate list. The question wording
and the option format are the ones measured at 100% accuracy in the spike —
change them only with numbers.
"""

from __future__ import annotations

from typing import Any

from jevassert.client import JevClient, JevError

from .trace import describe_options

INPUT_USD_PER_MTOK = 0.042  # TypeSafe early-access list price; output tokens are free.
NONE = "none"
DEFAULT_MODEL = "jev-latest"


def build_question(
    url: str, title: str, intent: str, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    options = describe_options(candidates)
    options[NONE] = "No visible element matches this intent."
    return {
        "type": "choice",
        "instructions": (
            f"On the page {url!r} ({title!r}) a browser agent wants to: {intent}. "
            "Which single listed element should it act on? Pick 'none' if no element fits."
        ),
        "criteria": options,
    }


def ask(
    client: JevClient,
    *,
    url: str,
    title: str,
    intent: str,
    candidates: list[dict[str, Any]],
    model: str = DEFAULT_MODEL,
    total_on_page: int = 0,
    dropped: int = 0,
) -> dict[str, Any]:
    """One Jev call -> a decision record ready to be written into a trace."""
    question = build_question(url, title, intent, candidates)
    state = {
        "page": (
            f"{title} — {url}\n{total_on_page} visible interactive elements; "
            f"{len(candidates)} listed" + (f" ({dropped} dropped)" if dropped else "")
        )
    }
    response, latency_ms = client.system_one(state, {"target": question}, model=model)
    answer = (response.get("answers") or {}).get("target") or {}
    usage = response.get("usage") or {}
    choice = answer.get("choice")
    chosen = next((c for c in candidates if c["cid"] == choice), None)
    return {
        "choice": choice,
        "chosen_fp": chosen["fp"] if chosen else None,
        "chosen_name": chosen["name"] if chosen else None,
        "confidence": answer.get("confidence"),
        "probabilities": answer.get("probabilities") or {},
        "model": response.get("model") or model,
        "latency_ms": round(latency_ms, 1),
        "usage": usage,
        "cost_usd": (usage.get("input_tokens") or 0) * INPUT_USD_PER_MTOK / 1_000_000,
        "error": None,
    }


def failed_decision(error: Exception) -> dict[str, Any]:
    """A step that never reached the model is recorded, not hidden."""
    return {
        "choice": None,
        "chosen_fp": None,
        "chosen_name": None,
        "confidence": None,
        "probabilities": {},
        "model": None,
        "latency_ms": None,
        "usage": None,
        "cost_usd": 0.0,
        "error": str(error) if isinstance(error, JevError) else f"{type(error).__name__}: {error}",
    }


__all__ = [
    "DEFAULT_MODEL",
    "INPUT_USD_PER_MTOK",
    "NONE",
    "ask",
    "build_question",
    "failed_decision",
]
