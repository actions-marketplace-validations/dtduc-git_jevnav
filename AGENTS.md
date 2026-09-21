# AGENTS.md — working notes for jevnav

Browser automation whose decisions you can replay, test and audit. Jev picks
the element, jevnav records the trace, gates risky actions and replays offline
in CI. Python, Playwright, local-first.

## Commands

```bash
uv sync
uv run playwright install chromium-headless-shell   # once
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Live smoke (needs `TYPESAFE_API_KEY` or `~/.config/typesafe/apikey.txt`):

```bash
DEMO_PASSWORD=hunter2 uv run jevnav run examples/local-demo/flow.yaml --report /tmp/run.md
uv run jevnav replay examples/local-demo/demo.trace.jsonl
```

## Conventions

- `SPEC.md` is canonical: trace format, fingerprints, replay semantics, gates.
  Change the spec first, then the code, then the tests.
- Replay is offline and deterministic. Nothing in the replay path may call a
  model, and nothing may resolve an action by position — always by fingerprint.
- The candidate description format is measured, not taste: the scope is added
  only where names collide, because adding it everywhere cost accuracy
  (2026-09-21, `describe()` docstring). Change wording only with numbers.
- Loop mode (`agent.py`) is not gated by a high confidence on purpose:
  measured correct loop decisions at p 0.41–0.99 and wrong ones at 0.39–0.47.
  Safety comes from the deterministic checks (role/value validation, progress
  detection, risky patterns) and from verifying `--success`. Do not "fix" the
  loop by raising `loop_min_confidence`; fix the check that is missing.
- Tests never touch the network or the real Jev endpoint: `tests/helpers.py`
  has a `FakeJev` whose transport answers from a script.
- `review` and `blocked` decisions never execute an action. Do not add a flag
  that "just acts anyway" without an explicit gate in `gates.yaml`.
- Local-first: no telemetry, no hosted service, no screenshots in the decision
  loop. Traces stay local (`*.trace.jsonl` is gitignored, example traces
  excepted).
- The candidate shortlist (`page.py`: ordering + `--max-candidates`) is a speed
  lever with a measured trade: same accuracy on the HN task at 40 vs 199
  candidates, 2.8x faster cold and 3.8x fewer tokens. If you change the ordering
  or the default cap, re-measure on a real page and write the number down.
- Side tools (screenshot, upload, drag, resize, emulate, route, trace, perf, heap,
  lighthouse) are observation/acting conveniences for the caller. They must never
  enter the decision path, the gate, or a replay, and they are not written to a
  trace. Screenshots in particular stay out of the decision loop on purpose: a
  pixel decision has no fingerprint and cannot be replayed.
- Play mode (`play.py`) is measured against `--policy random` at the same rate,
  always. Never claim a game result without the control run beside it, and never
  tune the bundled game until the agent wins: the honest number here is the
  decisions-per-second ceiling (1 / Jev latency), not the score.
- Browser modes live in `browser.py` and nowhere else: fresh, persistent profile
  (`--user-data-dir`), attached Chrome (`--cdp`). Never close a browser you
  attached to, never open a profile Chrome has locked, and never let a profile
  path or cookie reach a trace (`tests/test_browser.py`).

## Guardrails

- Verify before claiming done: lint, format check, tests, and a live smoke when
  the change touches extraction, decisions or replay.
- Never commit secrets, API keys or private traces.
- No external promotion (HN, Reddit, social) without approval. Directory
  listings and repo docs only.
