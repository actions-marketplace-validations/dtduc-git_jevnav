# Why replayable browser decisions

Two ways to automate a browser both fail in the same place.

**Selector-based tests** break the moment a label changes. They are precise,
deterministic and cheap, and they encode a decision a human already made — so
when the page moves, the test fails for a reason nobody cares about, and
somebody updates a selector.

**LLM browser agents** are confident, unauditable and occasionally wrong. They
read the page, decide, click — and the evidence of *why* lives in a context
window that is gone by the time anything goes wrong. When an agent clicks the
wrong button, the postmortem is a screenshot and a shrug.

jevnav sits in between, and the bet is narrow: **one decision at a time, with a
gate in front of it and a trace behind it.**

## The shape of a decision

1. The page is read into a ranked shortlist of visible interactive elements —
   in-viewport first, form controls before buttons before links — each with its
   role, accessible name, type and scope. Measured on Hacker News (199 elements
   → 40): same accuracy, 2.8× faster on a cold decision and 3.8× fewer input
   tokens.
2. The shortlist becomes a choice question. Jev answers with one element and a
   calibrated probability.
3. The gate decides whether that answer may run unattended: below the
   confidence bar, a risky action (nine languages of patterns), or a truncated
   page, and a human confirms first. Risky patterns are not overridable; the
   confidence bar is the caller's call.
4. The action is resolved **by fingerprint** (`role|name`), never by position,
   and the whole decision — candidates as the model saw them, choice,
   probability, cost, gate verdict — is written to a JSONL trace.

`replay` then re-resolves every recorded decision against the page as it is
now. No model call, no API key. A site change that breaks a recorded target
exits 1; everything else is reported as drift, not noise. With `--execute` it
also re-runs the actions and checks the recorded outcome selector.

That is the whole idea. The rest is discipline about it.

## Two things it is not

**Not a planner.** jevnav does not decide *what* to do — you do, or your agent
does. It decides *where* and records why. A `goal` loop exists for callers that
want it, but the contract is the single decision.

**Not a pixel tool.** No screenshots enter the decision loop, deliberately: a
pixel decision has no fingerprint and cannot be replayed. That is also why it is
not a visual-regression tool. `diff` reports structure and computed styles as
facts an agent can act on (`font-size 32px → 28px`), not as an image to eyeball.

## What is actually measured

This repository holds its own numbers, including the ones that argue against it.

| claim | number | where |
|---|---|---|
| element selection on real pages | 41/41 scored correct; 30/30 at the p≥0.9 gate | `research/browser-element-selection.md` |
| gate — risky patterns | recall **1.00**, precision **0.65** on 49 labeled intents, nine languages | `research/gate-study.md` |
| gate — confidence, hard pages | 0 of 6 wrong decisions auto; 61% of correct decisions still sent to review | `research/gate-study.md` |
| replay | deterministic; offline; exits 1 on a broken target | run it on your own traces |
| wall clock vs chrome-devtools | **chrome-devtools is 2.4× faster** on small pages | `research/driving-benchmark.md` |
| decision cost | ~$0.00004 and ~330ms per step | README |

The wall-clock row is the honest one: if speed on a small page is what you need,
use chrome-devtools. jevnav's advantage is decision cost and evidence, not
being fast — and the gate study says the gate is conservative: on a deliberately
hard page it stopped 61% of *correct* decisions too. The asymmetry is on
purpose. A missed risky action runs silently; a false review costs one
confirmation.

## Where the evidence layer earns its keep

- **CI.** A recorded run becomes a regression test that needs no API key and no
  model. The same Action that replays the committed example traces is what the
  repository's own CI runs on every push.
- **An ordinary pytest test.** The `jev` fixture ships with the package: write
  the test you would write anyway, describe targets as intents, and the trace it
  writes replays offline. `jev.expect("#welcome")` is recorded too, so the
  replay verifies the outcome rather than only re-running the clicks.
- **A coding agent working on a frontend codebase.** The facts are the door —
  `outline`, `styles`, `diff` — and the replay is what keeps the fix from
  rotting: pin the outcome (`goal(..., success=...)`) and re-check it in CI
  after every deploy.

## Non-goals

No SaaS, no hosted runner, no telemetry. Local-first: nothing leaves the machine
except the question sent to your configured Jev endpoint. No screenshots in the
decision loop, no text generation (`fill` takes its value from your flow or your
environment; `${VAR}` values are recorded as the variable name only). Traces
contain page URLs and element names — audit one before sharing it.

## Read next

- The trace format is the contract: [`SPEC.md`](../SPEC.md).
- The runnable pytest example, with a committed trace:
  [`examples/pytest-interop/`](../examples/pytest-interop/).
- The gate study: [`research/gate-study.md`](../research/gate-study.md).
- The driving benchmark, including the part where the other tool wins:
  [`research/driving-benchmark.md`](../research/driving-benchmark.md).
