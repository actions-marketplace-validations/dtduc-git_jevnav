# jevnav trace format — spec 0

Canonical description of what `jevnav` records and what `jevnav replay`
guarantees. Everything else in the tool is replaceable; this file is the
contract.

## Records

One trace = one JSONL file. First record is the run header, then one record per
decision, in order. Unknown `kind` values are an error (a trace is either
complete or broken, never half-read).

### `run` header

| field | type | meaning |
|---|---|---|
| `kind` | `"run"` | record type |
| `spec` | int | trace format version (currently `0`) |
| `started` | ISO-8601 UTC | when the run started |
| `tool` | string | e.g. `jevnav/0.1.0` |
| `flow` | string | flow id, or `mcp-session` |
| `model` | string | requested model (`jev-latest`, …) |
| `start` | string | start URL (flows only) |

### `step` record

| field | type | meaning |
|---|---|---|
| `kind` | `"step"` | record type |
| `step` | int | 1-based position in the flow |
| `intent` | string | what the agent wanted, verbatim |
| `action` | object | `{type}` plus `value` (literal from the flow), `value_from_env` (variable name), or `key` |
| `url`, `title` | string | page state the decision was made on |
| `total_on_page` | int | visible interactive elements found |
| `dropped` | int | candidates not shown to the model (cap is 255) |
| `dom_hash` | string | hash of the candidate set as the model sees it (identity + current value), not of the page bytes |
| `candidates` | array | see below |
| `expected_cid` | string \| null | ground truth, when the flow declared `expect`; `"none"` means "no element should match" |
| `decision` | object | `{choice, chosen_fp, chosen_name, confidence, probabilities, model, latency_ms, usage, cost_usd, error}` |
| `gate` | object | `{verdict: auto \| review \| blocked, reason}` |
| `locator` | object \| null | `{selector, unique}` — the standard Playwright locator for the choice |
| `result` | object | `{correct, executed, error}` |

### candidate

| field | type | meaning |
|---|---|---|
| `cid` | string | `c1`…, unique within the step; the choice question's option keys |
| `role` | string | explicit or implicit ARIA role |
| `name` | string | accessible name, original casing, whitespace collapsed |
| `fp` | string | **fingerprint**: `role\|name` casefolded — the element's identity |
| `tag`, `type` | string \| null | DOM tag and input type |
| `href`, `placeholder` | string \| null | truncated to 120 / 60 chars |
| `scope` | string \| null | nearest legend, form name or heading (max 60 chars) |
| `disabled`, `in_viewport` | bool | |

**How a candidate is described to the model** (option text):
`Name — role → href`, plus `in 'scope'` only when another candidate on the page
shares the same name, plus `[disabled]`. Measured 2026-09-21: adding the scope
to every line cost accuracy (a fieldset legend "Sign in" reads like the action
"Sign in"; p 0.85 → 0.47 on that intent), while adding it only for duplicate
names rescued the duplicate cases and changed nothing on pages with unique
names.

## Fingerprints

`fp = casefold(collapse_whitespace(role)) + "|" + casefold(collapse_whitespace(name))`.

The fingerprint is **semantic**, not positional: moving an element, renaming an
`id`, or restyling it does not change it; changing its accessible name or role
does. It is deliberately the same identity a human writes by hand
(`get_by_role("button", name="Sign in")`), which is why `locator` is derivable
from it.

Traces store each candidate's fingerprint **as the model saw it**. Replay
compares recorded fingerprints against freshly extracted ones and never
re-derives identity with newer code — old traces stay valid when extraction
improves.

## Replay semantics

For each step, in order:

