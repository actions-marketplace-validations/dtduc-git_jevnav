# Element selection on real pages — 2026-09-21

Question: when jevnav asks Jev "which element for this intent?" on a page nobody
prepared for it, how often is the answer right, and how often would a wrong
answer have run unattended?

## Method

- **Cases**: `benchmarks/element-selection/cases.json` — 30 hand-written cases
  across 8 real public sites (Hacker News front/newest, Wikipedia main page and
  an article, PyPI home and a project page, MDN, python.org, docs.python.org,
  GitHub, example.com, crates.io). Each case is one intent plus the element it
  should select.
- **Ground truth discipline**: a case is only scored when its `expect` selector
  matches **exactly one visible element** and that element is in the ranked
  shortlist the model saw. Anything else is reported as invalid, never scored —
  4 cases were invalid on this run, and two more were removed after hand review
  (see `EXCLUDED.md`, kept next to the cases).
- **One decision per case**, using the same choice question `browse` uses: the
  page's ranked shortlist as options, `none` allowed.
- Reproduce: `uv run python benchmarks/element-selection/run.py --json out.json`

## Result (model `jev-1.13.0`)

| metric | value |
|---|---|
| scored cases | 26 |
| correct | **25 (96.2%)** |
| confidence ≥ 0.9 (would run unattended) | 18 (69.2%) |
| **precision at that gate** | **18/18 (100%)** |
| wrong below the gate | 1 (`mdn-home-js`, p=0.57 → routed to review) |
| wrong above the gate | **0** |
| latency | p50 321ms, p95 346ms |
| cost | **$0.000155 per case** |
| invalid cases | 4 (ambiguous `expect` selectors, see below) |

The one miss is the interesting one: on MDN the intent was "open the JavaScript
reference" and the page shows several entries named JavaScript (main link, a
sidebar entry, a guide). Jev answered at p=0.57 — below the gate, so a run would
have stopped for a human instead of clicking. That is the property the study was
built to check: **no wrong answer was above the gate**, and the gate is the only
thing standing between a decision and an action.

## What this does not prove

- **n=26, one run, one model version.** With 25/26 the 95% confidence interval on
  accuracy is roughly 80–100%: this is a direction, not a proof of 96%.
- **Labels are mine, single-annotator.** The excluded cases are the evidence that
  hand labels are fallible; a second annotator is the honest next step.
- **Easy pages, in the sense that they are well-built**: semantic HTML, stable
  URLs, real labels. Government portals, internal CRMs and canvas-heavy apps are
  a different population, and they are where the review path matters most.
- Chromium only, one viewport, no cookie banners accepted or declined.

## Why it is still worth publishing

The claim jevnav makes is narrow and this measures exactly it: a decision at or
above the gate should be safe to run unattended. 18/18 at the gate, with the only
miss landing in review, is the first real-site evidence for that claim — and the
invalid-case log shows the failure modes honestly rather than hiding them.
