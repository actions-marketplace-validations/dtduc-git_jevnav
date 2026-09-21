import pytest

from jevnav.gates import (
    AUTO,
    BLOCKED,
    REVIEW,
    default_gates,
    load_gates,
    risk_match,
    threshold_for,
    verdict,
)
from jevnav.trace import make_candidate


def decision(choice="c1", confidence=0.95, error=None):
    return {"choice": choice, "confidence": confidence, "error": error}


BUTTON = make_candidate("c1", "button", "Save")


def test_confident_harmless_decision_is_auto():
    assert verdict(
        decision(), intent="save the settings", candidate=BUTTON, dropped=0, gates=default_gates()
    ) == (
        AUTO,
        None,
    )


def test_low_confidence_goes_to_review_with_the_number():
    gates = default_gates()
    result, reason = verdict(
        decision(confidence=0.72), intent="save", candidate=BUTTON, dropped=0, gates=gates
    )
    assert result == REVIEW
    assert "0.72" in reason


def test_none_and_errors_are_blocked():
    gates = default_gates()
    assert (
        verdict(decision(choice="none"), intent="x", candidate=None, dropped=0, gates=gates)[0]
        == BLOCKED
    )
    assert (
        verdict(decision(choice=None), intent="x", candidate=None, dropped=0, gates=gates)[0]
        == BLOCKED
    )
    failed = verdict(decision(error="HTTP 500"), intent="x", candidate=None, dropped=0, gates=gates)
    assert failed[0] == BLOCKED
    assert "HTTP 500" in failed[1]


def test_risky_intent_is_review_even_at_full_confidence():
    gates = default_gates()
    result, reason = verdict(
        decision(confidence=1.0),
        intent="delete the workspace",
        candidate=BUTTON,
        dropped=0,
        gates=gates,
    )
    assert result == REVIEW
    assert "risky" in reason


def test_risky_element_name_is_review_even_when_the_intent_is_calm():
    gates = default_gates()
    delete_button = make_candidate("c2", "button", "Delete account")
    result, _ = verdict(
        decision(choice="c2", confidence=1.0),
        intent="clean up my settings",
        candidate=delete_button,
        dropped=0,
        gates=gates,
    )
    assert result == REVIEW


def test_cancel_a_form_is_not_risky_but_cancel_a_subscription_is():
    gates = default_gates()
    cancel = make_candidate("c1", "button", "Cancel")
    assert risk_match("discard the notification changes", cancel, gates) is None
    assert risk_match("cancel my subscription", cancel, gates) is not None


def test_truncation_is_a_warning_by_default_and_a_gate_when_asked():
    gates = default_gates()
    assert verdict(decision(), intent="save", candidate=BUTTON, dropped=12, gates=gates)[0] == AUTO
    gates["truncated"] = "review"
    result, reason = verdict(decision(), intent="save", candidate=BUTTON, dropped=12, gates=gates)
    assert result == REVIEW
    assert "12 candidates" in reason


def test_loop_mode_uses_its_own_lower_threshold():
    gates = default_gates()
    assert threshold_for("save", gates) == 0.9
    assert threshold_for("save", gates, default_key="loop_min_confidence") == 0.5
    assert (
        verdict(decision(confidence=0.6), intent="save", candidate=BUTTON, dropped=0, gates=gates)[
            0
        ]
        == REVIEW
    )
    assert (
        verdict(
            decision(confidence=0.6),
            intent="save",
            candidate=BUTTON,
            dropped=0,
            gates=gates,
            default_key="loop_min_confidence",
        )[0]
        == AUTO
    )


def test_an_explicit_confidence_beats_the_configured_one_but_not_risk():
    gates = default_gates()
    assert threshold_for("save", gates, override=0.7) == 0.7
    assert (
        verdict(
            decision(confidence=0.75),
            intent="save",
            candidate=BUTTON,
            dropped=0,
            gates=gates,
            min_confidence=0.7,
        )[0]
        == AUTO
    )
    risky = verdict(
        decision(confidence=1.0),
        intent="delete the account",
        candidate=BUTTON,
        dropped=0,
        gates=gates,
        min_confidence=0.1,
    )
    assert risky[0] == REVIEW


def test_per_intent_override_wins_over_the_global_threshold():
    gates = default_gates()
    gates["intents"] = {"delete the *": {"min_confidence": 0.99}}
    assert threshold_for("delete the workspace", gates) == 0.99
    assert threshold_for("save the settings", gates) == 0.9
    assert (
        verdict(
            decision(confidence=0.95),
            intent="delete the workspace",
            candidate=BUTTON,
            dropped=0,
            gates=gates,
        )[0]
        == REVIEW
    )


def test_load_gates_rejects_a_bad_truncated_value(tmp_path):
    path = tmp_path / "gates.yaml"
    path.write_text("truncated: maybe\n")
    with pytest.raises(ValueError, match="truncated"):
        load_gates(path)


def test_load_gates_rejects_unknown_keys(tmp_path):
    path = tmp_path / "gates.yaml"
    path.write_text("min_confidence: 0.8\nwat: 1\n")
    with pytest.raises(ValueError, match="unknown keys"):
        load_gates(path)


def test_load_gates_reads_overrides(tmp_path):
    path = tmp_path / "gates.yaml"
    path.write_text("min_confidence: 0.8\nrisky: []\nintents:\n  'pay *': {min_confidence: 0.99}\n")
    gates = load_gates(path)
    assert gates["min_confidence"] == 0.8
    assert gates["risky"] == []
    assert threshold_for("pay the invoice", gates) == 0.99


def test_risky_patterns_are_not_english_only():
    gates = default_gates()
    for intent in (
        "xoá tài khoản của tôi",
        "Xóa tài khoản",
        "konto löschen",
        "das abo kündigen",  # the gate study's recall gap, 2026-09-21
        "supprimer le compte",
        "eliminar la cuenta",
        "アカウントを削除",
        "删除账户",
        "계정 삭제",
    ):
        assert risk_match(intent, None, gates), intent


def test_a_caller_cannot_lower_the_bar_to_zero():
    from jevnav.gates import MIN_CONFIDENCE_FLOOR

    gates = default_gates()
    assert threshold_for("save", gates, override=0.0) == MIN_CONFIDENCE_FLOOR
    assert threshold_for("save", gates, override=0.9) == 0.9
