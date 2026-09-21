"""gates.yaml: when a decision is safe to act on unattended.

Three verdicts, no ambiguity:

- ``auto``    — act unattended (confidence at or above threshold, nothing risky)
- ``review``  — a human confirms before the action happens
- ``blocked`` — no decision was possible (model answered ``none``, or the call failed)

Risky actions are matched as regular expressions against the intent, the chosen
element's name and its role, so a Delete button is risky even when the intent
sounds harmless. Submitting a form is deliberately *not* risky by default —
that is what browser automation is for; a "Pay" or "Place order" button still
matches through its name.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MIN_CONFIDENCE = 0.9
DEFAULT_RISKY = [
    r"\b(delete|remove|destroy|drop)\b",
    r"\b(pay|payment|purchase|buy|checkout|transfer|withdraw|refund)\b",
    r"\b(send|publish|post|upload|invite|share)\b",
    r"\b(place|confirm)\s+(the\s+)?(order|payment|purchase|booking)\b",
    r"\b(confirm|approve|authorize|grant|revoke)\b",
    r"\b(close|deactivate|terminate|downgrade|unsubscribe|cancel)\b.*\b(account|subscription|plan|workspace)\b",
    r"\b(rotate|reset|delete)\b.*\b(key|token|credential|secret|password)\b",
]

AUTO = "auto"
REVIEW = "review"
BLOCKED = "blocked"


def default_gates() -> dict[str, Any]:
    return {
        "min_confidence": DEFAULT_MIN_CONFIDENCE,
        "risky": list(DEFAULT_RISKY),
        "truncated": REVIEW,
        "intents": {},
    }


def load_gates(path: str | Path | None) -> dict[str, Any]:
    gates = default_gates()
    if path is None:
        return gates
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: gates must be a mapping")
    unknown = set(raw) - set(gates)
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    gates.update(raw)
    if not isinstance(gates["risky"], list) or not all(isinstance(p, str) for p in gates["risky"]):
        raise ValueError(f"{path}: risky must be a list of regular expressions")
    if not isinstance(gates["intents"], dict):
        raise ValueError(f"{path}: intents must be a mapping of intent pattern -> overrides")
    return gates


def threshold_for(intent: str, gates: dict[str, Any]) -> float:
    """Per-intent override by fnmatch pattern; the most specific pattern wins."""
    best: tuple[int, float] | None = None
    for pattern, overrides in gates.get("intents", {}).items():
        if fnmatch.fnmatch(intent.casefold(), pattern.casefold()):
            confidence = float(overrides.get("min_confidence", gates["min_confidence"]))
            score = (len(pattern), confidence)
            if best is None or score > best:
                best = score
    return best[1] if best else float(gates["min_confidence"])


def risk_match(intent: str, candidate: dict[str, Any] | None, gates: dict[str, Any]) -> str | None:
    subject = " ".join(
        part
        for part in [
            intent,
            (candidate or {}).get("name") or "",
            (candidate or {}).get("role") or "",
        ]
        if part
    ).casefold()
    for pattern in gates.get("risky", []):
        if re.search(pattern, subject, re.IGNORECASE):
            return pattern
    return None


def verdict(
    decision: dict[str, Any],
    *,
    intent: str,
    candidate: dict[str, Any] | None,
    dropped: int,
    gates: dict[str, Any],
) -> tuple[str, str | None]:
    """Classify one recorded decision. Returns (verdict, reason)."""
    if decision.get("error"):
        return BLOCKED, f"model call failed: {decision['error']}"
    if decision.get("choice") in (None, "none"):
        return BLOCKED, "no element matched the intent"
    confidence = decision.get("confidence")
    if confidence is None:
        return BLOCKED, "no confidence returned"
    if dropped and gates.get("truncated") == REVIEW:
        return REVIEW, f"{dropped} candidates were dropped before the model saw the page"
    risk = risk_match(intent, candidate, gates)
    if risk:
        return REVIEW, f"risky action matched {risk!r}"
    threshold = threshold_for(intent, gates)
    if confidence < threshold:
        return REVIEW, f"p={confidence:.2f} below threshold {threshold:.2f}"
    return AUTO, None
