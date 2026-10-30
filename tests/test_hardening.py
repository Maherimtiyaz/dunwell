"""Hardening tests: the signup gate and credential throttling.

These cover the two ways a public instance was open: ``/api/signup`` created
accounts for anyone who found the URL, and nothing slowed down password
guessing. Both are tested against an explicitly configured ``Application``
rather than the environment, so the suite-wide "signup open" fixture in
``conftest.py`` cannot mask a regression here.
"""

from __future__ import annotations

import json

import pytest

from dunwell.auth import AuthService
from dunwell.mailer import DryRunMailer
from dunwell.service import (
    Application,
    RateLimiter,
    signup_mode_from_env,
)
from dunwell.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "hardening.db")
    yield s
    s.close()


def make_app(store, *, signup_mode="closed", auth_limiter=None) -> Application:
    return Application(
        store=store,
        auth=AuthService(store),
        mailer=DryRunMailer(),
        signup_mode=signup_mode,
        auth_limiter=auth_limiter,
    )


def post(app, path, payload, ip="203.0.113.7"):
    return app.handle(
        "POST",
        path,
        {"content-type": "application/json"},
        json.dumps(payload).encode("utf-8"),
        ip,
    )


def error_of(response) -> dict:
    return json.loads(response.body.decode("utf-8"))["error"]


# ---------------------------------------------------------------------- #
# Signup mode resolution
# ---------------------------------------------------------------------- #


def test_signup_defaults_to_closed(monkeypatch) -> None:
    monkeypatch.delenv("DUNWELL_SIGNUP_MODE", raising=False)
    assert signup_mode_from_env() == "closed"


def test_a_typo_in_the_signup_mode_fails_closed(monkeypatch) -> None:
    """A misspelling must not be read as "open" by a fuzzy match."""
    for value in ("opne", "yes", "true", "1", ""):
        monkeypatch.setenv("DUNWELL_SIGNUP_MODE", value)
        assert signup_mode_from_env() == "closed", value


def test_signup_mode_is_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("DUNWELL_SIGNUP_MODE", " OPEN ")
    assert signup_mode_from_env() == "open"


def test_health_reports_the_signup_mode(store) -> None:
    """The dashboard reads this to decide whether to offer "Create account".

    Without it, a customer on a closed instance is shown a tab that answers
    403 - the first screen of the product would be a dead end.
    """
    def health(app):
        return json.loads(app.handle("GET", "/health", {}, b"", "203.0.113.7").body)

    assert health(make_app(store, signup_mode="closed"))["signup"] == "closed"
    assert health(make_app(store, signup_mode="open"))["signup"] == "open"
    # Liveness checks still see what they came for.
    assert health(make_app(store))["status"] == "up"


# ---------------------------------------------------------------------- #
# The gate
# ---------------------------------------------------------------------- #


def test_closed_signup_refuses_and_creates_nothing(store) -> None:
    app = make_app(store, signup_mode="closed")
    response = post(
        app, "/api/signup", {"email": "walkin@test.example", "password": "hunter2hunter2"}
    )

    assert response.status == 403
    assert error_of(response)["code"] == "signup_closed"
    assert "Set-Cookie" not in response.headers
    assert store.account_by_email("walkin@test.example") is None


def test_closed_signup_refuses_before_parsing_the_body(store) -> None:
    """A malformed body must not turn into a 400 on a closed instance.

    Parsing first would spend work on payloads that are refused anyway, and
    would leak the validation rules to someone with no way in.
    """
    app = make_app(store, signup_mode="closed")
    response = app.handle(
        "POST",
        "/api/signup",
        {"content-type": "application/json"},
        b"not json at all",
        "203.0.113.7",
    )

    assert response.status == 403
    assert error_of(response)["code"] == "signup_closed"


def test_open_signup_still_works(store) -> None:
    """The gate must be a gate, not a removal of the feature."""
    app = make_app(store, signup_mode="open")
    response = post(
        app, "/api/signup", {"email": "invited@test.example", "password": "hunter2hunter2"}
    )

    assert response.status == 200
    assert "Set-Cookie" in response.headers
    assert store.account_by_email("invited@test.example") is not None


# ---------------------------------------------------------------------- #
# Credential throttling
# ---------------------------------------------------------------------- #


def test_login_is_throttled_per_address(store) -> None:
    app = make_app(store, signup_mode="open", auth_limiter=RateLimiter(3, 60))
    post(app, "/api/signup", {"email": "owner@test.example", "password": "hunter2hunter2"})

    attempts = [
        post(app, "/api/login", {"email": "owner@test.example", "password": "wrong-pass"})
        for _ in range(4)
    ]

    assert [r.status for r in attempts[:3]] == [401, 401, 401]
    assert attempts[3].status == 429
    assert error_of(attempts[3])["code"] == "rate_limited"


def test_throttling_one_address_does_not_block_another(store) -> None:
    """An office behind one NAT must not lock out the rest of the internet."""
    app = make_app(store, signup_mode="open", auth_limiter=RateLimiter(2, 60))

    # Distinct accounts, so only the per-address key is in play.
    for email in ("a@test.example", "b@test.example"):
        post(app, "/api/login", {"email": email, "password": "nope"}, ip="203.0.113.7")
    blocked = post(
        app, "/api/login", {"email": "c@test.example", "password": "nope"}, ip="203.0.113.7"
    )
    elsewhere = post(
        app, "/api/login", {"email": "d@test.example", "password": "nope"}, ip="198.51.100.9"
    )

    assert blocked.status == 429
    assert elsewhere.status == 401


def test_one_account_cannot_be_guessed_from_many_addresses(store) -> None:
    """The per-email key is what stops a distributed guess at one account.

    Rotating the source address defeats the per-address key by design; the
    account must still be protected.
    """
    app = make_app(store, signup_mode="open", auth_limiter=RateLimiter(2, 60))
    post(app, "/api/signup", {"email": "target@test.example", "password": "hunter2hunter2"})

    results = [
        post(
            app,
            "/api/login",
            {"email": "target@test.example", "password": "guess"},
            ip=f"198.51.100.{n}",
        )
        for n in range(1, 5)
    ]

    assert results[0].status == 401
    assert results[1].status == 401
    assert results[2].status == 429
    assert results[3].status == 429


def test_waitlist_and_auth_have_separate_budgets(store) -> None:
    """Exhausting the login limiter must not stop someone joining the waitlist."""
    app = make_app(store, signup_mode="open", auth_limiter=RateLimiter(1, 60))
    app.waitlist_limiter = RateLimiter(5, 60)

    post(app, "/api/login", {"email": "x@test.example", "password": "nope"})
    blocked = post(app, "/api/login", {"email": "x@test.example", "password": "nope"})
    signup = post(app, "/api/waitlist", {"email": "new@test.example"})

    assert blocked.status == 429
    assert signup.status == 200
