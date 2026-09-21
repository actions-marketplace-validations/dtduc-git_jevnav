import pytest

from jevnav import page as page_module


def extract(page, **kwargs):
    return page_module.extract(page, **kwargs)


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


def test_candidate_list_is_a_shortlist_and_reports_what_was_dropped(page, tmp_path):
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
    assert len(candidates) == 120  # the default shortlist
    assert dropped == 180
    # the API allows 255 choices per question, and "none" takes one
    from jevnav.decide import build_question

    biggest, _, _ = extract(page, limit=254)
    assert len(biggest) == 254
    question = build_question("file:///x", "many", "press one", biggest)
    assert len(question["criteria"]) == 255


def test_extraction_prefers_in_viewport_and_form_controls(page, tmp_path):
    html = (
        "<html><body>"
        + '<input aria-label="Search this site">'
        + "".join(f'<a href="/n{i}">noise {i}</a>' for i in range(60))
        + '<button style="margin-top:2000px">Far away</button>'
        + "</body></html>"
    )
    path = tmp_path / "shortlist.html"
    path.write_text(html)
    page.goto(path.as_uri())
    candidates, _, dropped = extract(page, limit=5)
    assert dropped > 0
    names = [c["name"] for c in candidates]
    assert names[0] == "Search this site"  # form control, in viewport
    assert any(name.startswith("noise") for name in names)
    assert "Far away" not in names  # off-screen, last priority


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
    page_module.execute(page, sign_in, {"type": "click"}, settle_ms=0)
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


def test_stale_stamps_cannot_steal_a_decision(page):
    """Regression for the silent wrong click: stamps must not survive an extraction.

    Reproduces the reported case: a shortlist with dropped elements, then a
    scroll changes which elements are in view, then the chosen candidate is
    executed. The click must land on the chosen element, not on a stale stamp.
    """
    page.set_viewport_size({"width": 800, "height": 600})
    page.set_content(
        '<a id=L1 href="#one" style="display:block;height:40px">Go to docs</a>'
        '<div style="height:1400px"></div>'
        "<button id=B1 onclick=\"window.clicked='B1'\">Delete account</button>"
        "<button id=B2 onclick=\"window.clicked='B2'\">Keep account</button>"
    )
    first, _, dropped_first = extract(page, limit=2)
    assert dropped_first > 0
    page.evaluate("window.scrollTo(0, 1400)")
    second, _, _ = extract(page, limit=2)
    for candidate in second:
        assert page.locator(f'[data-jevcid="{candidate["cid"]}"]').count() == 1
    chosen = by_name(second, "Delete account")
    page_module.execute(page, chosen, {"type": "click"}, settle_ms=50)
    assert page.evaluate("window.clicked") == "B1"
    assert not page.url.endswith("#one")
    assert first  # the first shortlist is unused, kept for the report


def test_executing_a_duplicated_fingerprint_refuses_loudly(page, tmp_path):
    path = tmp_path / "dupes.html"
    path.write_text("<html><body><button>Send</button><button>Send</button></body></html>")
    page.goto(path.as_uri())
    candidates, _, _ = extract(page)
    duplicated = by_name(candidates, "Send")
    with pytest.raises(RuntimeError, match="2 candidates match"):
        page_module.execute(page, duplicated, {"type": "click"}, settle_ms=0)


def test_icon_only_controls_still_get_a_name(page, tmp_path):
    path = tmp_path / "icons.html"
    path.write_text(
        "<html><body>"
        '<button title="Close dialog"></button>'
        '<a href="#home"><img alt="Home"></a>'
        "<button><svg><title>Save</title></svg></button>"
        '<a href="#x"><img alt=""></a>'
        "</body></html>"
    )
    page.goto(path.as_uri())
    candidates, _, _ = extract(page)
    names = {c["name"] for c in candidates}
    assert "Close dialog" in names  # title attribute
    assert "Home" in names  # img alt inside the link
    assert "Save" in names  # svg title inside the button
    assert len(candidates) == 3  # the empty-alt link stays out


IFRAME_AND_SHADOW = """
<html><body>
  <button>Outside</button>
  <iframe srcdoc="<button onclick=&quot;window.clicked='yes'&quot;>Pay now</button>"></iframe>
  <my-widget></my-widget>
  <script>
    class MyWidget extends HTMLElement {
      connectedCallback() {
        const root = this.attachShadow({ mode: 'open' });
        root.innerHTML = '<button id=inner>Save draft</button>';
      }
    }
    customElements.define('my-widget', MyWidget);
  </script>
</body></html>
"""


def test_controls_inside_iframes_and_shadow_roots_are_candidates(page, tmp_path):
    path = tmp_path / "frames.html"
    path.write_text(IFRAME_AND_SHADOW)
    page.goto(path.as_uri())
    page.wait_for_timeout(200)
    candidates, total, _ = extract(page)
    by_name = {c["name"]: c for c in candidates}
    assert "Outside" in by_name
    assert "Pay now" in by_name  # inside an iframe
    assert "Save draft" in by_name  # inside an open shadow root
    assert by_name["Pay now"]["frame"] != by_name["Outside"]["frame"]
    assert all(":" in c["cid"] for c in candidates)  # cids are frame-namespaced


def test_executing_acts_inside_the_right_frame(page, tmp_path):
    path = tmp_path / "frames.html"
    path.write_text(IFRAME_AND_SHADOW)
    page.goto(path.as_uri())
    page.wait_for_timeout(200)
    candidates, _, _ = extract(page)
    pay = next(c for c in candidates if c["name"] == "Pay now")
    page_module.execute(page, pay, {"type": "click"}, settle_ms=50)
    assert page.frames[pay["frame"]].evaluate("window.clicked") == "yes"


def test_the_same_name_in_two_frames_is_not_ambiguous(page, tmp_path):
    """Fingerprint plus frame is the identity; a cross-frame duplicate must not block acting."""
    path = tmp_path / "dupes.html"
    path.write_text(
        "<html><body><button>Save</button>"
        "<iframe srcdoc='<button>Save</button>'></iframe></body></html>"
    )
    page.goto(path.as_uri())
    page.wait_for_timeout(200)
    candidates, _, _ = extract(page)
    saves = [c for c in candidates if c["name"] == "Save"]
    assert len(saves) == 2 and saves[0]["frame"] != saves[1]["frame"]
    page_module.execute(page, saves[1], {"type": "click"}, settle_ms=0)  # the one inside the iframe
