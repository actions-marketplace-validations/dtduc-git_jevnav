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

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

DEFAULT_VIEWPORT = {"width": 1280, "height": 900}


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
def browser_session(
    *,
    headed: bool = False,
    user_data_dir: str | None = None,
    cdp: str | None = None,
    viewport: dict[str, int] | None = None,
) -> Iterator[Any]:
    """Yield a page from a fresh browser, a persistent profile, or an attached Chrome."""
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
                yield page
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
                yield page
            finally:
                context.close()
            return
        browser = playwright.chromium.launch(headless=not headed)
        try:
            context = browser.new_context(viewport=viewport)
            yield context.new_page()
        finally:
            browser.close()
    finally:
        manager.__exit__(None, None, None)
