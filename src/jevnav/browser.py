"""How jevnav gets a browser: fresh, a persistent profile, or your running Chrome.

Three modes, one shape (a `page`):

- default          a fresh headless Chromium, no cookies, nothing kept
- `--user-data-dir` a persistent Chromium profile — log in once, stay logged in
- `--cdp URL`       attach to a Chrome you already have open (your session,
                    your extensions) over the DevTools protocol

Attaching never closes your browser and never reads your profile off disk: it
talks to the running instance, which is the only supported way to use a profile
Chrome has locked.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
ENGINES = ("chromium", "firefox", "webkit")
RING = 200  # console messages / network requests kept per page


class PageRecorder:
    """What happened on the page, for the agent to inspect afterwards.

    Console messages, network requests and page errors land in ring buffers, and
    dialogs are recorded with the policy that resolved them. This is observation
    only: it never feeds a decision and never reaches a trace's replay path.
    """

    def __init__(
        self,
        page: Any,
        *,
        dialog_policy: str = "dismiss",
        network_seq: Any = None,
    ) -> None:
        self.console: deque[dict[str, Any]] = deque(maxlen=RING)
        self.network: deque[dict[str, Any]] = deque(maxlen=RING)
        # shared across tabs when the BrowserThread supplies one
        self._network_seq = network_seq if network_seq is not None else itertools.count(1)
        self.dialogs: deque[dict[str, Any]] = deque(maxlen=20)
        self.dialog_policy = dialog_policy
        self.dialog_rules: list[tuple[str, str]] = []  # (message substring, accept|dismiss)
        self.pending_dialog: Any = None
        self.pending_deadline: float = 0.0
        page.on("console", self._console)
        page.on("pageerror", self._pageerror)
        page.on("response", self._response)
        page.on("requestfailed", self._failed)
        page.on("dialog", self._dialog)

    def _console(self, message: Any) -> None:
        self.console.append({"type": message.type, "text": message.text[:500]})

    def _pageerror(self, error: Any) -> None:
        self.console.append({"type": "pageerror", "text": str(error)[:500]})

    def _next_network_id(self) -> int:
        """A monotonic id: buffer positions shift once the ring is full."""
        return next(self._network_seq)

    def _response(self, response: Any) -> None:
        request = response.request
        self.network.append(
            {
                "id": self._next_network_id(),
                "method": request.method,
                "url": request.url[:300],
                "status": response.status,
                "resource": request.resource_type,
                "request": request,
                "response": response,
            }
        )

    def _failed(self, request: Any) -> None:
        self.network.append(
            {
                "id": self._next_network_id(),
                "method": request.method,
                "url": request.url[:300],
                "status": None,
                "error": (request.failure or "")[:200],
                "resource": request.resource_type,
            }
        )

    def _dialog(self, dialog: Any) -> None:
        """Answer a dialog inside the handler, where the sync API requires it.

        Parking a dialog to ask a human later deadlocks the renderer (measured:
        the next API call never returns), so the answer comes from a rule the
        caller set in advance: first matching rule by message substring, else the
        session policy. Either way the dialog is recorded.
        """
        message = dialog.message[:300]
        action, source = self.dialog_policy, "policy"
        for substring, rule_action in self.dialog_rules:
            if substring.casefold() in message.casefold():
                action, source = rule_action, f"rule:{substring}"
                break
        record = {"type": dialog.type, "message": message, "action": action, "source": source}
        self.dialogs.append(record)
        try:
            if action == "accept":
                dialog.accept()
            else:
                dialog.dismiss()
        except Exception:  # already handled by the page
            record["action"] = "gone"

    def set_dialog_policy(self, action: str, *, match: str | None = None) -> dict[str, Any]:
        """Set the session default, or a rule for dialogs whose text matches."""
        if action not in {"accept", "dismiss"}:
            raise ValueError("action must be accept or dismiss")
        if match is None:
            self.dialog_policy = action
            return {"policy": action}
        self.dialog_rules = [(sub, act) for sub, act in self.dialog_rules if sub != match]
        self.dialog_rules.append((match, action))
        return {"rule": {"match": match, "action": action}}

    def due_dialog(self) -> Any:
        """A parked dialog whose deadline passed is dismissed, not left hanging."""
        if self.pending_dialog is None:
            return None
        if time.monotonic() < self.pending_deadline:
            return self.pending_dialog
        dialog, self.pending_dialog = self.pending_dialog, None
        self.dialogs.append(
            {"type": dialog.type, "message": dialog.message[:300], "action": "timeout-dismiss"}
        )
        try:
            dialog.dismiss()
        except Exception:
            pass
        return None

    def resolve_dialog(self, action: str, text: str | None = None) -> dict[str, Any]:
        dialog, self.pending_dialog = self.pending_dialog, None
        if dialog is None:
            return {"handled": False, "reason": "no dialog is waiting"}
        if action == "accept" and dialog.type == "prompt" and text is not None:
            dialog.accept(text)
        elif action == "accept":
            dialog.accept()
        else:
            dialog.dismiss()
        record = {
            "handled": True,
            "type": dialog.type,
            "message": dialog.message[:300],
            "action": action,
        }
        self.dialogs.append(record)
        return record

    def console_tail(self, limit: int = 20, only_errors: bool = False) -> list[dict[str, Any]]:
        items = list(self.console)
        if only_errors:
            items = [item for item in items if item["type"] in {"error", "warning", "pageerror"}]
        return items[-limit:]

    def network_tail(self, limit: int = 20, only_failed: bool = False) -> list[dict[str, Any]]:
        """Serializable view of the newest requests, each with a stable id.

        The recorder keeps the Playwright request/response objects for
        network_detail; they never leave this method (the MCP layer json-dumps
        what it returns). The id is monotonic, so it stays valid in a later
        network_detail call even after the ring buffer drops old entries.
        """
        entries = list(self.network)
        if only_failed:
            entries = [e for e in entries if e["status"] is None or e["status"] >= 400]
        fields = ("id", "method", "url", "status", "resource", "error")
        return [{key: entry[key] for key in fields if key in entry} for entry in entries[-limit:]]

    def network_detail(
        self, *, id: int | None = None, url_contains: str | None = None
    ) -> dict[str, Any]:
        """Headers and (text) body for one recorded request, newest match first."""
        entries = list(self.network)
        if not entries:
            raise RuntimeError("no requests recorded yet")
        entry = None
        if id is not None:
            for candidate in reversed(entries):
                if candidate.get("id") == id:
                    entry = candidate
                    break
            if entry is None:
                raise RuntimeError(f"no recorded request with id {id}")
        elif url_contains:
            for candidate in reversed(entries):
                if url_contains in candidate["url"]:
                    entry = candidate
                    break
            if entry is None:
                raise RuntimeError(f"no recorded request matches {url_contains!r}")
        else:
            entry = entries[-1]
        request, response = entry.get("request"), entry.get("response")
        detail: dict[str, Any] = {
            "url": entry["url"],
            "method": entry["method"],
            "status": entry["status"],
            "resource": entry["resource"],
            "request_headers": dict(request.all_headers()) if request else {},
            "response_headers": dict(response.all_headers()) if response else {},
        }
        if request is not None and request.post_data:
            detail["post_data"] = request.post_data[:4000]
        if response is not None:
            try:
                body = response.text()
                detail["body"] = body[:4000]
                detail["body_truncated"] = len(body) > 4000
            except Exception as error:  # binary or gone
                detail["body"] = None
                detail["body_note"] = f"{type(error).__name__}: {error}"[:120]
        return detail

    def summary(self) -> dict[str, Any]:
        errors = self.console_tail(999, only_errors=True)
        failed = self.network_tail(999, only_failed=True)
        return {
            "console": {"total": len(self.console), "errors": len(errors)},
            "network": {"total": len(self.network), "failed": len(failed)},
            "dialogs": list(self.dialogs),
        }


def attach(page: Any, *, dialog_policy: str = "dismiss", network_seq: Any = None) -> PageRecorder:
    return PageRecorder(page, dialog_policy=dialog_policy, network_seq=network_seq)


def pick_attached_page(context: Any) -> Any:
    """Prefer the page the human is looking at, else whatever exists, else new."""
    pages = [
        page for page in context.pages if not page.url.startswith(("devtools://", "chrome://"))
    ]
    web_pages = [page for page in pages if page.url.startswith(("http://", "https://", "file://"))]
    if web_pages:
        return web_pages[-1]
    if pages:
        return pages[-1]
    return context.new_page()


@contextmanager
def browser_and_recorder(
    *,
    headed: bool = False,
    user_data_dir: str | None = None,
    cdp: str | None = None,
    viewport: dict[str, int] | None = None,
    dialog_policy: str = "dismiss",
    network_seq: Any = None,
    engine: str = "chromium",
    locale: str | None = None,
    timezone: str | None = None,
    user_agent: str | None = None,
) -> Iterator[tuple[Any, PageRecorder]]:
    """Yield (page, recorder) from a fresh browser, a profile, or an attached Chrome."""
    from playwright.sync_api import sync_playwright

    if engine not in ENGINES:
        raise ValueError(f"unknown browser engine {engine!r} (expected one of {list(ENGINES)})")
    if cdp and engine != "chromium":
        raise ValueError(
            "CDP attach only exists for chromium; drop --cdp or use --browser chromium"
        )
    viewport = viewport or DEFAULT_VIEWPORT
    context_options = {"viewport": viewport}
    if locale:
        context_options["locale"] = locale
    if timezone:
        context_options["timezone_id"] = timezone
    if user_agent:
        context_options["user_agent"] = user_agent
    manager = sync_playwright()
    playwright = manager.__enter__()
    browser_type = getattr(playwright, engine)
    try:
        if cdp:
            browser = playwright.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = pick_attached_page(context)
            try:
                yield (
                    page,
                    attach(page, dialog_policy=dialog_policy, network_seq=network_seq),
                )
            finally:
                browser.close()  # disconnects; the browser you attached to keeps running
            return
        if user_data_dir:
            context = browser_type.launch_persistent_context(
                user_data_dir,
                headless=not headed,
                viewport=viewport,
                args=["--no-first-run", "--no-default-browser-check"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                yield (
                    page,
                    attach(page, dialog_policy=dialog_policy, network_seq=network_seq),
                )
            finally:
                context.close()
            return
        browser = browser_type.launch(headless=not headed)
        try:
            context = browser.new_context(**context_options)
            page = context.new_page()
            yield (
                page,
                attach(page, dialog_policy=dialog_policy, network_seq=network_seq),
            )
        finally:
            browser.close()
    finally:
        manager.__exit__(None, None, None)


@contextmanager
def browser_session(**kwargs: Any) -> Iterator[Any]:
    """`browser_and_recorder`, for callers that only want the page."""
    with browser_and_recorder(**kwargs) as (page, _recorder):
        yield page
