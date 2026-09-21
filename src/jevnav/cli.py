"""jevnav CLI: run a flow (Jev decides, jevnav records), replay a trace offline."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from .agent import run_goal, summarize_goal
from .flow import FlowError, load_flow, recorded_url, resolve_url, run_flow, summarize_run
from .gates import load_gates
from .play import parse_actions, play
from .replay import replay_trace
from .report import render_goal_report, render_play_report, render_replay_report, render_run_report
from .trace import TraceWriter

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _client():
    from jevassert.client import JevClient

    return JevClient(api_key=_api_key())


def _api_key() -> str | None:
    import os

    if os.environ.get("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"]
    path = Path.home() / ".config/typesafe/apikey.txt"
    return path.read_text().strip() if path.exists() else None


@contextmanager
def _session(
    headed: bool,
    user_data_dir: str | None = None,
    cdp: str | None = None,
    dialog_policy: str = "dismiss",
    engine: str = "chromium",
    locale: str | None = None,
    timezone: str | None = None,
    user_agent: str | None = None,
):
    from .browser import browser_session

    with browser_session(
        headed=headed,
        user_data_dir=user_data_dir,
        cdp=cdp,
        dialog_policy=dialog_policy,
        engine=engine,
        locale=locale,
        timezone=timezone,
        user_agent=user_agent,
    ) as page:
        yield page


def add_browser_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument(
        "--user-data-dir",
        help="persistent Chromium profile: log in once (headful), stay logged in",
    )
    parser.add_argument(
        "--cdp",
        help="attach to a running Chrome over CDP, e.g. http://127.0.0.1:9222 "
        "(start Chrome with --remote-debugging-port=9222)",
    )
    parser.add_argument(
        "--browser",
        choices=["chromium", "firefox", "webkit"],
        default="chromium",
        help="which engine to drive (firefox/webkit need `playwright install firefox webkit`)",
    )
    parser.add_argument("--locale", help="context locale, e.g. en-US or vi-VN")
    parser.add_argument("--timezone", help="context timezone, e.g. Asia/Ho_Chi_Minh")
    parser.add_argument("--user-agent", help="override the user agent")
    parser.add_argument(
        "--dialog-policy",
        choices=["dismiss", "accept"],
        default="dismiss",
        help="what to do with alert/confirm/prompt dialogs (they are always recorded)",
    )


def cmd_run(args: argparse.Namespace) -> int:
    flow = load_flow(args.flow)
    if args.gates:
        gates = load_gates(args.gates)
    else:
        default_gates_path = Path(args.flow).parent / "gates.yaml"
        gates = load_gates(default_gates_path if default_gates_path.exists() else None)
    trace_path = Path(args.trace or f"{flow['id']}.trace.jsonl")
    client = _client()
    try:
        with _session(*session_options(args)) as page:
            with TraceWriter(
                trace_path,
                flow=flow["id"],
                model=args.model,
                start=recorded_url(flow["start"], Path(args.flow).resolve().parent),
                tool=f"jevnav/{__version__}",
            ) as writer:
                records = run_flow(
                    flow,
                    page=page,
                    client=client,
                    gates=gates,
                    writer=writer,
                    model=args.model,
                    dry_run=args.dry_run,
                )
    finally:
        client.close()

    summary = summarize_run(records)
    report = render_run_report(flow["id"], str(trace_path), records, summary, model=args.model)
    if args.report:
        Path(args.report).write_text(report)
    if args.json:
        print(json.dumps({"summary": summary, "steps": records}, indent=2))
    else:
        print(report, end="")
        print(f"trace: {trace_path}")
    failures = [r for r in records if r["decision"].get("error") or r["result"].get("error")]
    if failures:
        print(f"\n{len(failures)} step(s) failed to decide or act", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def cmd_replay(args: argparse.Namespace) -> int:
    with _session(*session_options(args)) as page:
        result = replay_trace(
            args.trace,
            page=page,
            swap=args.swap,
            execute=args.execute,
            settle_ms=args.settle_ms,
        )
    report = render_replay_report(result)
    if args.report:
        Path(args.report).write_text(report)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(report, end="")
    success = result.get("success") or {}
    if result["failed"] or success.get("verified") is False:
        failed = [
            f"steps {result['failed']}" if result["failed"] else "",
            "success check" if success.get("verified") is False else "",
        ]
        print(f"\nreplay failed: {', '.join(part for part in failed if part)}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def session_options(args: argparse.Namespace) -> tuple:
    """The `_session` arguments, from the shared browser flags."""
    return (
        args.headed,
        args.user_data_dir,
        args.cdp,
        args.dialog_policy,
        args.browser,
        args.locale,
        args.timezone,
        args.user_agent,
    )


def parse_context(pairs: list[str]) -> dict[str, str]:
    context: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise FlowError(f"--context needs KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        context[key] = value
    return context


def cmd_go(args: argparse.Namespace) -> int:
    context = parse_context(args.context)
    trace_path = Path(args.trace or "goal.trace.jsonl")
    client = _client()
    try:
        with _session(*session_options(args)) as page:
            with TraceWriter(
                trace_path,
                flow="goal",
                goal=args.goal,
                context_keys=list(context),
                success=args.success,
                max_steps=args.max_steps,
                model=args.model,
                tool=f"jevnav/{__version__}",
            ) as writer:
                result = run_goal(
                    args.goal,
                    page=page,
                    client=client,
                    gates=load_gates(args.gates),
                    writer=writer,
                    model=args.model,
                    context=context,
                    start=resolve_url(args.start, Path.cwd()) if args.start else None,
                    success=args.success,
                    max_steps=args.max_steps,
                    min_confidence=args.min_confidence,
                    dry_run=args.dry_run,
                    allow_risky=args.allow_risky,
                    settle_ms=args.settle_ms,
                )
    finally:
        client.close()
    summary = summarize_goal(result)
    report = render_goal_report(summary, result["steps"], trace_path=str(trace_path))
    if args.report:
        Path(args.report).write_text(report)
    if args.json:
        print(json.dumps({"summary": summary, "steps": result["steps"]}, indent=2))
    else:
        print(report, end="")
        print(f"trace: {trace_path}")
    if summary["status"] == "done" and summary["verified"] is not False:
        return EXIT_OK
    return EXIT_FAILED


def cmd_play(args: argparse.Namespace) -> int:
    """Real-time play (the Doom shape): a state probe, an action set, a fixed rate."""
    if not args.state_js and not args.state:
        raise FlowError("play needs a state probe: --state-js <file> or --state '<js>'")
    state_js = Path(args.state_js).read_text() if args.state_js else args.state
    actions = parse_actions(args.actions)
    trace_path = Path(args.trace or "play.trace.jsonl")
    client = _client()
    try:
        with _session(*session_options(args)) as page:
            if args.url:
                page.goto(resolve_url(args.url, Path.cwd()), wait_until="domcontentloaded")
            if args.ready_js:
                page.wait_for_function(args.ready_js, timeout=args.ready_timeout * 1000)
            with TraceWriter(
                trace_path,
                flow="play",
                goal=args.goal,
                actions=list(actions),
                rate_hz=args.rate,
                seconds=args.seconds,
                policy=args.policy,
                model=args.model,
                tool=f"jevnav/{__version__}",
            ) as writer:
                result = play(
                    goal=args.goal,
                    page=page,
                    client=client,
                    writer=writer,
                    state_js=state_js,
                    actions=actions,
                    model=args.model,
                    rate_hz=args.rate,
                    seconds=args.seconds,
                    max_steps=args.max_steps,
                    score_js=args.score_js,
                    policy=args.policy,
                    seed=args.seed,
                )
    finally:
        client.close()
    summary = {k: v for k, v in result.items() if k != "steps_detail"}
    report = render_play_report(summary, trace_path=str(trace_path))
    if args.report:
        Path(args.report).write_text(report)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(report, end="")
        print(f"trace: {trace_path}")
    if summary["errors"] and args.policy == "jev":
        return EXIT_FAILED
    return EXIT_OK


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp import serve

    if args.no_trace:
        args.trace = None
    elif not args.trace:
        # audits are the point: a session traces by default, next to the client's cwd
        args.trace = "jevnav-session.trace.jsonl"
    return serve(
        start=args.start,
        trace=args.trace,
        gates=args.gates,
        model=args.model,
        headed=args.headed,
        user_data_dir=args.user_data_dir,
        cdp=args.cdp,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jevnav", description=__doc__)
    parser.add_argument("--version", action="version", version=f"jevnav {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run", help="walk a flow: Jev picks each element, actions are executed, a trace is written"
    )
    run.add_argument("flow", help="flow.yaml")
    run.add_argument("--trace", help="where to write the trace (default <flow id>.trace.jsonl)")
    run.add_argument("--gates", help="gates.yaml (default: next to the flow)")
    run.add_argument("--report", help="write a markdown report here")
    run.add_argument("--model", default="jev-latest")
    add_browser_flags(run)
    run.add_argument("--dry-run", action="store_true", help="decide and record, but never act")
    run.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    run.set_defaults(func=cmd_run)

    replay = sub.add_parser("replay", help="re-resolve a recorded trace offline (no model call)")
    replay.add_argument("trace")
    replay.add_argument(
        "--swap", help="serve this local HTML file for every step (mutation testing)"
    )
    replay.add_argument("--execute", action="store_true", help="also re-run the recorded actions")
    replay.add_argument("--settle-ms", type=int, default=300)
    replay.add_argument("--report", help="write a markdown report here")
    add_browser_flags(replay)
    replay.add_argument("--json", action="store_true")
    replay.set_defaults(func=cmd_replay)

    go = sub.add_parser("go", help="let Jev drive towards a goal: decide, act, verify, record")
    go.add_argument(
        "--goal", required=True, help='what to achieve, e.g. "sign in with the demo account"'
    )
    go.add_argument("--start", help="URL to open first")
    go.add_argument(
        "--context",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="values the goal may need; ${ENV} refs are resolved and never traced",
    )
    go.add_argument("--success", help="selector that must be visible when the goal is done")
    go.add_argument("--max-steps", type=int, default=8)
    go.add_argument("--settle-ms", type=int, default=300)
    go.add_argument(
        "--min-confidence",
        type=float,
        help="your own confidence bar for the loop (risky patterns still gate)",
    )
    go.add_argument("--trace", help="where to write the trace (default goal.trace.jsonl)")
    go.add_argument("--gates", help="gates.yaml")
    go.add_argument("--report", help="write a markdown report here")
    go.add_argument("--model", default="jev-latest")
    add_browser_flags(go)
    go.add_argument("--dry-run", action="store_true", help="decide and record, but never act")
    go.add_argument(
        "--allow-risky",
        action="store_true",
        help="act on decisions the gate would send to review (sandboxes only)",
    )
    go.add_argument("--json", action="store_true")
    go.set_defaults(func=cmd_go)

    play = sub.add_parser(
        "play",
        help="play a real-time game: a JS state probe, a small action set, a fixed decision rate",
    )
    play.add_argument(
        "--goal", required=True, help='e.g. "catch the green blocks, dodge the red ones"'
    )
    play.add_argument("--url", help="game URL (local paths become file:// URLs)")
    play.add_argument(
        "--state-js",
        help="file with a JS expression returning the state text, e.g. window.jevnavState()",
    )
    play.add_argument("--state", help="the same JS expression inline")
    play.add_argument(
        "--actions",
        action="append",
        required=True,
        metavar="NAME[=KEY]",
        help="action set, e.g. --actions 'left=ArrowLeft' --actions 'right=ArrowRight'",
    )
    play.add_argument("--rate", type=float, default=5.0, help="decisions per second (default 5)")
    play.add_argument("--seconds", type=float, default=20.0)
    play.add_argument(
        "--max-steps", type=int, default=0, help="also stop after this many decisions"
    )
    play.add_argument(
        "--score-js", help="JS expression evaluated at the end, e.g. window.jevnavScore()"
    )
    play.add_argument(
        "--policy",
        choices=["jev", "random"],
        default="jev",
        help="'random' is the control run: same loop, no model, no cost",
    )
    play.add_argument("--seed", type=int, help="seed for --policy random")
    play.add_argument(
        "--ready-js",
        help="wait for this JS condition before playing, e.g. '() => !!window.jevnavState'",
    )
    play.add_argument("--ready-timeout", type=float, default=15.0)
    play.add_argument("--trace", help="where to write the trace (default play.trace.jsonl)")
    play.add_argument("--report", help="write a markdown report here")
    play.add_argument("--model", default="jev-latest")
    play.add_argument("--json", action="store_true")
    add_browser_flags(play)
    play.set_defaults(func=cmd_play)

    mcp = sub.add_parser("mcp", help="serve jevnav as an MCP tool (needs jevnav[mcp])")
    mcp.add_argument(
        "--start",
        help="optional: open this URL at startup. Without it the agent calls goto(url) "
        "itself, so one server serves every domain",
    )
    mcp.add_argument(
        "--trace",
        help="record every decision to this trace (default jevnav-session.trace.jsonl)",
    )
    mcp.add_argument("--no-trace", action="store_true", help="do not write a trace at all")
    mcp.add_argument("--gates", help="gates.yaml")
    mcp.add_argument("--model", default="jev-latest")
    mcp.add_argument("--headed", action="store_true")
    mcp.add_argument("--user-data-dir", help="persistent Chromium profile to reuse")
    mcp.add_argument(
        "--cdp", help="attach to a running Chrome over CDP, e.g. http://127.0.0.1:9222"
    )
    mcp.set_defaults(func=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"run", "go"} and not _api_key():
        print(
            "TYPESAFE_API_KEY is not set (and ~/.config/typesafe/apikey.txt is missing)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        return args.func(args)
    except FlowError as error:
        print(f"flow error: {error}", file=sys.stderr)
        return EXIT_USAGE
    except FileNotFoundError as error:
        print(f"missing file: {error}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
