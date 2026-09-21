"""The trace format: what a decision is, and what makes it replayable.

One JSONL file per run: a ``run`` header, then one ``step`` per decision.
The step records the candidate set *as the model saw it* — each candidate with
its semantic fingerprint (``fp``) — and the decision made from it. Replay uses
the recorded fingerprints, never a re-derivation, so the algorithm can evolve
without invalidating old traces.

See SPEC.md for the canonical description.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from . import __version__

SPEC = 0


def normalize_name(name: str) -> str:
    """Collapse whitespace and case so cosmetic markup changes don't drift."""
    return " ".join((name or "").split()).casefold()


def clean_name(name: str) -> str:
    """The accessible name as shown to the model: real casing, no stray whitespace."""
    return " ".join((name or "").split())


def fingerprint(role: str, name: str) -> str:
    """Semantic identity of an element: role + accessible name."""
    return f"{normalize_name(role)}|{normalize_name(name)}"


def dom_hash(candidates: Iterable[dict[str, Any]]) -> str:
    """Hash of what the model observes: identity plus current field values.

    Values are part of the observation, so a fill counts as a state change.
    """
    payload = json.dumps(sorted(f"{c['fp']}={c.get('value') or ''}" for c in candidates))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def make_candidate(
    cid: str,
    role: str,
    name: str,
    *,
    tag: str | None = None,
    type: str | None = None,
    href: str | None = None,
    placeholder: str | None = None,
    scope: str | None = None,
    value: str | None = None,
    disabled: bool = False,
    in_viewport: bool = True,
    frame: int = 0,
    rank: int = 0,
) -> dict[str, Any]:
    return {
        "cid": cid,
        "role": role,
        "name": clean_name(name),
        "fp": fingerprint(role, name),
        "tag": tag,
        "type": type,
        "href": href,
        "placeholder": placeholder,
        "scope": scope,
        "value": value,
        "disabled": disabled,
        "in_viewport": in_viewport,
        "frame": frame,
        "rank": rank,
    }


def describe(candidate: dict[str, Any], *, disambiguate: bool = False) -> str:
    """One line per candidate, as shown to the model.

    ``disambiguate`` adds the scope, and is set only when two candidates share a
    name. Measured 2026-09-21: adding the scope to every line cost accuracy
    (a fieldset legend "Sign in" reads like the action "Sign in", p 0.85 → 0.47),
    while adding it for duplicate names only rescues those cases and costs
    nothing on pages with unique names.
    """
    parts = [f"{candidate['name']} — {candidate['role']}"]
    if candidate.get("value"):
        parts.append(f'[value: "{candidate["value"]}"]')
    if candidate.get("href"):
        parts.append(f"→ {candidate['href']}")
    if disambiguate and candidate.get("scope"):
        parts.append(f"in {candidate['scope']!r}")
    if candidate.get("disabled"):
        parts.append("[disabled]")
    return " ".join(parts)


def describe_options(candidates: list[dict[str, Any]]) -> dict[str, str]:
    """The choice question's option map, with scope shown only where names collide."""
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate["name"]] = counts.get(candidate["name"], 0) + 1
    return {c["cid"]: describe(c, disambiguate=counts[c["name"]] > 1) for c in candidates}


class TraceWriter:
    """Append-only JSONL writer; every step is flushed so a crash keeps evidence."""

    def __init__(self, path: str | Path, **run_meta: Any) -> None:
        from .page import ORDER_SPEC  # page imports trace, so import at call time

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w")
        self.run_meta = {
            "kind": "run",
            "spec": SPEC,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": f"jevnav/{__version__}",
            "order_spec": ORDER_SPEC,
            **run_meta,
        }
        self._write(self.run_meta)

    def _write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    def step(self, **fields: Any) -> dict[str, Any]:
        record = {"kind": "step", **fields}
        self._write(record)
        return record

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class NullWriter:
    """No trace was asked for; steps are still built and returned."""

    path: Path | None = None

    def step(self, **fields: Any) -> dict[str, Any]:
        return {"kind": "step", **fields}


def read_trace(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (run header, steps). Rejects records that are not steps."""
    run: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    for line_no, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("kind") == "run":
            run = record
        elif record.get("kind") == "step":
            steps.append(record)
        else:
            raise ValueError(f"{path}:{line_no}: unknown record kind {record.get('kind')!r}")
    if not steps:
        raise ValueError(f"{path}: no steps in trace")
    return run, steps


def iter_fingerprints(step: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for candidate in step["candidates"]:
        yield candidate["fp"], candidate


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_name(text)).strip("-")[:limit] or "step"
