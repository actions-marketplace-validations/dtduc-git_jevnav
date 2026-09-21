# pytest interop — intents in an ordinary test

An ordinary Playwright test whose targets are intents. The `jev` fixture ships
with the package (a pytest plugin entry point), so there is no import and no
conftest — `pip install jevnav` is the whole setup.

```bash
pip install jevnav && playwright install chromium   # or: uv tool install jevnav
export TYPESAFE_API_KEY=...                          # or ~/.config/typesafe/apikey.txt
DEMO_PASSWORD=hunter2 pytest --jev-trace-dir=.
```

Every action is a Jev decision, gated the same way `jevnav go` gates its own: a
`review` or `blocked` verdict fails the test before anything runs.

The trace is the regression test. Commit it and replay it anywhere — offline, no
model call, no API key:

```bash
DEMO_PASSWORD=hunter2 jevnav replay --execute test_sign_in.trace.jsonl
```

```
- success check: **verified** (`#welcome` is visible)
steps 4  verdicts: ok 4
```

`jev.expect("#welcome")` is recorded on the trace, so `replay --execute` checks
the outcome too: if the banner stops appearing, the replay fails even when every
target still resolves. `DEMO_PASSWORD` is recorded as the variable name only —
the literal never lands in the committed file.

The fixture in full: `goto`, `click`, `fill`, `select`, `check`, `press` (intent
first), `expect` (a selector, asserted against the page and recorded), `page`
(the real Playwright page), `trace_path()`. Options: `--jev-trace-dir`,
`--jev-gates`, `--jev-headed`.

In CI, one step over the traces directory:

```yaml
- run: for trace in *.trace.jsonl; do uvx jevnav replay --execute "$trace"; done
  env:
    DEMO_PASSWORD: ${{ secrets.DEMO_PASSWORD }}
```
