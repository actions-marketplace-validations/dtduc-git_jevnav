# jevnav

**Browser automation whose decisions you can replay, test and audit.**

[![CI](https://github.com/dtduc-git/jevnav/actions/workflows/ci.yml/badge.svg)](https://github.com/dtduc-git/jevnav/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/jevnav.svg)](https://pypi.org/project/jevnav/)
[![Python](https://img.shields.io/pypi/pyversions/jevnav.svg)](https://pypi.org/project/jevnav/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Selector-based tests break the moment a label changes, and LLM browser agents
are confident, unauditable and occasionally wrong. jevnav sits in between:

1. **Jev picks the element.** The candidate list of the current page is turned
   into a choice question; the model answers with one element and a calibrated
   probability.
2. **Every decision is recorded.** The trace holds the candidates as the model
   saw them, the choice, the probability and the cost — one JSONL file per run.
3. **Risky actions are gated.** `p` below the threshold, or an intent that looks
   destructive, goes to a human instead of clicking.
4. **`replay` is the regression test.** Offline, no model call: re-resolve every
   recorded decision against the page as it is now. A site change that breaks a
   target fails CI; everything else is reported as drift, not noise.

## Install

```bash
uv tool install jevnav          # or: pip install jevnav
playwright install chromium     # one-time browser download
```

`jevnav run` needs a TypeSafe API key (`TYPESAFE_API_KEY`, or
`~/.config/typesafe/apikey.txt`). `jevnav replay` needs none — that is the point.

## Quickstart

```yaml
# flows/acme-login/flow.yaml
id: acme-login
start: https://app.example.com/login
steps:
  - intent: "Sign in to the existing account"
    action: click
  - intent: "Type the password"
    action: fill
    value: "${ACME_PASSWORD}"   # read from the environment, never written to the trace
  - intent: "Submit the login form"
    action: click
    expect: "button[type=submit]"   # optional ground truth, used to score the run
```

```bash
jevnav run flows/acme-login/flow.yaml --report run.md
jevnav replay acme-login.trace.jsonl --report replay.md   # offline, deterministic
```

`run` walks the flow: extract candidates → ask Jev → gate → act → record.
`replay` re-checks the trace against the live site, with no model in the loop:

```
steps 3  verdicts: ok 3
```

Change `Sign in` to `Log in` on the site and the same replay reports:

```
[01] changed   Sign in to the existing account
      no element now has 'button|sign in' (was 'Sign in' / 'button')
```

Exit code 1, with the reason — that is the CI gate.

## Gates

```yaml
# flows/acme-login/gates.yaml  (optional; sane defaults apply)
min_confidence: 0.9
risky:                                  # regular expressions, matched against
  - "\\b(delete|remove|purchase|pay)\\b"   # intent + chosen element name + role
intents:
  "delete the *": { min_confidence: 0.99 }
truncated: review                       # page had more than 255 candidates
```

Three verdicts, no ambiguity:

| verdict | meaning |
|---|---|
| `auto` | confidence at or above the threshold, nothing risky — the action runs |
| `review` | a human confirms first (low `p`, risky intent, truncated candidate list) |
| `blocked` | no decision was possible (model answered `none`, or the call failed) |

## MCP

```bash
pip install "jevnav[mcp]"
jevnav mcp --start https://app.example.com --trace session.trace.jsonl
```

The agent asks for an intent (`browse("open the billing settings")`); jevnav
extracts the candidates, asks Jev, applies the gate and — only on `auto` — acts
in its own browser, returning the target, its Playwright selector and the
confidence. `review` decisions come back unexecuted with the reason attached.
The whole session is written to the same trace format, so it can be replayed
afterwards.

## How it works

- **Candidates.** Visible interactive elements, capped at 255, in-viewport
  first. Each carries role, accessible name, type, href, placeholder and a
  scope (nearest legend/heading) so three "Email" fields stay distinguishable.
- **Fingerprint.** An element's identity is `role|name` (whitespace- and
  case-normalized). Traces store the fingerprint of every candidate as it was
  shown to the model, so replay never re-derives identity with new code.
- **Decisions.** One choice question per step: the option map is the candidate
  list, plus `none`. The decision is recorded with probabilities, usage and cost.
- **Replay.** Re-extract the page, compare fingerprints. `ok` (found),
  `moved` (found elsewhere on the page), `changed` (gone), `ambiguous` (now
  duplicated), `error`. `changed`, `ambiguous` and `error` fail; `moved` and
  drift counts are reported.
- **Actions.** `click`, `fill`, `select`, `check`, `hover`, `press`, `none`.
  Replay re-runs actions only with `--execute`, and resolves them by
  fingerprint — never by position — so a shifted page cannot click the wrong
  thing.

## Measured

From the build-time spike (44 decisions: local fixtures, Hacker News, PyPI,
Wikipedia; recorded 2026-09-21):

- **44/44** decisions correct; **28/28** at `p ≥ 0.9` (the auto gate).
- Replay caught **4/4** injected DOM changes with **0** false alarms on the
  unchanged pages.
- Latency p50 **334ms**, p95 **834ms**; **$0.000053** per decision.
- Asked for an element that does not exist, Jev answered `none` at `p=1.0`
  and `p=0.92` instead of inventing one.

Small sample, self-graded ground truth, easy intents — treat these as direction,
not proof. `replay` is the number that matters in CI, and it is deterministic.

## Non-goals

- No planner and no agent loop — you (or your agent) decide *what* to do; jevnav
  decides *where* and records why.
- No screenshots in the decision loop, no text generation (`fill` takes the text
  from your flow or your environment).
- No iframes, shadow DOM, canvas or file pickers in v0.1 — long tail, tracked as
  issues rather than half-supported.
- No SaaS, no hosted runner, no telemetry. Local-first: nothing leaves the
  machine except the question sent to your configured Jev endpoint.

## Privacy

Traces contain page URLs, element names and your actions — never screenshots or
page content. Literal `value`s from the flow are recorded (they are already in
your repo); `${ENV}` values are recorded as the variable name only. Traces are
gitignored by default; audit one before sharing it.

## Suite

jevnav is the browser piece of a verification stack: [mcplint](https://github.com/dtduc-git/mcplint)
(MCP configs), [harnessguard](https://github.com/dtduc-git/harnessguard) (agent
harnesses), [jevassert](https://github.com/dtduc-git/jevassert) +
[jev-packs](https://github.com/dtduc-git/jev-packs) (calibrated decision
packs), and [jev-table](https://github.com/dtduc-git/jev-table).

## License

Apache-2.0.
