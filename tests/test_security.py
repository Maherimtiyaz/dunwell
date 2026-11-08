"""Security regression tests for the service.

Each test covers a failure mode found in the pre-sale audit. They are kept
separate from the behavioural suite so a regression here is obvious in CI
rather than buried in a refactor.
"""

from __future__ import annotations

import json

import pytest

from dunwell.auth import AuthService, hash_password
from dunwell.io import _csv_safe
from dunwell.mailer import DryRunMailer
from dunwell.service import Application, SECURITY_HEADERS
from dunwell.store import Store, StoreError


@pytest.fixture()
def app(tmp_path):
    store = Store(tmp_path / "security.db")
    application = Application(
        store=store,
        auth=AuthService(store),
        mailer=DryRunMailer(),
        signup_mode="open",
    )
    yield application
    store.close()


def post(app, path, payload, headers=None, ip="203.0.113.9"):
    merged = {"Content-Type": "application/json", **(headers or {})}
    return app.handle(
        "POST", path, merged, json.dumps(payload).encode("utf-8"), ip
    )


def signup(app, email="owner@security.test"):
    response = post(app, "/api/signup", {"email": email, "password": "hunter2hunter2"})
    token = response.headers["Set-Cookie"].split("dunwell_session=")[1].split(";")[0]
    return response, "dunwell_session=" + token


def error_code(response) -> str:
    return json.loads(response.body.decode("utf-8"))["error"]["code"]


def test_internal_errors_do_not_echo_exception_text(app, monkeypatch) -> None:
    """A 500 must not hand the caller a path, SQL fragment, or driver message."""
    _, cookie = signup(app)

    def boom(_account_id):
        raise RuntimeError("secret /home/user/data/dunwell.db")

    monkeypatch.setattr(app.store, "load_ledger", boom)
    response = app.handle("GET", "/api/ledger", {"Cookie": cookie})

    body = response.body.decode("utf-8")
    assert response.status == 500
    assert "secret" not in body
    assert "dunwell.db" not in body
    assert json.loads(body)["error"]["message"] == "internal error"


def test_duplicate_signup_does_not_echo_the_address(app) -> None:
    """Signup must not become an account-existence oracle with the email in it."""
    signup(app, "dup@security.test")
    response = post(
        app, "/api/signup", {"email": "dup@security.test", "password": "hunter2hunter2"}
    )

    assert response.status == 409
    assert error_code(response) == "account_exists"
    assert "dup@security.test" not in response.body.decode("utf-8")


def test_cross_origin_write_with_a_cookie_is_refused(app) -> None:
    _, cookie = signup(app)
    response = app.handle(
        "POST",
        "/api/ledger",
        {
            "Cookie": cookie,
            "Origin": "https://evil.test",
            "Content-Type": "application/json",
        },
        b'{"demo": true}',
    )

    assert response.status == 403
    assert error_code(response) == "forbidden_origin"


def test_same_origin_write_with_a_cookie_is_allowed(app) -> None:
    _, cookie = signup(app)
    response = app.handle(
        "POST",
        "/api/ledger",
        {
            "Cookie": cookie,
            "Origin": "http://127.0.0.1",
            "Host": "127.0.0.1",
            "Content-Type": "application/json",
        },
        b'{"demo": true}',
    )

    assert response.status == 200


def test_cross_origin_non_json_is_refused_before_parsing(app) -> None:
    """A "simple" text/plain request must not skip the CORS preflight."""
    response = app.handle(
        "POST",
        "/api/waitlist",
        {"Origin": "https://evil.test", "Content-Type": "text/plain"},
        b'{"email": "spam@evil.test"}',
    )

    assert response.status == 415
    assert error_code(response) == "unsupported_media_type"


def test_api_responses_are_not_cached(app) -> None:
    response = app.handle("GET", "/api/waitlist/count")
    assert response.headers["Cache-Control"] == "no-store"


def test_security_headers_cover_clickjacking_and_csp() -> None:
    names = {name for name, _ in SECURITY_HEADERS}
    assert {
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
        "Content-Security-Policy",
    } <= names


def test_profile_fields_cannot_inject_a_header(app) -> None:
    response = post(
        app,
        "/api/signup",
        {
            "email": "profile@security.test",
            "password": "hunter2hunter2",
            "company_name": "Acme\r\nBcc: evil@test.example",
        },
    )

    assert response.status == 200
    account_id = json.loads(response.body)["account"]["id"]
    company = app.auth.profile(account_id)["company_name"]
    assert "\r" not in company and "\n" not in company


def test_overlong_passwords_are_refused(app) -> None:
    response = post(
        app,
        "/api/signup",
        {"email": "long@security.test", "password": "x" * 2000},
    )

    assert response.status == 400
    assert "too long" in response.body.decode("utf-8")


def test_session_cookie_is_secure_on_a_public_host(app) -> None:
    response = post(
        app,
        "/api/signup",
        {"email": "public@security.test", "password": "hunter2hunter2"},
        {"Host": "app.dunwell.example"},
    )

    assert "Secure" in response.headers["Set-Cookie"]


def test_session_cookie_is_not_secure_on_localhost(app) -> None:
    response = post(
        app,
        "/api/signup",
        {"email": "local@security.test", "password": "hunter2hunter2"},
        {"Host": "127.0.0.1:8906"},
    )

    assert "Secure" not in response.headers["Set-Cookie"]


def test_stored_duplicate_error_does_not_include_the_address(tmp_path) -> None:
    store = Store(tmp_path / "store.db")
    try:
        encoded = hash_password("hunter2hunter2")
        store.create_account("a@security.test", encoded)
        with pytest.raises(StoreError) as caught:
            store.create_account("a@security.test", encoded)
        assert "a@security.test" not in str(caught.value)
    finally:
        store.close()


def test_one_account_cannot_read_or_touch_another_ledger(app) -> None:
    """Every store query is scoped by account_id; prove it at the HTTP layer."""
    _, owner = signup(app, "owner-a@security.test")
    imported = app.handle(
        "POST",
        "/api/ledger",
        {"Cookie": owner, "Content-Type": "application/json"},
        b'{"demo": true}',
    )
    assert imported.status == 200

    _, stranger = signup(app, "owner-b@security.test")
    ledger = app.handle("GET", "/api/ledger", {"Cookie": stranger})
    assert json.loads(ledger.body)["invoices"] == 0

    report = app.handle("GET", "/api/report", {"Cookie": stranger})
    assert report.status == 400
    assert error_code(report) == "empty_ledger"

    send = app.handle(
        "POST",
        "/api/drafts/i1/send",
        {"Cookie": stranger, "Content-Type": "application/json"},
        b"{}",
    )
    assert send.status == 404
    assert error_code(send) == "unknown_invoice"


def test_csv_export_neutralises_formulas() -> None:
    assert _csv_safe("=HYPERLINK(\"http://evil\")") == "'=HYPERLINK(\"http://evil\")"
    assert _csv_safe("+1") == "'+1"
    assert _csv_safe("@cmd") == "'@cmd"
    assert _csv_safe("Acme Studio") == "Acme Studio"
