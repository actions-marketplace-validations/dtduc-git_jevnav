"""Markdown reports: what a run decided, and what replay found."""

from __future__ import annotations

from typing import Any

from .gates import AUTO


def _p(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def render_run_report(
    flow_id: str,
    trace_path: str,
    records: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    model: str | None = None,
) -> str:
    lines = [f"# jevnav run — {flow_id}", ""]
    lines.append(f"- trace: `{trace_path}`")
    models = sorted({r["decision"]["model"] for r in records if r["decision"].get("model")})
    model_label = ", ".join(f"`{m}`" for m in models) if models else f"`{model or 'unknown'}`"
    lines.append(f"- model: {model_label}")
    lines.append(
        f"- steps: {summary['steps']} — auto **{summary['auto']}**, review "
        f"**{summary['review']}**, blocked **{summary['blocked']}**"
    )
    if summary["scored"]:
        lines.append(
            f"- accuracy where ground truth was given: {summary['correct']}/{summary['scored']}"
        )
    latency = (
        f"p50 {summary['latency_p50_ms']:.0f}ms, p95 {summary['latency_p95_ms']:.0f}ms"
        if summary["latency_p50_ms"] is not None
        else "n/a"
    )
    lines.append(f"- cost: ${summary['cost_usd']:.6f} total · latency {latency}")
    lines.append("")
    lines.append("## Steps")
    lines.append("")
    lines.append("| # | intent | action | p | gate | target | expected | locator unique |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for record in records:
        decision = record["decision"]
        lines.append(
            "| {step} | {intent} | {action} | {p} | {gate} | {target} | {expected} "
            "| {unique} |".format(
                step=record["step"],
                intent=_cell(record["intent"]),
                action=record["action"]["type"],
                p=_p(decision.get("confidence")),
                gate=record["gate"]["verdict"],
                target=_cell(decision.get("chosen_name") or decision.get("choice") or "—"),
                expected=_cell(
                    record["expected_cid"] or "—",
                ),
                unique={True: "yes", False: "no", None: "—"}[
                    None if record["locator"] is None else record["locator"]["unique"]
                ],
            )
        )
    reviews = [r for r in records if r["gate"]["verdict"] != AUTO]
    if reviews:
        lines.append("")
        lines.append("## Needs a human")
        lines.append("")
        for record in reviews:
            lines.append(
                f"- step {record['step']} ({record['gate']['verdict']}): {record['intent']} — "
                f"{record['gate']['reason']}"
            )
    return "\n".join(lines) + "\n"


def render_replay_report(result: dict[str, Any]) -> str:
    counts = result["counts"]
    lines = [f"# jevnav replay — {result['trace']}", ""]
    lines.append(f"- steps: {result['steps']}")
    lines.append(
        "- verdicts: " + ", ".join(f"{name} **{count}**" for name, count in sorted(counts.items()))
    )
    if result.get("swapped"):
        lines.append(f"- every step served from `{result['swapped']}`")
    lines.append(f"- failing steps: {result['failed'] or 'none'}")
    lines.append("")
    lines.append("## Steps")
    lines.append("")
    lines.append("| # | intent | verdict | reason | drift (new/missing) |")
    lines.append("|---|---|---|---|---|")
    for step in result["results"]:
        drift = step["drift"]
        lines.append(
            "| {step} | {intent} | {verdict} | {reason} | {new}/{missing} |".format(
                step=step["step"],
                intent=_cell(step["intent"]),
                verdict=step["verdict"],
                reason=_cell(step.get("reason") or ""),
                new=drift["new"],
                missing=drift["missing"],
            )
        )
    return "\n".join(lines) + "\n"


def _cell(text: str, limit: int = 60) -> str:
    flat = " ".join(str(text).split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
