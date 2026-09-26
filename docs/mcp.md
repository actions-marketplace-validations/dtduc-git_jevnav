# jevnav as an MCP server

jevnav runs its own browser and exposes it as an MCP server, so a coding agent
(Claude Code, Codex, Cursor, anything that speaks MCP over stdio) can drive a
page through Jev decisions instead of writing selectors:

```bash
jevnav mcp          # that is the whole setup: no URL, no trace path, no flags
```

No URL is needed: the agent opens pages itself with `goto(url)`, so **one server
serves every domain** — a session can visit several sites, in several tabs. The
session writes `jevnav-session.trace.jsonl` in the client's working directory by
default (previous sessions are archived beside it, `--no-trace` opts out), so
every session is auditable without configuring anything.

`--start <url>` exists only as a convenience for a project-scoped config that
always begins on one page; put it in that project's config, not in your global
one. Same for the per-install choices: `--browser` (chromium, firefox or
webkit), `--user-data-dir` (log in to any number of sites once, in one profile),
`--cdp`, `--locale`, `--timezone`, `--user-agent`. The CDP-based tools
(`perf_metrics`, `heap_snapshot`, CPU/network throttling in `emulate`) need
chromium and say so on firefox/webkit.

Tools:

**Deciding** — the part no other browser MCP has:

| tool | what it does |
|---|---|
| `browse(intent, action, value, min_confidence)` | one step: Jev picks the element, the gate decides, and only `auto` acts |
| `goal(goal, context_json, max_steps, success)` | drive the whole way: "sign in and open billing" — `success` verifies the outcome; returns `done` / `stuck` / `review` plus the verification |
| `goto(url)` | open a page |
| `page_state()` | URL, title and the shortlist jevnav can see |
| `summary()` | this session: steps, auto/review/blocked, cost, latency |

**Acting** — runs immediately; the MCP annotations tell a host which of these
change state, and every call lands in the session trace:

| tool | what it does |
|---|---|
| `screenshot(path, full_page, selector)` | save a PNG for a human (never used by a decision) |
| `upload_files(paths, selector, intent)` | set files, on a selector or an input Jev picks (paths must be inside `--file-root`) |
| `drag(source_selector, target_selector)` | drag one element onto another |
| `resize(width, height)` | change the viewport |
| `emulate(color_scheme, media, geolocation, offline, cpu_throttle, network_conditions, …)` | emulate media, location, connectivity and a slow device (throttling: chromium) |
| `press_key(key, selector)` | a key or combination ("Control+A"), optionally on an element |
| `fill_form(fields_json)` | fill several fields in one call: {selector\|intent, value, action} — an `intent` resolves through the gate |
| `read_js(expression)` | evaluate JS in the page — arbitrary JavaScript, `--no-eval` disables it |
| `route(pattern, status, body, abort)` / `unroute(pattern)` | stub or block requests (testing) |
| `dialog_policy(action, match)` | answer future dialogs: the default, or rules by message text |
| `trace_start(screenshots)` / `trace_stop(path)` | a Playwright trace zip for `playwright show-trace` |
| `heap_snapshot(path)` | a Chromium heap snapshot (debug artifact) |
| `lighthouse(url, categories)` | Lighthouse scores, through a pinned npx version (needs node) |
| `scroll(direction, amount)` | scroll the document |
| `new_page(url)`, `select_page(i)`, `close_page(i)` | work with tabs |

The gate covers `browse` and `goal` — the calls where Jev chooses the action —
plus the `intent` variants of `fill_form` and `upload_files`. Everything else
runs immediately: a host that wants a human in the loop for `press_key`,
`fill_form`, `upload_files`, `read_js` or `route` enforces the MCP annotations
(`destructiveHint`, `openWorldHint`) on its side. Three launch flags set the
boundary: `--no-eval` disables `read_js`, `--file-root` limits where uploads read
and artifacts write (default: the working directory), and `goto`/`new_page` accept
http(s) unless `--allow-file-urls` is passed.

**Inspecting** — the agent's eyes (observation only; readers are not traced):

| tool | what it does |
|---|---|
| `console(limit, only_errors)` | recent console messages and page errors |
| `network(limit, only_failed)` | recent requests, with statuses |
| `network_detail(id, url_contains)` | one request's headers and body (id comes from `network`) |
| `dialogs()` | alert/confirm/prompt, with the policy or rule that resolved them |
| `outline(selector, limit)` | a page or region's structure (tags, headings, text, boxes) |
| `styles(selector, props, limit)` | computed styles of the matching elements |
| `perf_metrics()` | Chromium performance counters (CDP) |
| `wait_for(text, selector, timeout_ms)` | wait for something to appear |
| `tabs()` | list the open pages and which one jevnav is driving |

