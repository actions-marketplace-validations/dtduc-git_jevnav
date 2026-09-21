"""An ordinary test: the only difference is that targets are intents.

    DEMO_PASSWORD=... pytest --jev-trace-dir=traces
    DEMO_PASSWORD=... jevnav replay --execute traces/test_sign_in.trace.jsonl

The `jev` fixture ships with the package (a pytest plugin entry point), so this
file needs no import and no conftest.
"""

from pathlib import Path

APP = (Path(__file__).parent / "app.html").as_uri()


def test_sign_in(jev):
    jev.goto(APP)
    # Short intents calibrate better: measured on this page, "the email field on
    # the sign-in form" lands at p=0.84 (below the 0.9 gate) while "the email
    # address" lands at p=0.96. Be specific, not wordy.
    jev.fill("the email address", "demo@example.com")
    jev.fill("the password field", "${DEMO_PASSWORD}")
    jev.click("the sign-in button")
    jev.expect("#welcome")
