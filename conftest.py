import sys
from pathlib import Path

import pytest

# Make the package importable when running pytest from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _open_signup(monkeypatch):
    """Run the suite with self-serve signup enabled.

    The deployed default is ``closed`` - the signup route is unauthenticated,
    and an account can send mail - but almost every service test creates its
    account over HTTP. Enabling it here keeps those tests about their own
    subject instead of about the gate.

    The gate itself is covered explicitly in ``tests/test_hardening.py``,
    which passes the mode to ``Application`` rather than relying on the
    environment.
    """
    monkeypatch.setenv("DUNWELL_SIGNUP_MODE", "open")
