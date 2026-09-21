import pytest

from jevnav import page as page_module


def extract(page):
    return page_module.extract(page)


def by_name(candidates, name):
    return next(c for c in candidates if c["name"] == name)


def test_extraction_finds_roles_and_accessible_names(page, app_url):
    page.goto(app_url)
    candidates, total, dropped = extract(page)
    assert dropped == 0
    assert total == len(candidates)
    roles = {c["name"]: c["role"] for c in candidates}
    assert roles["Sign in"] == "button"
    assert roles["Pricing"] == "link"
    assert roles["Email"] == "textbox"
    assert roles["Alert email"] == "textbox"


def test_scope_separates_identical_labels(page, app_url):
    page.goto(app_url)
    candidates, _, _ = extract(page)
    email_fields = [c for c in candidates if c["role"] == "textbox"]
    assert {c["scope"] for c in email_fields} == {
        "Sign in",
        "New to Acme?",
        "Notification settings",
    }


def test_aria_label_wins_over_visible_text(page, app_url):
    page.goto(app_url)
    candidates, _, _ = extract(page)
    delete = by_name(candidates, "Delete Renew TLS certificate")
    assert delete["role"] == "button"
    assert delete["scope"] == "Today"


def test_hidden_elements_are_not_candidates(page, app_url):
    page.goto(app_url)
    page.evaluate("document.querySelector('#login-submit').style.display = 'none'")
    candidates, _, _ = extract(page)
    assert not any(c["name"] == "Sign in" for c in candidates)


def test_candidate_list_is_capped_and_reports_what_was_dropped(page, tmp_path):
    html = (
        "<html><body>"
        + "".join(f"<button>Button {i}</button>" for i in range(300))
        + "</body></html>"
    )
    path = tmp_path / "many.html"
    path.write_text(html)
    page.goto(path.as_uri())
    candidates, total, dropped = extract(page)
    assert total == 300
    assert len(candidates) == 254
    assert dropped == 46
    # the API allows 255 choices per question, and "none" takes one
    from jevnav.decide import build_question

    question = build_question("file:///x", "many", "press one", candidates)
    assert len(question["criteria"]) == 255


def test_locator_for_returns_a_standard_playwright_locator(page, app_url):
    page.goto(app_url)
    candidates, _, _ = extract(page)
    sign_in = by_name(candidates, "Sign in")
    selector, unique = page_module.locator_for(page, sign_in)
    assert selector == 'role=button[name="Sign in"]'
    assert unique is True
    assert page.locator(selector).count() == 1


def test_execute_clicks_the_chosen_element_only(page, app_url):
    page.goto(app_url)
    page.evaluate(
        "document.querySelector('#login-submit')"
        ".addEventListener('click', () => { window.clicked = 1; })"
    )
    candidates, _, _ = extract(page)
    sign_in = by_name(candidates, "Sign in")
    page_module.execute(page, sign_in["cid"], {"type": "click"}, settle_ms=0)
    assert page.evaluate("window.clicked") == 1


def test_execute_fills_by_fingerprint_not_by_position(page, app_url):
    page.goto(app_url)
    candidates, _, _ = extract(page)
    password = by_name(candidates, "Password")
    page_module.execute_fp(page, password["fp"], {"type": "fill", "value": "hunter2"}, settle_ms=0)
    assert page.input_value("#login-password") == "hunter2"


def test_execute_fp_refuses_an_ambiguous_fingerprint(page, tmp_path):
    path = tmp_path / "dupes.html"
    path.write_text("<html><body><button>Send</button><button>Send</button></body></html>")
    page.goto(path.as_uri())
    with pytest.raises(RuntimeError, match="2 candidates match"):
        page_module.execute_fp(page, "button|send", {"type": "click"}, settle_ms=0)


def test_execute_fp_refuses_a_missing_fingerprint(page, app_url):
    page.goto(app_url)
    with pytest.raises(RuntimeError, match="0 candidates match"):
        page_module.execute_fp(page, "button|gone", {"type": "click"}, settle_ms=0)


def test_extraction_reports_field_values_and_masks_passwords(page, app_url):
    page.goto(app_url)
    page.fill("#login-email", "demo@example.com")
    page.fill("#login-password", "hunter2")
    candidates, _, _ = extract(page)
    assert by_name(candidates, "Email")["value"] == "demo@example.com"
    assert by_name(candidates, "Password")["value"] == "••••"


def test_a_filled_field_is_visible_in_the_model_description(page, app_url):
    page.goto(app_url)
    page.fill("#login-email", "demo@example.com")
    candidates, _, _ = extract(page)
    from jevnav.trace import describe

    assert '[value: "demo@example.com"]' in describe(by_name(candidates, "Email"))