Wire it into a client (this JSON shape is what Cursor, Claude Desktop and VS
Code use; Claude Code also accepts
`claude mcp add --scope user jevnav -- uvx jevnav mcp`):

```json
{
  "mcpServers": {
    "jevnav": {
      "command": "uvx",
      "args": ["jevnav", "mcp"],
      "env": { "TYPESAFE_API_KEY": "..." }
    }
  }
}
```

Why an agent would: it does not need its own Playwright MCP, `browse`/`goal`
cannot click a `Delete` by accident (`review` never executes; risky-action
patterns ship for nine languages, and extend them in `gates.yaml`), every
decision is replayable with `jevnav replay --execute`, and every acting call is
evidence in the same trace.
Cost is about **$0.00004 and 330ms per step**; `page_state` and `goto` are free.
Every tool declares its MCP annotations — read-only, destructive, idempotent,
open-world — so a client can see which calls change state before making them.

## Dialogs: answered by rule, not parked

Playwright's sync API must answer a dialog inside its handler. Parking one so a
human can decide later blocks the renderer and the next call never returns
(measured on this codebase, then removed). So jevnav answers from a policy you
set in advance — `dialog_policy("accept", match="delete")` — and records every
dialog with the rule that fired, so the run stays auditable.

## Which one to reach for

| | Playwright (library) | chrome-devtools-mcp | jevnav |
|---|---|---|---|
| who picks the element | a human writes selectors | the LLM, from a snapshot | **Jev**, with a calibrated probability |
| scope | the full test-authoring API | 29 tools, primitives + profiling | 33 tools, intent-level acting + observation |
| risky actions | whatever the test says | whatever the LLM says | **never executed by `browse`/`goal`** until a human says so (risky patterns cover nine languages); direct primitives run immediately and are annotated |
| regression evidence | trace viewer, re-run the test | none | **decision trace + offline replay that exits 1** |
| outcome assertion | `expect(...)` | none | `--success` selector, verified or reported unverified |
| engines | chromium, firefox, webkit | chromium | chromium, firefox, webkit (`--browser`) |
| CPU throttling / Slow-3G | ✅ | ✅ | ✅ (chromium, CDP) |
| request headers/body | ✅ | ✅ | ✅ `network_detail` |
| multi-field form fill | ✅ | ✅ `fill_form` | ✅ `fill_form` (selector or intent) |
| key combos | ✅ | ✅ `press_key` | ✅ `press_key` |
| per-step cost | 0 | one LLM turn per step (~38k chars of snapshot) | **$0.00004** |

This is not a replacement argument: the three do different jobs, and running
more than one costs a line of config (jevnav's browser starts in ~20ms and is
lazy, so a second server is close to free). Playwright is the library you write
a test suite with — jevnav is built on it. chrome-devtools is what you reach for
to debug a page: screenshots, console, network, performance, all the raw detail
in the model's context, which is exactly right for debugging and exactly wrong
for driving. jevnav is the decision + evidence layer: an intent in, a gated
action out, a trace that replays offline. Reach for it when the same flow has to
keep working, and for chrome-devtools when you need to find out why it stopped.

## When the gate says `review`

`review` is the tool refusing to guess — and it hands back what you need to
resolve it: the runners-up with their probabilities and a hint. Measured on
Wikipedia's main page (254 candidates):

1. `goal("search Wikipedia for ...")` → `review` at p=0.33 — the page has two
   plausible ways to submit, so nothing was clicked.
2. The caller re-reads the page and calls `browse("Click the Search button that
   submits the search form in the site header", "click", min_confidence=0.8)`
   → **auto at p=0.95**, clicked for real.
3. `goal(...)` again → `done`, **verified: true** against `.mw-search-results`.

So: be specific, and if you know the page better than the model does, set your
own `min_confidence` — the confidence bar is the caller's call. Risky patterns,
role/value validation and the outcome check are not overridable.

The stdio path is tested end-to-end in CI: a real MCP client connects to a
`jevnav mcp` subprocess, lists the tools, calls `goal` and checks the browser
acted (`tests/test_mcp_server.py`, no network, fake Jev endpoint).
