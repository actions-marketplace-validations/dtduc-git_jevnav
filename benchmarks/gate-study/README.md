# The gate, measured

```bash
uv run python benchmarks/gate-study/run.py                  # part A, offline, free
uv run python benchmarks/gate-study/run.py --live --runs 3  # both, ~$0.005
```

Two measurements of the same gate:

- **Part A** — `intents.json`: 49 hand-labeled intents (28 risky, 21 benign
  lookalikes) across nine languages, classified by the shipped risky patterns.
  Precision and recall against the labels, with every mistake named. Labels were
  written before running the classifier.
- **Part B** — `pages/ambiguous.html`: near-duplicate controls (two "Save",
  three "Edit", an icon-only button, benign buttons the word list flags) decided
  `--runs` times each through the same machinery `browse` uses, then classified
  by the real `verdict()`. Reported: accuracy, verdict distribution, what
  happened to every wrong decision, and the two costs — auto precision and the
  false review rate.

Part A is deterministic; part B needs a TypeSafe key and is not. Results are
written to `results-<date>.json`. The published study is
`research/gate-study.md`.
