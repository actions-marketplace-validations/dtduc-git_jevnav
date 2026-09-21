"""Offline helpers: a Jev client that answers from a script, and a browser fixture."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jevassert.client import JevClient

FIXTURES = Path(__file__).parent / "fixtures"


class FakeJev:
    """A JevClient whose transport is a dict: intent keyword -> option name (or (name, p)).

    Matching is on the *option description* the model would see, so tests assert
    against what the model is actually shown, not against internal ids.
    """

    def __init__(
        self, answers: dict[str, str | tuple[str, float]], *, default: str = "none"
    ) -> None:
        self.answers = answers
        self.default = default
        self.calls: list[dict[str, Any]] = []

    def client(self, *, api_key: str = "test-key") -> JevClient:
        return JevClient(api_key=api_key, transport=self._transport)

    def _transport(self, method: str, url: str, headers: dict[str, str], body: dict[str, Any]):
        question = body["questions"]["target"]
        criteria: dict[str, str] = question["criteria"]
        instructions = question["instructions"]
        self.calls.append(
            {"instructions": instructions, "criteria": criteria, "state": body["state"]}
        )
        target, confidence = self._answer(instructions, criteria)
        return (
            200,
            {
                "model": "jev-fake-1",
                "usage": {"input_tokens": 120, "output_tokens": 8},
                "answers": {
                    "target": {
                        "type": "choice",
                        "choice": target,
                        "confidence": confidence,
                        "probabilities": {
                            cid: (confidence if cid == target else 0.0) for cid in criteria
                        },
                    }
                },
            },
            None,
        )

    def _answer(self, instructions: str, criteria: dict[str, str]) -> tuple[str, float]:
        for keyword, expected in self.answers.items():
            if keyword.casefold() not in instructions.casefold():
                continue
            name, confidence = expected if isinstance(expected, tuple) else (expected, 0.95)
            if name == "none":
                return "none", confidence
            for cid, description in criteria.items():
                if description.casefold().startswith(name.casefold()):
                    return cid, confidence
            raise AssertionError(
                f"fake answer {name!r} matches no option in {list(criteria.values())}"
            )
        return self.default, 0.5


def fixture_url(name: str) -> str:
    return (FIXTURES / name).as_uri()
