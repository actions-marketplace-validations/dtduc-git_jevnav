import pytest

from jevnav.trace import (
    TraceWriter,
    describe,
    describe_options,
    dom_hash,
    fingerprint,
    make_candidate,
    read_trace,
)


def test_fingerprint_normalizes_case_and_whitespace():
    assert fingerprint("Button", "  Sign\n In ") == "button|sign in"
    assert fingerprint("button", "sign in") == fingerprint("BUTTON", " Sign  in ")


def test_dom_hash_ignores_order_but_not_identity():
    a = [make_candidate("c1", "button", "Sign in"), make_candidate("c2", "link", "Home")]
    b = [make_candidate("c9", "link", "Home"), make_candidate("c4", "button", "Sign in")]
    assert dom_hash(a) == dom_hash(b)
    assert dom_hash(a) != dom_hash(a[:1])


def test_candidate_carries_its_fingerprint():
    candidate = make_candidate("c1", "textbox", "Email", scope="Sign in", placeholder="you@x")
    assert candidate["fp"] == "textbox|email"
    assert candidate["scope"] == "Sign in"


def test_describe_is_one_signal_line():
    candidate = make_candidate("c1", "link", "Pricing", href="/pricing", scope="Home")
    assert describe(candidate) == "Pricing — link → /pricing"
    assert describe(candidate, disambiguate=True) == "Pricing — link → /pricing in 'Home'"


def test_scope_is_shown_only_where_names_collide():
    unique = [make_candidate("c1", "button", "Save", scope="Settings")]
    dupes = [
        make_candidate("c1", "button", "Delete", scope="Today"),
        make_candidate("c2", "button", "Delete", scope="Later"),
        make_candidate("c3", "button", "Save", scope="Settings"),
    ]
    assert describe_options(unique)["c1"] == "Save — button"
    options = describe_options(dupes)
    assert options["c1"] == "Delete — button in 'Today'"
    assert options["c2"] == "Delete — button in 'Later'"
    assert options["c3"] == "Save — button"


def test_trace_roundtrip(tmp_path):
    path = tmp_path / "run.trace.jsonl"
    with TraceWriter(path, flow="demo", model="jev-latest") as writer:
        writer.step(step=1, intent="Sign in", candidates=[make_candidate("c1", "button", "Go")])
        writer.step(step=2, intent="Sign out", candidates=[])
    run, steps = read_trace(path)
    assert run["kind"] == "run"
    assert run["flow"] == "demo"
    assert run["spec"] == 0
    assert [s["step"] for s in steps] == [1, 2]
    assert steps[0]["candidates"][0]["fp"] == "button|go"


def test_trace_rejects_unknown_records(tmp_path):
    path = tmp_path / "bad.trace.jsonl"
    path.write_text('{"kind": "step", "step": 1, "candidates": []}\n{"kind": "wat"}\n')
    with pytest.raises(ValueError, match="unknown record kind"):
        read_trace(path)


def test_trace_requires_steps(tmp_path):
    path = tmp_path / "empty.trace.jsonl"
    path.write_text('{"kind": "run"}\n')
    with pytest.raises(ValueError, match="no steps"):
        read_trace(path)
