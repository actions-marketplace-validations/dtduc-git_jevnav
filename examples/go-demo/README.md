# go-demo

An agent run recorded by `jevnav go`, committed with its trace — replay it with
no API key and no network:

```bash
jevnav replay examples/go-demo/demo.trace.jsonl --execute
```

```
- steps: 5
- verdicts: ok **5**
- success check: **verified** (`#pricing.visible` is visible)
```

That is the whole point of jevnav: Jev drove the browser (filled two fields,
clicked Sign in, then clicked Pricing, then stopped because the goal was met),
and the run can be re-executed later — offline, deterministically — as a
regression test. `--execute` re-runs the recorded actions and then checks the
recorded success selector.

Drop `--execute` and replay only re-resolves each recorded decision against the
page; break the page (rename `Sign in` to `Log in`) and the replay fails on
step 3 with the reason.

## Recording it yourself

```bash
jevnav go \
  --goal "Sign in to Acme Console with the demo account and then open the pricing page" \
  --start examples/go-demo/app.html \
  --context email=demo@example.com --context password=hunter2 \
  --success "#pricing.visible" \
  --report goal.md
```

The report shows every step: what Jev decided (`status`, `action`, `target`),
its confidence, the gate verdict and what happened. Risky steps (a `Delete`
button, a goal that mentions money) stop the loop at `review` and are never
executed — unless you pass `--allow-risky`, which is for sandboxes only.
