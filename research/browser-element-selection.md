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

## Result (model `jev-1.13.0`, second pass)

| metric | value |
|---|---|
| cases written | 71 |
| scored (label survived the visible-unique check) | **41** |
| correct | **41 (100%)** |
| confidence ≥ 0.9 (would run unattended) | 30 (73%) |
| **precision at that gate** | **30/30 (100%)** |
| wrong | **0** (the earlier `mdn-home-js` miss answered correctly this run) |
| latency | p50 365ms, p95 794ms |
| cost | **$0.000153 per case** |
| invalid cases | **30** — my labels, not the tool |

The invalid rate is the finding worth keeping: 30 of 71 `expect` selectors matched
zero or several *visible* elements when the harness checked them (a footer and a
nav link with the same `href`, a selector that only exists behind a collapsed
menu, a page whose markup differs from memory). The harness refuses to score
those rather than guessing, which is why the accuracy above is over 41 cases and
not 71. Writing labels that survive requires checking them against the live page
while writing — the next pass does exactly that, and a second annotator then
adjudicates the ones the two of us label differently.

Earlier pass: 26 scored, 25 correct, 18/18 at the gate, one miss at p=0.57
(`mdn-home-js`) that went to review.

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
