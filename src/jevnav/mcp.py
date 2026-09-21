"""jevnav as an MCP server: the agent asks for an intent, jevnav decides, gates and acts.

Every call is recorded to the same trace format as ``jevnav run``, so an MCP
session is replayable and auditable afterwards. Actions that the gate marks
``review`` are never executed — the tool returns the decision and the reason,
and the agent (or the human behind it) decides what to do.

    jevnav mcp --start https://app.example.com --trace session.trace.jsonl
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from . import page as page_module
from .agent import run_goal, summarize_goal
from .decide import ask, failed_decision
from .flow import action_runtime, summarize_run
from .gates import AUTO, load_gates, verdict
from .trace import TraceWriter

ACTION_TYPES = {"click", "fill", "select", "check", "hover", "press"}


def build_single_question(
    page: Any, intent: str, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """The `browse` question shape, reused by the primitives that need a choice."""
    from .decide import build_question

    return build_question(page.url, page.title(), intent, candidates)


def _safe_title(page: Any) -> str:
    try:
        return page.title()[:120]
    except Exception:
        return ""


class BrowserThread:
    """A sync Playwright browser that lives on its own thread.

    MCP servers run an asyncio loop in the thread that calls ``serve()``, and
    sync Playwright cannot start in a thread that has a running loop. So the
    browser gets a thread of its own and every browser operation is handed to
    it and waited for — one agent, one browser, one call at a time.
    """

    def __init__(
        self,
        headed: bool = False,
        timeout: float = 60.0,
        user_data_dir: str | None = None,
        cdp: str | None = None,
        dialog_policy: str = "dismiss",
        engine: str = "chromium",
        locale: str | None = None,
        timezone: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._page: Any = None
        self.recorder: Any = None
        self._dialog_policy = dialog_policy
        self._browser_options = {
            "engine": engine,
            "locale": locale,
            "timezone": timezone,
            "user_agent": user_agent,
        }
        self._thread = threading.Thread(
            target=self._serve, args=(headed, user_data_dir, cdp), daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("the browser did not start in time")
        if self._error is not None:
            raise self._error

    def _serve(self, headed: bool, user_data_dir: str | None, cdp: str | None) -> None:
        from contextlib import ExitStack

        from .browser import browser_and_recorder

        with ExitStack() as stack:
            try:
                page, recorder = stack.enter_context(
                    browser_and_recorder(
                        headed=headed,
                        user_data_dir=user_data_dir,
                        cdp=cdp,
                        dialog_policy=self._dialog_policy,
                        **self._browser_options,
                    )
                )
                self._page, self.recorder = page, recorder
            except BaseException as error:  # surfaced in the caller's thread
                self._error = error
                self._ready.set()
                return
            self._ready.set()
            while True:
                job = self._jobs.get()
                if job is None:
                    break
                function, args, box = job
                try:
                    box["value"] = function(self._page, *args)
                except BaseException as error:
                    box["error"] = error
                finally:
                    box["done"].set()

    def switch_page(self, chooser: Callable[[Any], Any]) -> Any:
        """Change which page the session drives, in the thread that owns it (tabs)."""

        def job(page: Any) -> Any:
            from .browser import attach

            new_page = chooser(page)
            self._page = new_page
            self.recorder = attach(new_page, dialog_policy=self._dialog_policy)
            return new_page

        return self.call(job)

    def call(self, function: Callable[..., Any], *args: Any) -> Any:
        box: dict[str, Any] = {"done": threading.Event()}
        self._jobs.put((function, args, box))
        box["done"].wait()
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def close(self) -> None:
        self._jobs.put(None)
        self._thread.join(timeout=10)


def archive_existing_trace(trace: str | Path) -> Path | None:
    """Move a previous session's trace aside so evidence is never overwritten.

    The configured path always holds the latest session; older ones stay next
    to it as ``name.<UTC stamp>.jsonl``.
    """
    path = Path(trace)
    if not path.exists() or not path.stat().st_size:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    archived = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
    path.rename(archived)
    return archived


class Session:
    """One browser, one client, one trace — shared by every MCP tool call."""

    def __init__(
        self,
        *,
        start: str | None,
        trace: str | None = None,
        gates: str | None = None,
        model: str = "jev-latest",
        headed: bool = False,
        page: Any = None,
        client: Any = None,
        user_data_dir: str | None = None,
        cdp: str | None = None,
        dialog_policy: str = "dismiss",
        engine: str = "chromium",
        locale: str | None = None,
        timezone: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        from .cli import _client

        self.gates = load_gates(gates)
        self.model = model
        self.client = client or _client()
        self.browser: BrowserThread | None = None
        self.page = page
        self.recorder: Any = None
        self._browser_kwargs = {
            "headed": headed,
            "user_data_dir": user_data_dir,
            "cdp": cdp,
            "dialog_policy": dialog_policy,
            "engine": engine,
            "locale": locale,
            "timezone": timezone,
            "user_agent": user_agent,
        }
        if page is not None:
            from .browser import attach

            self.recorder = attach(page, dialog_policy=dialog_policy)
        self.writer = None
        if trace:
            archive_existing_trace(trace)
            self.writer = TraceWriter(
                trace, flow="mcp-session", model=model, tool=f"jevnav/{__version__}"
            )
        self.steps: list[dict[str, Any]] = []
        if start:
            self.on_page(lambda page: page.goto(start, wait_until="domcontentloaded"))

    def close(self) -> None:
        if self.writer:
            self.writer.close()
        if self.browser:
            self.browser.close()
        close = getattr(self.client, "close", None)
        if close:
            close()

    def on_page(self, function: Callable[[Any], Any]) -> Any:
        """Run one browser operation, launching the browser the first time it is needed."""
        if self.browser is not None:
            return self.browser.call(
                lambda page: (self.recorder and self.recorder.due_dialog(), function(page))[1]
            )
        if self.page is None:
            self.browser = BrowserThread(**self._browser_kwargs)
            self.page, self.recorder = self.browser.call(lambda page: (page, self.browser.recorder))
            return self.browser.call(function)
        return function(self.page)

    # ---- the observability side: what the agent may inspect --------------
    def console(self, limit: int = 20, only_errors: bool = False) -> dict[str, Any]:
        return {"messages": self._recorder().console_tail(limit, only_errors)}

    def network(self, limit: int = 20, only_failed: bool = False) -> dict[str, Any]:
        return {"requests": self._recorder().network_tail(limit, only_failed)}

    def dialogs(self) -> dict[str, Any]:
        """Dialogs seen so far, with the policy that resolved each one."""
        return {"dialogs": list(self._recorder().dialogs)}

    def read_js(self, expression: str) -> Any:
        """Evaluate JS in the page and return it (observation; not traced)."""
        return self.on_page(lambda page: page.evaluate(expression))

    def wait_for(
        self, text: str | None = None, selector: str | None = None, timeout_ms: int = 15000
    ) -> dict[str, Any]:
        if text:
            self.on_page(lambda page: page.wait_for_selector(f"text={text}", timeout=timeout_ms))
        elif selector:
            self.on_page(lambda page: page.wait_for_selector(selector, timeout=timeout_ms))
        else:
            raise ValueError("wait_for needs text or selector")
        state = self.page_state()
        return {"url": state["url"], "title": state["title"]}

    def scroll(self, direction: str = "down", amount: int = 800) -> dict[str, Any]:
        delta = amount if direction == "down" else -amount
        self.on_page(lambda page: page.mouse.wheel(0, delta))
        state = self.page_state()
        return {"url": state["url"], "elements": state["elements"]}

    def tabs(self) -> dict[str, Any]:
        def describe(page: Any) -> dict[str, Any]:
            pages = page.context.pages
            return {
                "pages": [
                    {
                        "index": i,
                        "url": item.url[:200],
                        "title": _safe_title(item),
                        "current": item == page,
                    }
                    for i, item in enumerate(pages)
                ]
            }

        return self.on_page(describe)

    def select_page(self, index: int) -> dict[str, Any]:
        if self.browser is None:
            raise RuntimeError("tabs need jevnav's own browser (not an injected page)")
        self.browser.switch_page(lambda page: page.context.pages[index])
        return self.tabs()

    def new_page(self, url: str | None = None) -> dict[str, Any]:
        if self.browser is None:
            raise RuntimeError("tabs need jevnav's own browser (not an injected page)")
        self.browser.switch_page(
            lambda page: (
                (page.context.new_page().goto(url, wait_until="domcontentloaded") and None)
                or page.context.pages[-1]
                if url
                else page.context.new_page()
            )
        )
        return self.tabs()

    def close_page(self, index: int) -> dict[str, Any]:
        if self.browser is None:
            raise RuntimeError("tabs need jevnav's own browser (not an injected page)")

        def chooser(page: Any) -> Any:
            target = page.context.pages[index]
            pages = page.context.pages
            target.close()
            return page if page in pages and not page.is_closed() else pages[-1]

        self.browser.switch_page(chooser)
        return self.tabs()

    # ---- acting primitives the agent may need beyond click/fill -------------
    def screenshot(
        self, path: str | None = None, *, full_page: bool = False, selector: str | None = None
    ) -> dict[str, Any]:
        """Save a PNG for a human (or the agent) to look at. Never used by a decision."""
        target = Path(path or f"jevnav-screenshot-{int(time.time())}.png")
        target.parent.mkdir(parents=True, exist_ok=True)

        def capture(page: Any) -> None:
            if selector:
                page.locator(selector).first.screenshot(path=str(target))
            else:
                page.screenshot(path=str(target), full_page=full_page)

        self.on_page(capture)
        return {"path": str(target), "bytes": target.stat().st_size, "full_page": full_page}

    def upload_files(
        self, paths: list[str], *, selector: str | None = None, intent: str | None = None
    ) -> dict[str, Any]:
        """Set files on a file input, chosen by selector or by an intent Jev resolves."""
        missing = [item for item in paths if not Path(item).exists()]
        if missing:
            raise FileNotFoundError(f"no such file: {', '.join(missing)}")

        def choose(page: Any) -> str:
            if selector:
                locator = page.locator(selector).first
            else:
                candidates, _, _ = page_module.extract(page)
                files = [c for c in candidates if (c.get("type") or "").lower() == "file"]
                if not files:
                    raise RuntimeError("no file input on the page")
                if intent and len(files) > 1:
                    question = build_single_question(page, intent, files)
                    response, _ = self.client.system_one(
                        {"page": f"{page.title()} — {page.url}"},
                        {"target": question},
                        model=self.model,
                    )
                    choice = ((response.get("answers") or {}).get("target") or {}).get("choice")
                    chosen = next((c for c in files if c["cid"] == choice), None)
                    if chosen is None:
                        raise RuntimeError("no file input matched the intent")
                else:
                    chosen = files[0]
                locator = page.locator(f'[data-jevcid="{chosen["cid"]}"]').first
            locator.set_input_files(paths)
            return locator.get_attribute("aria-label") or "file input"

        label = self.on_page(choose)
        return {"files": paths, "target": label}

    def drag(
        self,
        *,
        source_selector: str,
        target_selector: str,
        source_position: dict[str, float] | None = None,
        target_position: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Drag one element onto another (mouse-based, like Playwright's drag_to)."""

        def do(page: Any) -> None:
            page.locator(source_selector).first.drag_to(
                page.locator(target_selector).first,
                source_position=source_position,
                target_position=target_position,
            )

        self.on_page(do)
        return {"dragged": source_selector, "onto": target_selector}

    def press_key(self, key: str, selector: str | None = None) -> dict[str, Any]:
        """Press a key or combination ("Control+A", "Shift+Enter"), optionally on an element."""
        if selector:
            self.on_page(lambda page: page.locator(selector).first.press(key))
        else:
            self.on_page(lambda page: page.keyboard.press(key))
        return {"key": key, "selector": selector}

    def fill_form(self, fields: list[dict[str, Any]]) -> dict[str, Any]:
        """Fill several fields in one call.

        ``fields`` is a list of ``{"selector"|"intent", "value", "action"?}`` where
        action is fill (default), select, check or type. An ``intent`` is resolved
        by Jev against the file/text inputs the page shows.
        """
        results: list[dict[str, Any]] = []
        for field in fields:
            action = field.get("action", "fill")
            value = field.get("value")
            selector = field.get("selector")
            if selector is None and field.get("intent"):
                selector = self._selector_for_intent(
                    field["intent"], roles={"textbox", "searchbox", "combobox", "checkbox", "radio"}
                )
            if selector is None:
                raise ValueError(f"field {field!r} needs a selector or an intent")
            self.on_page(
                lambda page, sel=selector, act=action, val=value: _apply_field(page, sel, act, val)
            )
            results.append({"selector": selector, "action": action, "value": value})
        return {"filled": results}

    def _selector_for_intent(self, intent: str, *, roles: set[str]) -> str:
        def choose(page: Any) -> str:
            candidates, _, _ = page_module.extract(page)
            pool = [c for c in candidates if c["role"] in roles] or candidates
            if not pool:
                raise RuntimeError(f"nothing to resolve {intent!r} against")
            question = build_single_question(page, intent, pool)
            response, _ = self.client.system_one(
                {"page": f"{page.title()} — {page.url}"}, {"target": question}, model=self.model
            )
            choice = ((response.get("answers") or {}).get("target") or {}).get("choice")
            chosen = next((c for c in pool if c["cid"] == choice), None)
            if chosen is None:
                raise RuntimeError(f"no element matched {intent!r}")
            return f'[data-jevcid="{chosen["cid"]}"]'

        return self.on_page(choose)

    def network_detail(
        self, index: int | None = None, url_contains: str | None = None
    ) -> dict[str, Any]:
        """Headers and body of one recorded request (newest match when filtering by URL)."""
        return self._recorder().network_detail(index=index, url_contains=url_contains)

    def dialog_policy(self, action: str = "accept", match: str | None = None) -> dict[str, Any]:
        """Answer dialogs from now on: the session default, or a rule by message text.

        The sync API has to answer inside the dialog handler, so a dialog cannot
        be parked for a human (that deadlocks the page — measured). Set the
        policy before the dialog appears instead; every dialog is still recorded.
        """
        return self._recorder().set_dialog_policy(action, match=match)

    def resize(self, width: int, height: int) -> dict[str, Any]:
        self.on_page(lambda page: page.set_viewport_size({"width": width, "height": height}))
        return {"viewport": {"width": width, "height": height}}

    NETWORK_PRESETS = {
        "Slow 3G": {"latency_ms": 400, "download_kbps": 400, "upload_kbps": 400},
        "Fast 3G": {"latency_ms": 150, "download_kbps": 1600, "upload_kbps": 750},
        "Slow 4G": {"latency_ms": 80, "download_kbps": 4000, "upload_kbps": 3000},
        "Fast 4G": {"latency_ms": 20, "download_kbps": 16000, "upload_kbps": 9000},
    }

    def emulate(
        self,
        *,
        color_scheme: str | None = None,
        reduced_motion: str | None = None,
        forced_colors: str | None = None,
        media: str | None = None,
        geolocation: str | None = None,
        offline: bool | None = None,
        cpu_throttle: float | None = None,
        network_conditions: str | None = None,
        latency_ms: int | None = None,
        download_kbps: int | None = None,
        upload_kbps: int | None = None,
    ) -> dict[str, Any]:
        """Emulate media, geolocation, CPU throttling and network conditions.

        CPU and network throttling go through CDP, so they are chromium-only.
        ``network_conditions`` accepts a preset name or explicit kbps/latency.
        """
        applied: dict[str, Any] = {}
        preset = self.NETWORK_PRESETS.get(network_conditions) if network_conditions else None
        if network_conditions and preset is None and latency_ms is None:
            raise ValueError(
                f"unknown network preset {network_conditions!r} (expected one of "
                f"{sorted(self.NETWORK_PRESETS)} or explicit latency/download_kbps)"
            )
        profile = preset or {}
        latency = latency_ms if latency_ms is not None else profile.get("latency_ms")
        down = download_kbps if download_kbps is not None else profile.get("download_kbps")
        up = upload_kbps if upload_kbps is not None else profile.get("upload_kbps")

        def run(page: Any) -> None:
            if color_scheme or reduced_motion or forced_colors or media:
                page.emulate_media(
                    color_scheme=color_scheme,
                    reduced_motion=reduced_motion,
                    forced_colors=forced_colors,
                    media=media,
                )
            if geolocation is not None:
                latitude, _, longitude = geolocation.partition(",")
                page.context.set_geolocation(
                    {"latitude": float(latitude), "longitude": float(longitude)}
                )
                applied["geolocation"] = geolocation
            if offline is not None:
                page.context.set_offline(offline)
                applied["offline"] = offline
            if cpu_throttle is not None or down is not None:
                cdp = page.context.new_cdp_session(page)
                if cpu_throttle is not None:
                    cdp.send("Emulation.setCPUThrottlingRate", {"rate": cpu_throttle})
                    applied["cpu_throttle"] = cpu_throttle
                if down is not None:
                    cdp.send(
                        "Network.emulateNetworkConditions",
                        {
                            "offline": bool(offline),
                            "latency": latency or 0,
                            "downloadThroughput": int(down * 1024 / 8),
                            "uploadThroughput": int((up or down) * 1024 / 8),
                        },
                    )
                    applied["network"] = {
                        "latency_ms": latency or 0,
                        "download_kbps": down,
                        "upload_kbps": up or down,
                        "preset": network_conditions,
                    }
                cdp.detach()

        self.on_page(run)
        for key, value in {
            "color_scheme": color_scheme,
            "reduced_motion": reduced_motion,
            "forced_colors": forced_colors,
            "media": media,
        }.items():
            if value:
                applied[key] = value
        return applied or {"note": "nothing asked for"}

    def route(
        self,
        pattern: str,
        *,
        status: int = 200,
        body: str | None = None,
        content_type: str = "application/json",
        abort: bool = False,
    ) -> dict[str, Any]:
        """Stub or block matching requests (tests only; routes are not part of a trace)."""

        def handler(route: Any) -> None:
            if abort:
                route.abort()
            else:
                route.fulfill(status=status, body=body or "", content_type=content_type)

        self.on_page(lambda page: page.route(pattern, handler))
        return {"pattern": pattern, "stub": bool(body), "abort": abort, "status": status}

    def unroute(self, pattern: str | None = None) -> dict[str, Any]:
        if pattern:
            self.on_page(lambda page: page.unroute(pattern))
        else:
            self.on_page(lambda page: page.unroute_all(behavior="ignoreErrors"))
        return {"unrouted": pattern or "all"}

    def trace_start(self, screenshots: bool = True) -> dict[str, Any]:
        """Start a Playwright trace (separate from jevnav's decision trace)."""
        self.on_page(
            lambda page: page.context.tracing.start(screenshots=screenshots, snapshots=True)
        )
        return {"tracing": True, "screenshots": screenshots}

    def trace_stop(self, path: str | None = None) -> dict[str, Any]:
        target = Path(path or f"jevnav-trace-{int(time.time())}.zip")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.on_page(lambda page: page.context.tracing.stop(path=str(target)))
        return {
            "path": str(target),
            "bytes": target.stat().st_size,
            "open_with": "npx playwright show-trace <path>",
        }

    # ---- profiling: chromium-only, and never part of a decision ------------
    def perf_metrics(self) -> dict[str, Any]:
        """Chromium performance counters (CDP Performance.getMetrics) for the page."""

        def collect(page: Any) -> dict[str, float]:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Performance.enable")
            metrics = cdp.send("Performance.getMetrics")["metrics"]
            cdp.detach()
            return {item["name"]: item["value"] for item in metrics}

        metrics = self.on_page(collect)
        interesting = (
            "Timestamp",
            "Documents",
            "Frames",
            "JSEventListeners",
            "LayoutCount",
            "RecalcStyleCount",
            "ScriptDuration",
            "LayoutDuration",
            "RecalcStyleDuration",
            "TaskDuration",
            "JSHeapUsedSize",
            "JSHeapTotalSize",
        )
        return {"metrics": {k: v for k, v in metrics.items() if k in interesting}}

    def heap_snapshot(self, path: str | None = None) -> dict[str, Any]:
        """Write a Chromium heap snapshot (open it in Chrome DevTools > Memory)."""
        target = Path(path or f"jevnav-heap-{int(time.time())}.heapsnapshot")
        target.parent.mkdir(parents=True, exist_ok=True)

        def dump(page: Any) -> None:
            cdp = page.context.new_cdp_session(page)
            chunks: list[str] = []
            cdp.on("HeapProfiler.addHeapSnapshotChunk", lambda event: chunks.append(event["chunk"]))
            cdp.send("HeapProfiler.enable")
            cdp.send("HeapProfiler.takeHeapSnapshot", {"reportProgress": False})
            cdp.detach()
            target.write_text("".join(chunks))

        self.on_page(dump)
        return {"path": str(target), "bytes": target.stat().st_size}

    def lighthouse(
        self,
        url: str | None = None,
        *,
        categories: str = "performance,accessibility,best-practices,seo",
    ) -> dict[str, Any]:
        """Run Lighthouse against the current URL through npx (needs node on PATH)."""
        import shutil
        import subprocess
        import tempfile

        if shutil.which("npx") is None:
            raise RuntimeError("lighthouse needs node/npx on PATH")
        target = url or self.page.url
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "lighthouse.json"
            process = subprocess.run(
                [
                    "npx",
                    "-y",
                    "lighthouse",
                    target,
                    "--output=json",
                    f"--output-path={report}",
                    "--chrome-flags=--headless=new --no-sandbox",
                    f"--only-categories={categories}",
                    "--quiet",
                ],
                capture_output=True,
                text=True,
                timeout=240,
            )
            if not report.exists():
                raise RuntimeError(f"lighthouse produced no report: {process.stderr.strip()[:300]}")
            payload = json.loads(report.read_text())
        scores = {
            name: (data.get("score") if data.get("score") is None else round(data["score"], 3))
            for name, data in (payload.get("categories") or {}).items()
        }
        return {"url": target, "scores": scores}

    def _recorder(self) -> Any:
        if self.recorder is None:
            self.on_page(lambda page: None)  # launches the browser if it is not up yet
        return self.recorder

    def browse(
        self, intent: str, action: str, value: str | None, min_confidence: float | None = None
    ) -> dict[str, Any]:
        return self.on_page(lambda page: self._browse(page, intent, action, value, min_confidence))

    def _browse(
        self,
        page: Any,
        intent: str,
        action: str,
        value: str | None,
        min_confidence: float | None = None,
    ) -> dict[str, Any]:
        if action not in ACTION_TYPES:
            return {
                "status": "error",
                "error": f"unknown action {action!r} (expected one of {sorted(ACTION_TYPES)})",
            }
        candidates, total, dropped = page_module.extract(page)
        step = {
            "step": len(self.steps) + 1,
            "intent": intent,
            "action": {"type": action, **({"value": value} if value is not None else {})},
            "url": page.url,
            "title": page.title(),
            "total_on_page": total,
            "dropped": dropped,
            "candidates": candidates,
            "expected_cid": None,
        }
        if not candidates:
            decision = failed_decision(RuntimeError("no visible interactive elements on the page"))
        else:
            try:
                decision = ask(
                    self.client,
                    url=page.url,
                    title=step["title"],
                    intent=intent,
                    candidates=candidates,
                    model=self.model,
                    total_on_page=total,
                    dropped=dropped,
                )
            except Exception as error:
                decision = failed_decision(error)
        chosen = page_module.by_cid(candidates, decision.get("choice") or "")
        gate, reason = verdict(
            decision,
            intent=intent,
            candidate=chosen,
            dropped=dropped,
            gates=self.gates,
            min_confidence=min_confidence,
        )
        selector = None
        if chosen is not None:
            selector, unique = page_module.locator_for(page, chosen)
            selector = selector if unique else None
        step |= {
            "decision": decision,
            "gate": {"verdict": gate, "reason": reason},
            "locator": {"selector": selector, "unique": bool(selector)},
            "result": {"correct": None, "executed": False, "error": None},
        }
        out: dict[str, Any] = {
            "status": gate,
            "confidence": decision.get("confidence"),
            "model": decision.get("model"),
            "reason": reason,
            "target": {"name": decision.get("chosen_name"), "selector": selector},
        }
        if gate != AUTO:
            from .agent import alternatives as ranked_alternatives

            out["alternatives"] = ranked_alternatives(candidates, decision)
            out["hint"] = (
                "Call browse again with a more specific intent (name the element and where it is); "
                "specific intents score much higher than a broad goal."
            )
        if gate == AUTO:
            try:
                page_module.execute(
                    page,
                    chosen["cid"],
                    action_runtime(
                        {"action": action, **({"value": value} if value is not None else {})}
                    ),
                )
                step["result"]["executed"] = True
                out |= {"url": page.url, "title": page.title()}
            except Exception as error:
                step["result"]["error"] = f"{type(error).__name__}: {error}"
                out |= {"status": "error", "error": step["result"]["error"]}
        if self.writer:
            self.writer.step(**step)
        self.steps.append(step)
        return out

    def goal(
        self,
        goal: str,
        context: dict[str, str] | None = None,
        max_steps: int = 8,
        success: str | None = None,
    ) -> dict[str, Any]:
        """Drive the browser towards a goal, one gated Jev decision per step."""
        from .trace import NullWriter

        result = self.on_page(
            lambda page: run_goal(
                goal,
                page=page,
                client=self.client,
                gates=self.gates,
                writer=self.writer or NullWriter(),
                model=self.model,
                context=context or {},
                max_steps=max_steps,
                success=success,
            )
        )
        self.steps.extend(result["steps"])
        return summarize_goal(result)

    def goto(self, url: str) -> dict[str, Any]:
        """Open a URL in jevnav's browser (the agent's first move when it has no --start)."""
        self.on_page(lambda page: page.goto(url, wait_until="domcontentloaded"))
        state = self.page_state()
        return {"url": state["url"], "title": state["title"], "elements": state["elements"]}

    def page_state(self) -> dict[str, Any]:
        return self.on_page(self._page_state)

    def _page_state(self, page: Any) -> dict[str, Any]:
        candidates, total, dropped = page_module.extract(page)
        return {
            "url": page.url,
            "title": page.title(),
            "elements": total,
            "listed": len(candidates),
            "dropped": dropped,
            "candidates": [
                {"name": c["name"], "role": c["role"], "scope": c["scope"]} for c in candidates[:30]
            ],
            "candidates_shown": min(30, len(candidates)),
        }

    def summary(self) -> dict[str, Any]:
        return summarize_run(self.steps) if self.steps else {"steps": 0}


