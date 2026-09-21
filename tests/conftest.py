"""Shared fixtures: one browser per session, one page per test."""

from __future__ import annotations

import pytest
from helpers import fixture_url


@pytest.fixture(scope="session")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    manager = playwright.sync_playwright()
    instance = manager.__enter__()
    browser = instance.chromium.launch(headless=True)
    yield browser
    browser.close()
    manager.__exit__(None, None, None)


@pytest.fixture
def page(browser):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    yield page
    page.close()


@pytest.fixture
def app_url() -> str:
    return fixture_url("app.html")


@pytest.fixture
def mutated_url() -> str:
    return fixture_url("app.mutated.html")
