"""Measure the gate on its own terms: what the word list catches, and what the
confidence threshold does when the model is actually wrong.

Part A is offline and deterministic: labeled intents (nine languages, risky and
benign lookalikes) are classified by the risky patterns, precision and recall
against the labels are reported, and every mistake is named.

Part B is live and needs a TypeSafe key: a local page with near-duplicate
controls (two "Save", three "Edit", an icon-only button) and two
benign-but-flagged buttons, decided `--runs` times each through the same
machinery `browse` uses, then classified by the real `verdict()`. Reported:
accuracy, verdict distribution, and the two numbers that matter — of the wrong
decisions, how many the gate caught (review/blocked); of the right ones, how
many it sent to review anyway.

    uv run python benchmarks/gate-study/run.py                  # part A only
    uv run python benchmarks/gate-study/run.py --live --runs 3  # both, ~$0.005
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "src"))

from jevnav.decide import build_question  # noqa: E402
from jevnav.gates import default_gates, risk_match, verdict  # noqa: E402
from jevnav.page import extract  # noqa: E402

INPUT_USD_PER_MTOK = 0.042
CAUGHT = {"review", "blocked"}


def api_key() -> str:
    candidate = pathlib.Path.home() / ".config/typesafe/apikey.txt"
    if candidate.exists():
        return candidate.read_text().strip()
    raise SystemExit("no API key found")


def part_a(gates: dict) -> dict:
    cases = json.loads((HERE / "intents.json").read_text())
    rows = []
    for case in cases:
        pattern = risk_match(case["intent"], {"name": case["target"], "role": "button"}, gates)
        rows.append({**case, "flagged": bool(pattern), "pattern": pattern})
    tp = sum(row["flagged"] and row["risky"] for row in rows)
    fp = sum(row["flagged"] and not row["risky"] for row in rows)
    fn = sum(not row["flagged"] and row["risky"] for row in rows)
    tn = sum(not row["flagged"] and not row["risky"] for row in rows)
    by_language = {}
    for lang in sorted({row["lang"] for row in rows}):
        subset = [row for row in rows if row["lang"] == lang]
        by_language[lang] = {
            "cases": len(subset),
            "flagged": sum(row["flagged"] for row in subset),
            "missed_risky": [row["id"] for row in subset if row["risky"] and not row["flagged"]],
        }
    return {
        "cases": len(rows),
        "risky": tp + fn,
        "benign": fp + tn,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        "flagged_benign": [row["id"] for row in rows if row["flagged"] and not row["risky"]],
        "missed_risky": [row["id"] for row in rows if row["risky"] and not row["flagged"]],
        "by_language": by_language,
        "rows": rows,
    }


def part_b(gates: dict, runs: int) -> dict:
    from jevassert.client import JevClient
    from playwright.sync_api import sync_playwright

    cases = json.loads((HERE / "cases.json").read_text())
    page_url = (HERE / "pages" / "ambiguous.html").as_uri()
    client = JevClient(api_key=api_key())
    rows: list[dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        for run in range(1, runs + 1):
            for case in cases:
                row: dict = {"run": run, "id": case["id"], "intent": case["intent"]}
                try:
                    page.goto(page_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(150)
                    candidates, total, dropped = extract(page)
                    expected_cid = page.locator(case["expect"]).first.evaluate(
                        "el => { const c = el.closest('[data-jevcid]');"
                        " return c ? c.getAttribute('data-jevcid') : null; }"
                    )
                    expected = next((c for c in candidates if c["cid"] == expected_cid), None)
                    if expected is None:
                        row |= {"valid": False, "reason": "expected element is not a candidate"}
                        rows.append(row)
                        continue
                    question = build_question(page_url, page.title(), case["intent"], candidates)
                    response, latency_ms = client.system_one(
                        {"page": f"{page.title()} — {page.url}"},
                        {"target": question},
                        model="jev-latest",
                    )
                    answer = (response.get("answers") or {}).get("target") or {}
                    chosen = next((c for c in candidates if c["cid"] == answer.get("choice")), None)
                    gate, reason = verdict(
                        {"choice": answer.get("choice"), "confidence": answer.get("confidence")},
                        intent=case["intent"],
                        candidate=chosen,
                        dropped=dropped,
                        gates=gates,
                    )
                    usage = response.get("usage") or {}
                    row |= {
                        "valid": True,
                        "correct": bool(chosen and chosen["fp"] == expected["fp"]),
                        "chosen": chosen["name"] if chosen else "none",
                        "expected": expected["name"],
                        "confidence": answer.get("confidence"),
                        "gate": gate,
                        "gate_reason": reason,
                        "risky_label": case["risky"],
                        "latency_ms": round(latency_ms, 1),
                        "cost_usd": (usage.get("input_tokens") or 0)
                        * INPUT_USD_PER_MTOK
                        / 1_000_000,
                    }
                except Exception as error:
                    row |= {"valid": False, "reason": f"{type(error).__name__}: {str(error)[:120]}"}
                rows.append(row)
                mark = "ok  " if row.get("correct") else ("MISS" if row.get("valid") else "err ")
                print(
                    f"[{mark}] run {row['run']} {row['id']:16s} p={row.get('confidence')} "
                    f"gate={row.get('gate')} chose {row.get('chosen')!r}"
                )
        browser.close()
    client.close()

    valid = [row for row in rows if row.get("valid")]
    wrong = [row for row in valid if not row["correct"]]
    right = [row for row in valid if row["correct"]]
    reviews = [row for row in valid if row["gate"] == "review"]
    latencies = [row["latency_ms"] for row in valid]
    auto = [row for row in valid if row["gate"] == "auto"]
    # `blocked` is the model answering `none`, not the gate catching anything;
    # `review` is the gate stopping a decision that would otherwise have run.
    wrong_reviewed = [row for row in wrong if row["gate"] == "review"]
    # A review is warranted when the choice was wrong, or the action was labeled
    # risky by hand — the question the gate exists to answer.
    warranted = [row for row in reviews if not row["correct"] or row["risky_label"]]
    return {
        "runs": runs,
        "decisions": len(valid),
        "correct": len(right),
        "accuracy": round(len(right) / len(valid), 4) if valid else None,
        "verdicts": {
            name: sum(row["gate"] == name for row in valid)
            for name in ("auto", "review", "blocked")
        },
        "wrong": len(wrong),
        "wrong_blocked": [row["id"] for row in wrong if row["gate"] == "blocked"],
        "wrong_reviewed": [row["id"] for row in wrong_reviewed],
        "wrong_auto": [row["id"] for row in wrong if row["gate"] == "auto"],
        "threshold_catches": len(wrong_reviewed),
        "false_review_rate": round(sum(row["gate"] == "review" for row in right) / len(right), 4)
        if right
        else None,
        "review_precision": round(len(warranted) / len(reviews), 4) if reviews else None,
        "auto_precision": round(sum(row["correct"] for row in auto) / len(auto), 4)
        if auto
        else None,
        "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "cost_usd": round(sum(row.get("cost_usd") or 0 for row in valid), 6),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="run part B (needs a TypeSafe key)")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--json", help="where to write the raw results")
    args = parser.parse_args()

    gates = default_gates()
    result: dict = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "part_a": part_a(gates),
    }
    a = result["part_a"]
    print(
        f"part A: {a['cases']} labeled intents — precision {a['precision']}, recall {a['recall']}"
    )
    print(f"  flagged benign: {a['flagged_benign']}")
    print(f"  missed risky:   {a['missed_risky']}")

    if args.live:
        result["part_b"] = part_b(gates, args.runs)
        b = result["part_b"]
        print(
            f"part B: {b['decisions']} decisions — accuracy {b['accuracy']}, "
            f"verdicts {b['verdicts']}"
        )
        print(
            f"  wrong {b['wrong']} — reviewed {b['wrong_reviewed']}, blocked {b['wrong_blocked']}, "
            f"auto (missed) {b['wrong_auto']}"
        )
        print(
            f"  review precision {b['review_precision']} | false reviews "
            f"{b['false_review_rate']} | auto precision {b['auto_precision']}"
        )

    out = (
        pathlib.Path(args.json) if args.json else HERE / f"results-{time.strftime('%Y-%m-%d')}.json"
    )
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