def server_class() -> Any:
    """The MCP server class, whichever name this SDK version uses (FastMCP in 1.x)."""
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def serve(
    *,
    start: str | None = None,
    trace: str | None = None,
    gates: str | None = None,
    model: str = "jev-latest",
    headed: bool = False,
    user_data_dir: str | None = None,
    cdp: str | None = None,
    dialog_policy: str = "dismiss",
    engine: str = "chromium",
    locale: str | None = None,
    timezone: str | None = None,
    user_agent: str | None = None,
) -> int:
    try:
        server_class()
    except ImportError:
        print("the MCP server needs the optional dependency: pip install 'jevnav[mcp]'")
        return 2

    session = Session(
        start=start,
        trace=trace,
        gates=gates,
        model=model,
        headed=headed,
        user_data_dir=user_data_dir,
        cdp=cdp,
        dialog_policy=dialog_policy,
        engine=engine,
        locale=locale,
        timezone=timezone,
        user_agent=user_agent,
    )
    mcp = server_class()("jevnav")

    @mcp.tool()
    def browse(
        intent: str,
        action: str = "click",
        value: str | None = None,
        min_confidence: float | None = None,
    ) -> str:
        """Find the element matching an intent and, if the gate allows it, act on it.

        Returns the gate verdict (auto / review / blocked), the confidence, the
        target element and its Playwright selector. Only ``auto`` decisions are
        executed. On large pages even precise intents score 0.8-0.95, so pass
        ``min_confidence`` to set your own bar; risky patterns and the
        deterministic checks still apply and cannot be overridden.
        """
        return json.dumps(session.browse(intent, action, value, min_confidence), ensure_ascii=False)

    @mcp.tool()
    def goto(url: str) -> str:
        """Open a URL in jevnav's browser and report what is on the page."""
        return json.dumps(session.goto(url), ensure_ascii=False)

    @mcp.tool()
    def goal(
        goal: str, context_json: str = "{}", max_steps: int = 8, success: str | None = None
    ) -> str:
        """Drive the browser towards a goal: Jev decides every step, jevnav acts.

        ``context_json`` is a JSON object of values the goal may need, e.g.
        {"email": "a@b.c", "password": "${PW}"}. ``success`` is a selector that
        must be visible when the goal is done: pass it and the outcome comes
        back verified or the run is reported as unverified. Returns the outcome
        (done, stuck, review, ...), the steps taken, cost and the verification.
        Risky steps stop the loop and come back unexecuted.
        """
        try:
            context = json.loads(context_json or "{}")
        except json.JSONDecodeError as error:
            return json.dumps({"status": "error", "error": f"context_json is not JSON: {error}"})
        return json.dumps(session.goal(goal, context, max_steps, success), ensure_ascii=False)

    @mcp.tool()
    def page_state() -> str:
        """Current URL, title and the interactive elements jevnav can see."""
        return json.dumps(session.page_state(), ensure_ascii=False)

    @mcp.tool()
    def console(limit: int = 20, only_errors: bool = False) -> str:
        """Recent console messages and page errors (observation only, never traced)."""
        return json.dumps(session.console(limit, only_errors), ensure_ascii=False)

    @mcp.tool()
    def network(limit: int = 20, only_failed: bool = False) -> str:
        """Recent network requests; only_failed keeps 4xx/5xx and transport errors."""
        return json.dumps(session.network(limit, only_failed), ensure_ascii=False)

    @mcp.tool()
    def screenshot(
        path: str | None = None, full_page: bool = False, selector: str | None = None
    ) -> str:
        """Save a PNG of the page (or one element) to a path, for a human to look at."""
        return json.dumps(
            session.screenshot(path, full_page=full_page, selector=selector), ensure_ascii=False
        )

    @mcp.tool()
    def upload_files(
        paths: list[str], selector: str | None = None, intent: str | None = None
    ) -> str:
        """Set files on a file input, chosen by selector or by an intent Jev resolves."""
        return json.dumps(
            session.upload_files(paths, selector=selector, intent=intent), ensure_ascii=False
        )

    @mcp.tool()
    def drag(source_selector: str, target_selector: str) -> str:
        """Drag one element onto another."""
        return json.dumps(
            session.drag(source_selector=source_selector, target_selector=target_selector),
            ensure_ascii=False,
        )

    @mcp.tool()
    def press_key(key: str, selector: str | None = None) -> str:
        """Press a key or combination ("Control+A", "Shift+Enter"), optionally on an element."""
        return json.dumps(session.press_key(key, selector), ensure_ascii=False)

    @mcp.tool()
    def fill_form(fields_json: str) -> str:
        """Fill several fields in one call: a JSON list of {selector|intent, value, action?}."""
        try:
            fields = json.loads(fields_json)
        except json.JSONDecodeError as error:
            return json.dumps({"error": f"fields_json is not JSON: {error}"})
        return json.dumps(session.fill_form(fields), ensure_ascii=False)

    @mcp.tool()
    def network_detail(index: int | None = None, url_contains: str | None = None) -> str:
        """Headers and body of one recorded request (newest match when filtering by URL)."""
        return json.dumps(session.network_detail(index, url_contains), ensure_ascii=False)

    @mcp.tool()
    def dialog_policy(action: str = "accept", match: str | None = None) -> str:
        """Answer dialogs from now on: the default (match=None) or one whose text matches."""
        return json.dumps(session.dialog_policy(action, match), ensure_ascii=False)

    @mcp.tool()
    def resize(width: int, height: int) -> str:
        """Resize the browser viewport."""
        return json.dumps(session.resize(width, height), ensure_ascii=False)

    @mcp.tool()
    def emulate(
        color_scheme: str | None = None,
        reduced_motion: str | None = None,
        forced_colors: str | None = None,
        media: str | None = None,
        geolocation: str | None = None,
        offline: bool | None = None,
    ) -> str:
        """Emulate media, geolocation ("lat,lon") and connectivity."""
        return json.dumps(
            session.emulate(
                color_scheme=color_scheme,
                reduced_motion=reduced_motion,
                forced_colors=forced_colors,
                media=media,
                geolocation=geolocation,
                offline=offline,
            ),
            ensure_ascii=False,
        )

    @mcp.tool()
    def route(
        pattern: str,
        status: int = 200,
        body: str | None = None,
        content_type: str = "application/json",
        abort: bool = False,
    ) -> str:
        """Stub or block requests matching a URL pattern (testing; not part of a trace)."""
        return json.dumps(
            session.route(
                pattern, status=status, body=body, content_type=content_type, abort=abort
            ),
            ensure_ascii=False,
        )

    @mcp.tool()
    def unroute(pattern: str | None = None) -> str:
        """Remove one route stub, or all of them."""
        return json.dumps(session.unroute(pattern), ensure_ascii=False)

    @mcp.tool()
    def perf_metrics() -> str:
        """Chromium performance counters for the current page (CDP)."""
        return json.dumps(session.perf_metrics(), ensure_ascii=False)

    @mcp.tool()
    def heap_snapshot(path: str | None = None) -> str:
        """Write a Chromium heap snapshot to a file."""
        return json.dumps(session.heap_snapshot(path), ensure_ascii=False)

    @mcp.tool()
    def lighthouse(
        url: str | None = None, categories: str = "performance,accessibility,best-practices,seo"
    ) -> str:
        """Run Lighthouse (through npx) against the current or given URL and return the scores."""
        return json.dumps(session.lighthouse(url, categories=categories), ensure_ascii=False)

    @mcp.tool()
    def trace_start(screenshots: bool = True) -> str:
        """Start a Playwright trace (open it later with `npx playwright show-trace`)."""
        return json.dumps(session.trace_start(screenshots), ensure_ascii=False)

    @mcp.tool()
    def trace_stop(path: str | None = None) -> str:
        """Stop tracing and write the trace zip."""
        return json.dumps(session.trace_stop(path), ensure_ascii=False)

    @mcp.tool()
    def dialogs() -> str:
        """Every alert/confirm/prompt seen, with the policy that resolved it."""
        return json.dumps(session.dialogs(), ensure_ascii=False)

    @mcp.tool()
    def read_js(expression: str) -> str:
        """Evaluate a JS expression in the page and return its value (observation only)."""
        return json.dumps({"value": session.read_js(expression)}, ensure_ascii=False, default=str)

    @mcp.tool()
    def wait_for(
        text: str | None = None, selector: str | None = None, timeout_ms: int = 15000
    ) -> str:
        """Wait until text or a selector appears, then report the page."""
        return json.dumps(session.wait_for(text, selector, timeout_ms), ensure_ascii=False)

    @mcp.tool()
    def scroll(direction: str = "down", amount: int = 800) -> str:
        """Scroll the page down or up by pixels of document height."""
        return json.dumps(session.scroll(direction, amount), ensure_ascii=False)

    @mcp.tool()
    def tabs() -> str:
        """List the open pages and which one jevnav is driving."""
        return json.dumps(session.tabs(), ensure_ascii=False)

    @mcp.tool()
    def new_page(url: str | None = None) -> str:
        """Open a new tab (optionally at a URL) and drive it from now on."""
        return json.dumps(session.new_page(url), ensure_ascii=False)

    @mcp.tool()
    def select_page(index: int) -> str:
        """Drive the tab at this index (see tabs)."""
        return json.dumps(session.select_page(index), ensure_ascii=False)

    @mcp.tool()
    def close_page(index: int) -> str:
        """Close the tab at this index and keep driving a remaining one."""
        return json.dumps(session.close_page(index), ensure_ascii=False)

    @mcp.tool()
    def summary() -> str:
        """This session so far: steps, auto/review/blocked counts, cost, latency."""
        return json.dumps(session.summary(), ensure_ascii=False)

    try:
        mcp.run()
    finally:
        session.close()
    return 0


def _apply_field(page: Any, selector: str, action: str, value: Any) -> None:
    locator = page.locator(selector).first
    if action == "fill":
        locator.fill(str(value))
    elif action == "type":
        locator.type(str(value))
    elif action == "select":
        locator.select_option(str(value))
    elif action == "check":
        locator.check() if value in (True, "true", None) else locator.uncheck()
    else:
        raise ValueError(f"unknown fill_form action {action!r}")
