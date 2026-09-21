# Driving benchmark — 2026-09-21

Question: on a real *driving* task (log in, fill a form, use a table, read an
iframe), what does an agent actually get from jevnav versus chrome-devtools-mcp
when both are driven by the same LLM?

## Method

- **App**: `benchmarks/driving/app/` — a local, deterministic ops console with a
  sign-in form, a task table with row actions, a **create-task form inside an
  open shadow root**, and an **invoice inside an iframe**.
- **Tasks** (`login`, `create`, `payment`, `archive`): each ends in a string the
  agent must report (`ANSWER=…`), so success is checked by the harness, not by
  the agent's self-assessment.
- **Both servers, one prompt difference**: the same `opencode run` prompt with
  the tool namespace forced to `jevnav_*` or `chrome-devtools_*`. Same model
  (`deepseek-v4.1-flash`, the cheap default), same app, n=2 runs per task per
  server.
- Reproduce: `uv run python benchmarks/driving/run.py --runs 2`

## Result (normalized answer match)

| task | jevnav | chrome-devtools-mcp | jevnav wall | cdt wall | jevnav calls | cdt calls |
|---|---|---|---|---|---|---|
| login | 2/2 | 2/2 | 19.1s | 16.5s | 4.5 | 5.0 |
| create (shadow DOM) | **2/2** | 0/2 | 78.1s | 17.0s | 25.5 | 5.5 |
| payment (iframe) | 2/2 | 2/2 | 46.4s | 18.8s | 10.5 | 5.0 |
| archive (table) | 2/2 | 2/2 | 39.0s | 25.0s | 9.0 | 9.0 |
| **total** | **8/8** | 6/8 | 45.6s mean | **19.3s mean** | 12.4 | 6.1 |

## What the numbers actually say

1. **Both can drive this app.** The two `create` failures for chrome-devtools
   were flakiness, not a capability gap: a manual rerun of the same task with
   the same prompt finished in 6 calls with the correct answer
   (`Created task Audit (high, notified)`) — filling straight through the shadow
   root. The failing runs ended with the model echoing the prompt's placeholder
   instead of finishing, which is a cheap-model behaviour, not a tool limit.
2. **chrome-devtools is ~2.4x faster end-to-end on this app** (19.3s vs 45.6s mean)
   and needs fewer calls (6.1 vs 12.4). That is the honest correction to the
   earlier framing: jevnav's win is *decision* latency (~330ms per Jev request vs
   a full LLM turn) and cost, **not** whole-task wall clock when the LLM is fast
   and the page is small. jevnav's loop asks four questions per step and the
   orchestrating agent spends calls on extraction and status, which shows up
   here (25.5 calls for one form).
3. **The benchmark does not measure what jevnav is for.** Nothing here exercises
   the gate, `--success` verification, the decision trace, or replay — the
   properties that justify jevnav exist. A task set that *does* would look like:
   the same run repeated in CI after a deploy, with a risky step in the middle.
4. Grader note, kept because it is a real finding: the first pass graded answers
   exactly and marked `128.50 USD` as a failure against `128.50`. Agents wrap
   answers; the harness now normalizes. Any "success rate" for LLM agents that
   does not say how the answer was match is not a number.

## Caveats

- n=2 per cell, one cheap model, one local app, prompts written by me.
- The forced-namespace prompt is a proxy for "an agent that has both servers
  installed and picks one"; a real agent with both may mix them.
- Wall clock includes opencode's own startup (~2-3s), which is why the login
  deltas (2.6s) are inside the noise.
