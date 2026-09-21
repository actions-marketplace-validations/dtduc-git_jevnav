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
- Tests never touch the network or the real Jev endpoint: `tests/helpers.py`
  has a `FakeJev` whose transport answers from a script.
- `review` and `blocked` decisions never execute an action. Do not add a flag
  that "just acts anyway" without an explicit gate in `gates.yaml`.
- Local-first: no telemetry, no hosted service, no screenshots in the decision
  loop. Traces stay local (`*.trace.jsonl` is gitignored, example traces
  excepted).

## Guardrails

- Verify before claiming done: lint, format check, tests, and a live smoke when
  the change touches extraction, decisions or replay.
- Never commit secrets, API keys or private traces.
- No external promotion (HN, Reddit, social) without approval. Directory
  listings and repo docs only.
