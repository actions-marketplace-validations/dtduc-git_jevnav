"""jevnav diff: the mockup-vs-app comparison, deterministic and offline."""

import pytest

from jevnav.diffpage import compare, render, structure_diff, style_diff

MOCKUP = """
<html><head><title>New pricing</title></head><body style="margin:0">
<main>
  <h1 style="font-size:32px">Pricing</h1>
  <p>Three plans for every team.</p>
  <button id="cta" style="border-radius:8px">Start free</button>
  <section><h2>Enterprise</h2><button>Talk to sales</button></section>
</main></body></html>
"""

APP = """
<html><head><title>Pricing</title></head><body style="margin:0">
<main>
  <h1 style="font-size:28px">Pricing</h1>
  <button id="cta" style="border-radius:4px">Start free</button>
  <button id="extra">Book a demo</button>
</main></body></html>
"""


@pytest.fixture
def pages(tmp_path):
    mockup = tmp_path / "mockup.html"
    app = tmp_path / "app.html"
    mockup.write_text(MOCKUP)
    app.write_text(APP)
    return mockup.as_uri(), app.as_uri()


def test_an_identical_page_reports_nothing(page, tmp_path):
    target = tmp_path / "same.html"
    target.write_text(MOCKUP)
    result = compare(target.as_uri(), target.as_uri(), page=page, style_selector="h1,button")
    assert result["identical"] is True
    assert "identical" in render(result)


def test_structure_finds_missing_new_and_moved(page, pages):
    mockup, app = pages
    result = compare(mockup, app, page=page, style_selector="h1")
    kinds = {item["kind"] for item in result["structure"]}
    assert "missing" in kinds  # the paragraph and the Enterprise section
    assert "new" in kinds  # the extra button
    missing = [item for item in result["structure"] if item["kind"] == "missing"]
    assert any("Three plans" in item["element"] for item in missing)


def test_styles_finds_the_changed_values(page, pages):
    mockup, app = pages
    result = compare(mockup, app, page=page, style_selector="h1,#cta")
    changes = {
        (item["element"], item["property"]): (item["mockup"], item["app"])
        for item in result["style"]
    }
    assert any(
        prop == "font-size" and pair == ("32px", "28px") for (_el, prop), pair in changes.items()
    )
    assert any(
        prop == "border-radius" and pair == ("8px", "4px") for (_el, prop), pair in changes.items()
    )


def test_the_report_tells_a_coding_agent_what_to_change(page, pages):
    mockup, app = pages
    report = render(compare(mockup, app, page=page, style_selector="h1"))
    assert "## Structure" in report and "## Styles" in report
    assert "font-size" in report and "missing" in report


def test_a_box_tolerance_ignores_small_moves():
    a = [{"tag": "button", "text": "Go", "id": None, "name": None, "box": [10, 10, 100, 30]}]
    b = [{"tag": "button", "text": "Go", "id": None, "name": None, "box": [12, 11, 102, 31]}]
    assert structure_diff(a, b, tolerance=4) == []
    moved = structure_diff(a, b, tolerance=1)
    assert moved and moved[0]["kind"] == "moved"
    assert moved[0]["detail"].startswith("x+2")


def test_style_diff_reports_a_count_mismatch():
    a = {
        "selector": "h1",
        "elements": [
            {"element": "h1", "text": "A", "box": [1, 1], "styles": {"font-size": "32px"}}
        ],
    }
    b = {"selector": "h1", "elements": []}
    differences = style_diff("h1", a, b)
    assert differences[-1]["property"] == "count"
    assert differences[-1]["mockup"] == 1


@pytest.fixture
def cli_session(monkeypatch, page):
    from contextlib import contextmanager

    from jevnav import cli

    @contextmanager
    def session(*args, **kwargs):
        yield page

    monkeypatch.setattr(cli, "_session", session)
    return page


def test_the_cli_exits_non_zero_when_pages_differ(pages, tmp_path, capsys, cli_session):
    from jevnav import cli

    mockup, app = pages
    report = tmp_path / "diff.md"
    code = cli.main(["diff", mockup, app, "--style-selector", "h1", "--report", str(report)])
    assert code == 1
    assert "jevnav diff" in capsys.readouterr().out
    assert "## Structure" in report.read_text()


def test_the_cli_exits_zero_when_pages_match(tmp_path, capsys, cli_session):
    from jevnav import cli

    target = tmp_path / "same.html"
    target.write_text(MOCKUP)
    assert cli.main(["diff", str(target), str(target), "--json"]) == 0
    assert '"identical": true' in capsys.readouterr().out
