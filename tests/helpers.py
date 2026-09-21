"""Offline helpers: a Jev client that answers from a script, and fixture URLs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jevassert.client import JevClient

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = Path(__file__).parent.parent / "examples"

LOOP_DEFAULTS = {"status": "in_progress", "action": "click", "value_key": "none", "target": "none"}


class FakeJev:
    """A JevClient whose transport answers from a script.

    Single questions match an intent keyword to an option name; goal-loop
    requests (they carry a ``status`` question) consume ``script`` entries in
    order: ``{"status": ..., "action": ..., "target": <option name>, "value_key": ...}``.
    Matching is on the option description the model would see, so tests assert
    against what the model is shown, not against internal ids.
    """

    def __init__(
        self,
        answers: dict[str, str | tuple[str, float]] | None = None,
        *,
        default: str = "none",
        script: list[dict[str, str]] | None = None,
        confidence: float = 0.97,
    ) -> None:
        self.answers = answers or {}
        self.default = default
        self.script = list(script or [])
        self.confidence = confidence
        self.calls: list[dict[str, Any]] = []

    def client(self, *, api_key: str = "test-key") -> JevClient:
        return JevClient(api_key=api_key, transport=self._transport)

    def _transport(self, method: str, url: str, headers: dict[str, str], body: dict[str, Any]):
        questions = body["questions"]
        self.calls.append({"state": body["state"], "questions": questions})
        answers = (
            self._loop_answers(questions)
            if "status" in questions
            else self._single_answers(questions)
        )
        return (
            200,
            {
                "model": "jev-fake-1",
                "usage": {"input_tokens": 120, "output_tokens": 8},
                "answers": answers,
            },
            None,
        )

    def _single_answers(self, questions: dict[str, Any]) -> dict[str, Any]:
        if "target" not in questions and "action" in questions:  # play mode
            criteria: dict[str, str] = questions["action"]["criteria"]
            if self.script:
                name = self.script.pop(0)
                choice = name if name in criteria else self._find(criteria, name)
                return {"action": self._choice(choice, self.confidence, list(criteria))}
            choice, confidence = self._answer(questions["action"]["instructions"], criteria)
            return {"action": self._choice(choice, confidence, list(criteria))}
        criteria = questions["target"]["criteria"]
        instructions = questions["target"]["instructions"]
        choice, confidence = self._answer(instructions, criteria)
        return {"target": self._choice(choice, confidence, list(criteria))}

    @staticmethod
    def _choice(choice: str, confidence: float, cids: list[str]) -> dict[str, Any]:
        """The winner plus a plausible remainder, the way the real endpoint answers."""
        others = [cid for cid in cids if cid != choice]
        share = round((1 - confidence) / len(others), 3) if others else 0.0
        return {
            "type": "choice",
            "choice": choice,
            "confidence": confidence,
            "probabilities": {cid: (confidence if cid == choice else share) for cid in cids},
        }

    def _loop_answers(self, questions: dict[str, Any]) -> dict[str, Any]:
        step = self.script.pop(0) if self.script else {"status": "done"}
        answers: dict[str, Any] = {}
        for qid in ("status", "action", "value_key"):
            if qid not in questions:
                continue
            choice = step.get(qid, LOOP_DEFAULTS[qid])
            if qid == "value_key" and choice != "none":
                assert choice in questions[qid]["criteria"], f"{choice!r} is not a context key"
            answers[qid] = self._choice(choice, self.confidence, list(questions[qid]["criteria"]))
        criteria = questions["target"]["criteria"]
        target = step.get("target", "none")
        choice = "none" if target == "none" else self._find(criteria, target)
        answers["target"] = self._choice(choice, self.confidence, list(criteria))
        return answers

    def _answer(self, instructions: str, criteria: dict[str, str]) -> tuple[str, float]:
        for keyword, expected in self.answers.items():
            if keyword.casefold() not in instructions.casefold():
                continue
            name, confidence = (
                expected if isinstance(expected, tuple) else (expected, self.confidence)
            )
            if name == "none":
                return "none", confidence
            return self._find(criteria, name), confidence
        return self.default, 0.5

    @staticmethod
    def _find(criteria: dict[str, str], name: str) -> str:
        for cid, description in criteria.items():
            if description.casefold().startswith(name.casefold()):
                return cid
        raise AssertionError(f"fake answer {name!r} matches no option in {list(criteria.values())}")


def fixture_url(name: str) -> str:
    return (FIXTURES / name).as_uri()


def example_url(name: str) -> str:
    """Examples are shipped artifacts: tests point at them so they cannot rot."""
    return (EXAMPLES / name).as_uri()
