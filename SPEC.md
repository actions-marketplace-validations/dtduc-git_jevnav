# jevnav trace format — spec 0

Canonical description of what `jevnav` records and what `jevnav replay`
guarantees. Everything else in the tool is replaceable; this file is the
contract.

## Records

One trace = one JSONL file. First record is the run header, then records in
order: one `step` per decision, and one `action` per acting-tool call (the
direct primitives; see "Side tools"). Unknown `kind` values are an error (a
trace is either complete or broken, never half-read).

### `run` header

| field | type | meaning |
|---|---|---|
| `kind` | `"run"` | record type |
| `spec` | int | trace format version (currently `0`) |
| `started` | ISO-8601 UTC | when the run started |
| `tool` | string | e.g. `jevnav/0.1.0` |
| `order_spec` | int | version of the shortlist ordering (`ORDER_SPEC` in `trace.py`, bumped with `page.py`'s ROLE_RANK/global_order); replay excuses a position change on an identical candidate set only across different versions |
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
| `candidates` | array | the shortlist the model saw (see below) |
| `expected_cid` | string \| null | ground truth, when the flow declared `expect`; `"none"` means "no element should match" |
| `decision` | object | `{choice, chosen_fp, chosen_name, confidence, probabilities, model, latency_ms, usage, cost_usd, error}` |
| `gate` | object | `{verdict: auto \| review \| blocked, reason}` |
| `locator` | object \| null | `{selector, unique}` — the standard Playwright locator for the choice |
| `result` | object | `{correct, executed, error}` |

### candidate

| field | type | meaning |
|---|---|---|
| `cid` | string | `f0:c1`…, frame-namespaced and unique within the step; the choice question's option keys |
| `role` | string | explicit or implicit ARIA role |
| `name` | string | accessible name, original casing, whitespace collapsed |
| `fp` | string | **fingerprint**: `role\|name` casefolded — the element's identity |
| `tag`, `type` | string \| null | DOM tag and input type |
| `href`, `placeholder` | string \| null | truncated to 120 / 60 chars |
| `scope` | string \| null | nearest legend, form name or heading (max 60 chars) |
| `value` | string \| null | current field value at extraction time (part of `dom_hash`) |
| `disabled`, `in_viewport` | bool | |
| `frame` | int | which frame the element lives in (0 is the main frame) |
| `rank` | int | position in its frame's shortlist — an ordering tiebreaker, not a DOM index and not identity |

**How a candidate is described to the model** (option text):
`Name — role → href`, plus `in 'scope'` only when another candidate on the page
shares the same name, plus `[disabled]`. Measured 2026-09-21: adding the scope
to every line cost accuracy (a fieldset legend "Sign in" reads like the action
"Sign in"; p 0.85 → 0.47 on that intent), while adding it only for duplicate
names rescued the duplicate cases and changed nothing on pages with unique
names.

## What the model sees

`candidates` is a **shortlist**, not every element on the page. Ordering is
deterministic: in-viewport first, then form controls (textbox, searchbox,
combobox, checkbox, radio, switch), then buttons/tabs/menuitems, then links,
then DOM order. Every frame is read (main frame first) and open shadow roots are pierced: a
control inside a payment iframe or a web component is a candidate like any
other. Cids are frame-namespaced (`f1:c7`), the stamps in the DOM carry the same
name, and acting resolves by fingerprint **within that frame**.

The list is capped **globally, across every frame together**:
`min(--max-candidates, 254)`, because the API allows 255 choices per question and
`none` takes one. Ordering is in-viewport first, then form controls, then links,
then frame index, then DOM order — so the cap keeps the most actionable elements
of the whole page, not everything from the first frame. `dropped` counts
everything the model did not see, across frames (truncation is a warning by
default, `truncated: review` to gate on it).

Measured 2026-09-21 on Hacker News (199 candidates): the same decisions at 40
candidates cost 1815 input tokens and ~300ms instead of 6973 tokens and ~350ms
warm / 842ms cold.

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
   - fingerprint matches one element at a different position → `moved`, unless
     the candidate set is identical **and** the trace's `run.order_spec` differs
     from the running `ORDER_SPEC`: different ordering logic may re-rank the
     shortlist, while the same ordering and the same set mean the page itself
     reordered. Traces without the field fall back to their `tool` version, and
     an unknown ordering never excuses movement
   - fingerprint matches one element at the recorded position → `ok`
   - `choice` is `none`/null → `ok` (nothing to resolve), with a reason
   - navigation or extraction failure → `error`
4. Drift is always reported: how many fingerprints appeared (`drift.new`) and
   disappeared (`drift.missing`), and whether the candidate set is identical
   (`page_identical`).

`changed`, `ambiguous` and `error` fail the replay (exit code 1). `moved` and
drift do not fail: cosmetic churn is reported, not punished. A name that changes
every deploy (a counter, an unread badge) is still a `changed`; `--normalize
REGEX` relaxes the comparison on both sides for those, opt-in and recorded in
the replay result.

With `--execute`, recorded actions are re-run after a successful resolution.
Actions are resolved by **fingerprint**, never by position, and an action whose
value came from `${ENV}` reads the variable from the environment at replay time.
If the fingerprint is gone or duplicated, replay refuses to act.

### `action` record

One per acting-tool call that does not go through a decision (`goto`,
`fill_form` with a selector, `press_key`, `upload_files`, `route`, `read_js`,
the artifact writers, …). It is evidence, not a decision: no candidates, no
model, no gate, and `replay` skips it (see `read_actions` in `trace.py`).

| field | type | meaning |
|---|---|---|
| `kind` | `"action"` | record type |
| `at` | ISO-8601 UTC | when the call was recorded |
| `tool` | string | MCP tool name |
| `request` | object | the arguments, with typed values and JS expressions masked (`"<N chars>"`) |
| `result` | object | `{"ok": true}`, or `{"error": "Type: message"}` |

## Side tools

`console`, `network`, `dialogs`, `read_js`, `outline`, `styles`, `wait_for`, `scroll`, the tab tools
(`tabs`, `new_page`, `select_page`, `close_page`), `screenshot`, `upload_files`,
`drag`, `resize`, `emulate`, `route`/`unroute`, `trace_start`/`trace_stop`,
`perf_metrics`, `heap_snapshot` and `lighthouse` are the agent's eyes and hands
around the decision loop. The **observation** ones (`console`, `network`,
`dialogs`, `outline`, `styles`, `perf_metrics`, `wait_for`, `tabs`) participate
in nothing and are not written to the trace. The **acting** ones run immediately
and are recorded as `action` records; they are not part of a trace's replay
path, because replay re-resolves decisions. Dialogs are the exception worth knowing: the sync API must answer a dialog
inside its handler (parking one deadlocks the page — measured), so `dialog_policy`
sets the answer in advance — a session default, or rules matched against the
dialog's message text — and every dialog is recorded with the rule that fired.

## Browser modes

A trace is independent of how the browser was obtained: fresh headless
Chromium, a persistent profile (`--user-data-dir`) or an attached Chrome
(`--cdp`). Nothing about the profile — its path, its cookies, its extensions —
is written to a trace, and the `run` header carries no browser-mode field.

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

The caller may lower the confidence bar (`browse(min_confidence=…)`,
`go --min-confidence`), never below `MIN_CONFIDENCE_FLOOR` (0.3): an LLM passing
zero would otherwise turn the gate into "no risky pattern matched, so run". The
risk patterns ship in English, Vietnamese, German, French, Spanish, Portuguese,
Japanese, Chinese and Korean; extend them per flow in `gates.yaml`.

Acting resolves by **fingerprint**, never by position: `execute` re-extracts and
requires exactly one candidate with the chosen fingerprint, and `[data-jevcid]`
stamps are cleared before every extraction (a stale stamp used to make a
position-based lookup click a different element silently).

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

## Diff (`jevnav diff a b`)

Comparison only — no model, no repository writes. Both pages are opened in one
browser session and snapshotted with the same extractors as the `outline` and
`styles` tools.

- Structure is matched by `tag|label`, where the label is the element's own
  text, falling back to its aggregated text only when it has no descendants we
  would report separately (so a container is not "changed" when a child goes
  away). Differences are `missing`, `new` or `moved` (box delta beyond
  `--tolerance`, default 4px).
- Styles are compared element-by-element for `--style-selector`, property by
  property; every `px` number is rounded, so fractional layout noise does not
  read as a change.
- Exit code 0 when identical, 1 when anything differs — usable as "the app must
  still match the design" in CI.

## Play mode (`jevnav play`)

The Doom shape: the caller supplies a JS state probe and an action set; there is
no candidate extraction. One request per tick, and movement keys stay held
between decisions.

| field | meaning |
|---|---|
| `state` | the probe's output, whitespace-collapsed, capped at 1200 characters |
| `state_hash` | hash of that state text, so a run can be compared tick by tick |
| `decision` | as usual (`choice`, `confidence`, `probabilities`, `model`, `latency_ms`, `usage`, `cost_usd`, `error`) |
| `action` | `{type: "hold", key}` — the key held from this tick on |
| `gate` | always `{verdict: "n/a"}`: a game action has no blast radius, and a review queue at 5Hz is a queue nobody reads |
| `elapsed_ms` | how long the tick took (decision + action) |

The `run` header carries `goal`, `actions`, `rate_hz`, `seconds` and `policy`.
A play trace is evidence, not a fingerprint replay: a live game state is not
reproducible, so `replay` on it re-resolves nothing. `--policy random` runs the
identical loop with no model call, which is the control run for any claim about
the agent's play.
