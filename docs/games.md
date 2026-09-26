# Games: the Doom shape

A game has no candidate list to extract, so `play` takes the other shape: you
give it a JS state probe and a small action set, and Jev decides at a fixed rate
while the game keeps running — movement keys stay held between decisions, so the
last answer applies while the model thinks. Exactly how Jev plays Doom (fed
structured state as text, ~10 calls/second, no images).

```bash
jevnav play \
  --goal "catch the green blocks, dodge the red ones" \
  --url "examples/game/index.html?seed=7" \
  --state-js examples/game/state.js \
  --actions "left=ArrowLeft" --actions "right=ArrowRight" \
  --rate 4 --seconds 60 --score-js "window.jevnavScore()" \
  --ready-js "() => !!window.jevnavState" \
  --report play.md
```

Measured on the bundled game (`../examples/game/`), 60 seconds, three seeds, same
decision rate for both sides:

| seed | random control @3/s | Jev @3/s |
|---|---|---|
| 3 | 0 | 1 |
| 7 | 1 | **6** |
| 11 | 1 | 2 |
| mean | 0.67 | **3.0** |

Jev latency was 312ms p50 from Vietnam, which is what caps the loop at ~3
decisions/second (TypeSafe's Doom demo ran ~10/s from a US network). Run
`--policy random` for your own control, and keep the trace: it is the evidence.

What works where: a DOM game (like the bundled one, or 2048) exposes state to
JS, so a probe is easy. A `<canvas>`/WebGL game — or Flash — has no state in the
DOM; it needs the game itself to expose one (Chocolate Doom WASM does, which is
how the browser Doom agents read it). jevnav still takes no screenshots and
makes no pixel decisions, deliberately: that is what keeps decisions replayable.
