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

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
RING = 200  # console messages / network requests kept per page


class PageRecorder:
    """What happened on the page, for the agent to inspect afterwards.

    Console messages, network requests and page errors land in ring buffers, and
    dialogs are recorded with the policy that resolved them. This is observation
    only: it never feeds a decision and never reaches a trace's replay path.
    """

    def __init__(self, page: Any, *, dialog_policy: str = "dismiss") -> None:
        self.console: deque[dict[str, Any]] = deque(maxlen=RING)
        self.network: deque[dict[str, Any]] = deque(maxlen=RING)
        self.dialogs: deque[dict[str, Any]] = deque(maxlen=20)
        self.dialog_policy = dialog_policy
        page.on("console", self._console)
        page.on("pageerror", self._pageerror)
        page.on("response", self._response)
        page.on("requestfailed", self._failed)
        page.on("dialog", self._dialog)

    def _console(self, message: Any) -> None:
        self.console.append({"type": message.type, "text": message.text[:500]})

    def _pageerror(self, error: Any) -> None:
        self.console.append({"type": "pageerror", "text": str(error)[:500]})

    def _response(self, response: Any) -> None:
        request = response.request
        self.network.append(
            {
                "method": request.method,
                "url": request.url[:300],
                "status": response.status,
                "resource": request.resource_type,
            }
        )

    def _failed(self, request: Any) -> None:
        self.network.append(
            {
                "method": request.method,
                "url": request.url[:300],
                "status": None,
                "error": (request.failure or "")[:200],
                "resource": request.resource_type,
            }
        )

    def _dialog(self, dialog: Any) -> None:
        record = {
            "type": dialog.type,
            "message": dialog.message[:300],
            "action": self.dialog_policy,
        }
        self.dialogs.append(record)
        try:
            if self.dialog_policy == "accept":
                dialog.accept()
            else:
                dialog.dismiss()
        except Exception:  # already handled by the page
            record["action"] = "gone"

    def console_tail(self, limit: int = 20, only_errors: bool = False) -> list[dict[str, Any]]:
        items = list(self.console)
        if only_errors:
            items = [item for item in items if item["type"] in {"error", "warning", "pageerror"}]
        return items[-limit:]

    def network_tail(self, limit: int = 20, only_failed: bool = False) -> list[dict[str, Any]]:
        items = list(self.network)
        if only_failed:
            items = [item for item in items if item["status"] is None or item["status"] >= 400]
        return items[-limit:]

    def summary(self) -> dict[str, Any]:
        errors = self.console_tail(999, only_errors=True)
        failed = self.network_tail(999, only_failed=True)
        return {
            "console": {"total": len(self.console), "errors": len(errors)},
            "network": {"total": len(self.network), "failed": len(failed)},
            "dialogs": list(self.dialogs),
        }


def attach(page: Any, *, dialog_policy: str = "dismiss") -> PageRecorder:
    return PageRecorder(page, dialog_policy=dialog_policy)


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
) -> Iterator[tuple[Any, PageRecorder]]:
    """Yield (page, recorder) from a fresh browser, a profile, or an attached Chrome."""
    from playwright.sync_api import sync_playwright

    viewport = viewport or DEFAULT_VIEWPORT
    manager = sync_playwright()
    playwright = manager.__enter__()
    try:
        if cdp:
            browser = playwright.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = pick_attached_page(context)
            try:
                yield page, attach(page, dialog_policy=dialog_policy)
            finally:
                browser.close()  # disconnects; the browser you attached to keeps running
            return
        if user_data_dir:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=not headed,
                viewport=viewport,
                args=["--no-first-run", "--no-default-browser-check"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                yield page, attach(page, dialog_policy=dialog_policy)
            finally:
                context.close()
            return
        browser = playwright.chromium.launch(headless=not headed)
        try:
            context = browser.new_context(viewport=viewport)
            page = context.new_page()
            yield page, attach(page, dialog_policy=dialog_policy)
        finally:
            browser.close()
    finally:
        manager.__exit__(None, None, None)


@contextmanager
def browser_session(**kwargs: Any) -> Iterator[Any]:
    """`browser_and_recorder`, for callers that only want the page."""
    with browser_and_recorder(**kwargs) as (page, _recorder):
        yield page