1. Navigate to the recorded `url` **only if the browser is not already there**
   (a flow's steps share a page state; re-navigating would destroy it).
2. Extract the current candidates.
3. Resolve the recorded `decision.choice`:
   - fingerprint absent → `changed`
   - fingerprint matches more than one element → `ambiguous`
   - fingerprint matches one element at a different position → `moved`
   - fingerprint matches one element at the recorded position → `ok`
   - `choice` is `none`/null → `ok` (nothing to resolve), with a reason
   - navigation or extraction failure → `error`
4. Drift is always reported: how many fingerprints appeared (`drift.new`) and
   disappeared (`drift.missing`), and whether the candidate set is identical
   (`page_identical`).

`changed`, `ambiguous` and `error` fail the replay (exit code 1). `moved` and
drift do not fail: cosmetic churn is reported, not punished.

With `--execute`, recorded actions are re-run after a successful resolution.
Actions are resolved by **fingerprint**, never by position, and an action whose
value came from `${ENV}` reads the variable from the environment at replay time.
If the fingerprint is gone or duplicated, replay refuses to act.

## Gates

`gates.yaml` (optional, per flow) decides whether a decision may be acted on
unattended:

| key | default | meaning |
|---|---|---|
| `min_confidence` | `0.9` | below this → `review` |
| `risky` | built-in list | regular expressions matched against `intent + chosen name + role` → `review` |
| `intents` | `{}` | fnmatch pattern → `{min_confidence}` overrides; the longest matching pattern wins |
| `truncated` | `warn` | `dropped > 0`: `warn` records it, `review` gates on it |

Verdicts: `auto` (act), `review` (a human confirms first), `blocked` (no
decision was possible: `none`, or the model call failed). `blocked` and
`review` never execute an action.

## Goal loop (`jevnav go`, MCP `goal`)

One request per step answers four questions together:

| question | type | meaning |
|---|---|---|
| `status` | choice | `in_progress` \| `done` \| `stuck` |
| `action` | choice | `click` \| `fill` \| `select` \| `check` \| `hover` \| `press` |
| `target` | choice | the candidate list (option keys are `cid`s) plus `none` |
| `value_key` | choice | a context key, or `none` (only asked when the goal has context) |

The state block carries the goal, the step number, the context *keys* (never
values), the current page (title, URL), a digest of the visible text (at most
700 characters) and the numbered history of steps with their effect on the page.

Step records in a loop trace use the same shape as a flow trace, plus:

| field | meaning |
|---|---|
| `decision.status`, `decision.status_confidence` | the model's view of the goal |
| `decision.action`, `decision.action_confidence` | the action it chose |
| `decision.value_key` | the context key it chose, if any |
| `value_source` | `model` (it picked the key) or `name-match` (the field name matched a key) |
| `gate.verdict` | as in a flow, plus `n/a` for the step that stopped the loop (`done`) |
| `verify` | on the step where the model said `done`: `{selector, verified}` — the claim checked against the page, recorded whether it passed, failed or was not attempted (`null`) |

The `run` header of a `jevnav go` trace also carries `goal` and `success`.
`replay --execute` verifies whichever of the two it finds: the header's
`success`, or the last step's `verify.selector` (MCP sessions have per-goal
verification in the step, since one session may run several goals).

Loop rules, all deterministic:

- `fill` on a role that cannot take text → `blocked`, nothing runs.
- `fill`/`select` with no context value for the field → `blocked`.
- The same page state twice in a row → `no_progress`, the run stops.
- `--max-steps` (default 8) → `max_steps`.
- `done` stops the run; the claim is verified against `--success` when given:
  `verified: true` (the selector is visible), `verified: false` → the run's
  status becomes `unverified` and the command fails, or `verified: null` when no
  selector was given (reported, not hidden).
- The loop's gate uses `loop_min_confidence` (default 0.5), not
  `min_confidence`: loop decisions carry a lower calibrated p by construction
  (measured: correct decisions at p 0.41–0.99, wrong at 0.39–0.47).
- Candidate lists are capped at **254** (255 choices minus the `none` option),
  in-viewport first, and the step records how many were dropped. Truncation is
  a warning by default (`truncated: review` gates on it): real pages exceed the
  cap routinely — Wikipedia's main page, measured — and blocking them would
  stop most of the internet.

`replay --execute` re-runs a loop trace and re-checks the recorded `success`
selector, so an agent run becomes a deterministic CI test.
