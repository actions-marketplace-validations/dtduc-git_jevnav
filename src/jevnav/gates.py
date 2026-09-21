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
# Loop mode asks four questions at once, and the target confidence runs lower
# even when the decision is right (measured 2026-09-21: correct loop decisions
# at p 0.41-0.99, wrong ones at 0.39-0.47 — p does not separate them). The loop
# is kept safe by role/value validation, progress detection, the risky patterns
# and the success verification, not by a high threshold.
DEFAULT_LOOP_MIN_CONFIDENCE = 0.5
# A caller may lower its own bar, but not to zero: an LLM passing 0 turned the
# gate into "no risky regex matched, so run". Risky patterns still always win.
MIN_CONFIDENCE_FLOOR = 0.3
DEFAULT_RISKY = [
    r"\b(delete|remove|destroy|drop)\b",
    r"\b(pay|payment|purchase|buy|checkout|transfer|withdraw|refund)\b",
    r"\b(send|publish|post|upload|invite|share)\b",
    r"\b(place|confirm)\s+(the\s+)?(order|payment|purchase|booking)\b",
    r"\b(confirm|approve|authorize|grant|revoke)\b",
    r"\b(close|deactivate|terminate|downgrade|unsubscribe|cancel)\b.*\b(account|subscription|plan|workspace)\b",
    r"\b(rotate|reset|delete)\b.*\b(key|token|credential|secret|password)\b",
    # the same actions in the languages real UIs are written in: English-only
    # patterns meant the gate quietly became "p >= threshold" on those pages
    r"(xoá|xóa|hủy|xoa tai khoan|đóng tài khoản|thanh toán|chuyển tiền|hoàn tiền)",
    r"(löschen|entfernen|konto schließen|bezahlen|kaufen|überweisen|erstatten)",
    r"(supprimer|effacer|fermer le compte|payer|acheter|virement|rembourser)",
    r"(eliminar|borrar|cerrar la cuenta|pagar|comprar|transferir|reembolsar)",
    r"(excluir|apagar|encerrar a conta|pagar|comprar|transferir|reembolsar)",
    r"(削除|消去|アカウントを削除|支払|購入|送金|返金)",
    r"(删除|删除账户|关闭账户|支付|付款|购买|转账|退款)",
    r"(삭제|계정 삭제|결제|구매|송금|환불)",
]

# Real pages exceed the candidate cap routinely (Wikipedia's main page drops
# ~10 links), so truncation is recorded as a warning by default. Set
# `truncated: review` to gate on it anyway.
DEFAULT_TRUNCATED = "warn"

AUTO = "auto"
REVIEW = "review"
BLOCKED = "blocked"


def default_gates() -> dict[str, Any]:
    return {
        "min_confidence": DEFAULT_MIN_CONFIDENCE,
        "loop_min_confidence": DEFAULT_LOOP_MIN_CONFIDENCE,
        "risky": list(DEFAULT_RISKY),
        "truncated": DEFAULT_TRUNCATED,
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
    if gates["truncated"] not in {"warn", "review", None}:
        raise ValueError(f"{path}: truncated must be 'warn' or 'review'")
    return gates


def threshold_for(
    intent: str,
    gates: dict[str, Any],
    *,
    default_key: str = "min_confidence",
    override: float | None = None,
) -> float:
    """The confidence bar: an explicit override, else per-intent, else the default.

    ``override`` is the caller's own bar (``browse(min_confidence=...)``,
    ``go --min-confidence``). Risky patterns and the deterministic checks are
    never overridable — only the confidence question is delegated to whoever
    knows the page best.
    """
    if override is not None:
        return max(float(override), MIN_CONFIDENCE_FLOOR)
    best: tuple[int, float] | None = None
    for pattern, overrides in gates.get("intents", {}).items():
        if fnmatch.fnmatch(intent.casefold(), pattern.casefold()):
            confidence = float(overrides.get("min_confidence", gates[default_key]))
            score = (len(pattern), confidence)
            if best is None or score > best:
                best = score
    return best[1] if best else float(gates[default_key])


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
    default_key: str = "min_confidence",
    min_confidence: float | None = None,
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
    threshold = threshold_for(intent, gates, default_key=default_key, override=min_confidence)
    if confidence < threshold:
        return REVIEW, f"p={confidence:.2f} below threshold {threshold:.2f}"
    return AUTO, None
