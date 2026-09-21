"""Measure element selection on real public pages, with hand-written ground truth.

One decision per case: the intent is turned into the same choice question
`browse` uses (the page's ranked shortlist as options), Jev answers, and the
answer is compared with the element the `expect` selector points at. Reported:
accuracy, precision at the p>=0.9 gate (what would run unattended), review rate,
latency and cost.

    uv run python benchmarks/element-selection/run.py [--limit N] [--json out.json]

Cases whose `expect` selector matches zero or more than one candidate are
reported as invalid rather than silently scored.
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

from jevassert.client import JevClient  # noqa: E402

from jevnav.decide import build_question  # noqa: E402
from jevnav.page import extract  # noqa: E402

HIGH_CONF = 0.9
INPUT_USD_PER_MTOK = 0.042


def api_key() -> str:
    for candidate in (pathlib.Path.home() / ".config/typesafe/apikey.txt",):
        if candidate.exists():
            return candidate.read_text().strip()
    raise SystemExit("no API key found")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json", help="write the raw per-case results here")
    parser.add_argument("--only", help="substring filter on the case id")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    cases = json.loads((HERE / "cases.json").read_text())
    if args.only:
        cases = [case for case in cases if args.only in case["id"]]
    if args.limit:
        cases = cases[: args.limit]
    client = JevClient(api_key=api_key())
    rows: list[dict] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        for case in cases:
            row: dict = {"id": case["id"], "url": case["url"], "intent": case["intent"]}
            try:
                page.goto(case["url"], wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(600)
                candidates, total, dropped = extract(page)
                locator = page.locator(case["expect"])
                visible = [
                    locator.nth(index)
                    for index in range(locator.count())
                    if locator.nth(index).is_visible()
                ]
                if len(visible) != 1:
                    row |= {
                        "valid": False,
                        "reason": f"expect selector matched {len(visible)} visible elements",
                    }
                    rows.append(row)
                    print(f"[invalid] {case['id']}: expect matched {len(visible)} visible")
                    continue
                expected_cid = visible[0].evaluate(
                    "el => { const c = el.closest('[data-jevcid]');"
                    " return c ? c.getAttribute('data-jevcid') : null; }"
                )
                expected = next((c for c in candidates if c["cid"] == expected_cid), None)
                if expected is None:
                    row |= {"valid": False, "reason": "expected element is not a candidate"}
                    rows.append(row)
                    print(f"[invalid] {case['id']}: expected element not in the shortlist")
                    continue
                expected_fp = expected["fp"]
                question = build_question(case["url"], page.title(), case["intent"], candidates)
                started = time.perf_counter()
                response, latency_ms = client.system_one(
                    {"page": f"{page.title()} — {page.url}"},
                    {"target": question},
                    model="jev-latest",
                )
                wall = (time.perf_counter() - started) * 1000
                answer = (response.get("answers") or {}).get("target") or {}
                usage = response.get("usage") or {}
                chosen = next((c for c in candidates if c["cid"] == answer.get("choice")), None)
                row |= {
                    "valid": True,
                    "choice": answer.get("choice"),
                    "chosen": chosen["name"] if chosen else "none",
                    "chosen_fp": chosen["fp"] if chosen else None,
                    "expected_fp": expected_fp,
                    "confidence": answer.get("confidence"),
                    "correct": bool(chosen and expected_fp and chosen["fp"] == expected_fp),
                    "latency_ms": round(latency_ms, 1),
                    "wall_ms": round(wall, 1),
                    "candidates": len(candidates),
                    "total_on_page": total,
                    "dropped": dropped,
                    "cost_usd": (usage.get("input_tokens") or 0) * INPUT_USD_PER_MTOK / 1_000_000,
                    "input_tokens": usage.get("input_tokens"),
                    "model": response.get("model"),
                }
                print(
                    f"[{'ok  ' if row['correct'] else 'MISS'}] p={row['confidence']:.2f} "
                    f"{case['id']:28s} chose {row['chosen'][:34]!r}"
                )
            except Exception as error:  # a page that would not load is not a decision
                row |= {"valid": False, "reason": f"{type(error).__name__}: {str(error)[:120]}"}
                print(f"[error] {case['id']}: {row['reason']}")
            rows.append(row)
        browser.close()
    client.close()

    valid = [row for row in rows if row.get("valid")]
    scored = [row for row in valid if row.get("correct") is not None]
    high = [row for row in scored if (row.get("confidence") or 0) >= HIGH_CONF]
    latencies = [row["latency_ms"] for row in valid]
    summary = {
        "cases": len(rows),
        "valid": len(valid),
        "invalid": len(rows) - len(valid),
        "scored": len(scored),
        "correct": sum(row["correct"] for row in scored),
        "accuracy": round(sum(row["correct"] for row in scored) / len(scored), 4)
        if scored
        else None,
        "auto_at_high_conf": len(high),
        "auto_rate": round(len(high) / len(scored), 4) if scored else None,
        "precision_at_gate": round(sum(row["correct"] for row in high) / len(high), 4)
        if high
        else None,
        "wrong_above_gate": [row["id"] for row in high if not row["correct"]],
        "wrong_below_gate": [
            row["id"]
            for row in scored
            if not row["correct"] and (row.get("confidence") or 0) < HIGH_CONF
        ],
        "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "latency_p95_ms": round(sorted(latencies)[int(0.95 * len(latencies)) - 1], 1)
        if latencies
        else None,
        "cost_usd": round(sum(row.get("cost_usd") or 0 for row in valid), 6),
        "cost_per_case_usd": round(sum(row.get("cost_usd") or 0 for row in valid) / len(valid), 6)
        if valid
        else None,
        "models": sorted({row.get("model") for row in valid if row.get("model")}),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print("\n=== summary ===")
    for key, value in summary.items():
        print(f"{key}: {value}")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
