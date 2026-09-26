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
- `diff` reports; it never decides. Keep it that way: no model calls, no edits to
  a repository, no pixel comparison. If a difference needs a judgement call, that
  belongs to the caller.
- Dialogs cannot be parked for a human: Playwright's sync API must answer inside
  the handler, and a parked dialog blocks the renderer (measured: the next call
  never returned). They are answered by `dialog_policy` rules set in advance and
  always recorded. Do not reintroduce a "park and ask" path.
- Identity is the fingerprint everywhere: decisions, traces, replay *and*
  execution. `execute` takes a candidate and resolves by fingerprint with a
  uniqueness check; never reintroduce a position-based lookup (a stale
  `data-jevcid` once made a click land on a different element, silently).
- Side tools (screenshot, upload, drag, resize, emulate, route, trace, perf, heap,
  lighthouse) are observation/acting conveniences for the caller. They must never
  enter the decision path or a replay. Since 0.2.0 every *acting* call is written
  to the trace as a `kind: "action"` record (values masked; `replay` skips it —
  SPEC § `action` record); observation tools are not traced. Only the `intent`
  variants of `fill_form`/`upload_files` go through the gate, because there Jev
  picks the element. Screenshots in particular stay out of the decision loop on
  purpose: a pixel decision has no fingerprint and cannot be replayed.
- Play mode (`play.py`) is measured against `--policy random` at the same rate,
  always. Never claim a game result without the control run beside it, and never
  tune the bundled game until the agent wins: the honest number here is the
  decisions-per-second ceiling (1 / Jev latency), not the score.
- Browser modes live in `browser.py` and nowhere else: fresh, persistent profile
  (`--user-data-dir`), attached Chrome (`--cdp`). Never close a browser you
  attached to, never open a profile Chrome has locked, and never let a profile
  path or cookie reach a trace (`tests/test_browser.py`).
- Every command takes the same browser flags (`add_browser_flags` in `cli.py`,
  `mcp` included): engine, profile, CDP, locale, timezone, user agent. A flag
  that would change what runs but cannot apply is refused, not ignored: `--cdp`
  with another engine, a profile, or context options stops the command at
  startup (`cdp_conflict`; `browser.py` checks again for API callers), and the
  CDP-only MCP tools refuse firefox/webkit. Chromium launch flags go to chromium
  only — WebKit's Linux MiniBrowser exits on an option it does not know.
- CDP emulation belongs to the session that set it: detaching the session resets
  it. Throttling keeps one attached session per tab (`_throttle_session`); a
  "set, then detach" CDP call is a silent no-op for anything stateful. A tab's
  network override also beats the context's `set_offline`, so every offline
  change is re-sent to the throttled tabs (`_sync_network_overrides`).
- MCP tools raise through the `tool()` wrapper in `serve()`: SDK 2.x sends only
  "Error executing tool X" for an ordinary exception, so errors are re-raised as
  the SDK's `ToolError` to keep the reason visible to the agent.

## Diagrams

`docs/architecture.html` (the system), `docs/seq-chrome-devtools.html` and
`docs/seq-jevnav.html` (the two loops), each built from a committed archify spec
(`*.sequence.json`, `architecture.archify.json`). Regenerate with the archify
skill's `deliver` (validate first: 0 errors, 9/9 checks) and re-run
`visual-check`; never hand-edit the HTML.

## Guardrails

- Verify before claiming done: lint, format check, tests, and a live smoke when
  the change touches extraction, decisions or replay.
- Never commit secrets, API keys or private traces.
- No external promotion (HN, Reddit, social) without approval. Directory
  listings and repo docs only.
