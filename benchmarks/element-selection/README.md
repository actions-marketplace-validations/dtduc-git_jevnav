# Element selection on real pages

```bash
uv run python benchmarks/element-selection/run.py --json out.json     # all cases
uv run python benchmarks/element-selection/run.py --only hn --limit 3 # a slice
```

`cases.json` is the case set: one intent, one `expect` selector, one page. The
runner opens each page, turns the intent into the same choice question `browse`
uses (the ranked shortlist as options, `none` allowed), compares the answer with
the element the selector points at, and prints accuracy, precision at the p>=0.9
gate, review rate, latency and cost.

Ground truth only counts when `expect` matches exactly one *visible* element and
that element is in the shortlist; everything else is reported as invalid rather
than scored. `EXCLUDED.md` records the cases removed after hand review and why.

The published study (26 scored cases, 25 correct, 18/18 at the gate) is
`research/browser-element-selection.md`.
