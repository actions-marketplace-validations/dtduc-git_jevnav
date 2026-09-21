# local-demo

A six-step flow over a local fixture, with a committed trace — so you can try
the interesting half of jevnav (replay) with no API key and no network.

```bash
jevnav replay examples/local-demo/demo.trace.jsonl
```

```
- steps: 6
- verdicts: ok **6**
```

All six recorded decisions still resolve — including the one recorded as
`none` ("delete the whole account" does not exist here, and replay confirms it
still does not). Recording the trace needed a Jev key and a browser; replaying
it needs neither.

Now break the page and replay again:

```bash
sed -i '' 's/>Sign in</>Log in</' examples/local-demo/app.html   # macOS; drop the '' on Linux
jevnav replay examples/local-demo/demo.trace.jsonl
```

```
- failing steps: [1, 4]
| 1 | Sign in to the existing account | changed | no element now has 'button|sign in' ... |
| 4 | Submit the login form | changed | no element now has 'button|sign in' ... |
```

Exit code 1, one failing step, with the reason. That is what a CI gate looks
like. Restore the file with `git checkout examples/local-demo/app.html`.

## Recording the flow yourself

```bash
export DEMO_PASSWORD=hunter2          # the trace records the variable name, never the value
jevnav run examples/local-demo/flow.yaml --report run.md
```

The run shows all three gate verdicts:

| step | verdict | why |
|---|---|---|
| 1–4 | `auto` | confident, harmless |
| 5 | `review` | "delete the task…" matches the risky patterns — nothing is clicked |
| 6 | `blocked` | Jev answers `none` at `p=1.0` for "delete the whole account", which does not exist here |

`--dry-run` decides and records without touching the page.
