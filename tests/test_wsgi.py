"""The WSGI bridge, exercised the way a managed host will call it.

``test_service.py`` calls ``Application.handle`` directly, which proves the
routes but not the transport. These tests drive the ``WsgiAdapter`` with a raw
environ, which is what PythonAnywhere, Gunicorn, and uWSGI hand the app — so a
mistake in header flattening or body framing shows up here rather than in
production.
"""

from __future__ import annotations

import io
import json
from datetime import date

import pytest

from dunwell.auth import AuthService
from dunwell.mailer import DryRunMailer
from dunwell.service import Application, RateLimiter
from dunwell.store import Store
from dunwell.wsgi import WsgiAdapter, client_ip, create_app

AS_OF = date(2026, 10, 16)


@pytest.fixture()
def app(tmp_path):
    store = Store(tmp_path / "wsgi.db")
    application = Application(
        store=store,
        auth=AuthService(store),
        mailer=DryRunMailer(),
        company=None,
        now=lambda: AS_OF,
    )
    yield application
    store.close()


def call(
    app: Application,
    method: str = "GET",
    path: str = "/",
    *,
    query: str = "",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    remote: str = "127.0.0.1",
) -> tuple[str, dict[str, str], bytes]:
    """Invoke the adapter the way a WSGI server would, and capture the reply."""
    captured: dict[str, object] = {}

    def start_response(status, response_headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = response_headers

    environ: dict[str, object] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "REMOTE_ADDR": remote,
        "wsgi.input": io.BytesIO(body),
        "CONTENT_LENGTH": str(len(body)) if body else "",
    }
    for key, value in (headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value

    chunks = WsgiAdapter(app)(environ, start_response)
    return (
        str(captured["status"]),
        dict(captured["headers"]),  # type: ignore[arg-type]
        b"".join(chunks),
    )


# ---------------------------------------------------------------------- #
# Happy path
# ---------------------------------------------------------------------- #


def test_health_answers_with_a_complete_wsgi_response(app) -> None:
    status, headers, body = call(app, "GET", "/health")
    assert status.startswith("200")
    assert json.loads(body) == {"ok": True, "status": "up"}
    # Without a Content-Length the server must chunk or close; either way the
    # client cannot tell how much body to expect.
    assert headers["Content-Length"] == str(len(body))
    assert headers["Content-Type"].startswith("application/json")


def test_query_string_is_reassembled(app) -> None:
    status, _, body = call(app, "GET", "/api/waitlist/count", query="")
    assert status.startswith("200")
    assert json.loads(body)["ok"] is True


def test_waitlist_signup_reads_the_request_body(app) -> None:
    payload = b'{"email":"wsgi@test.example","source":"landing"}'
    status, _, body = call(
        app,
        "POST",
        "/api/waitlist",
        headers={"Content-Type": "application/json"},
        body=payload,
    )
    assert status.startswith("200")
    assert json.loads(body)["ok"] is True
    assert app.store.waitlist_count() == 1


# ---------------------------------------------------------------------- #
# CORS, which a separately hosted landing page depends on
# ---------------------------------------------------------------------- #


def test_preflight_is_bodyless_without_a_content_length(app) -> None:
    app.cors_origins = frozenset({"https://dunwell.app"})
    status, headers, body = call(
        app,
        "OPTIONS",
        "/api/waitlist",
        headers={
            "Origin": "https://dunwell.app",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert status.startswith("204")
    assert body == b""
    # PEP 3333: a 204 must not be framed with a body.
    assert "Content-Length" not in headers
    assert headers["Access-Control-Allow-Origin"] == "https://dunwell.app"


def test_signup_carries_the_cors_header(app) -> None:
    app.cors_origins = frozenset({"https://dunwell.app"})
    status, headers, _ = call(
        app,
        "POST",
        "/api/waitlist",
        headers={"Origin": "https://dunwell.app", "Content-Type": "application/json"},
        body=b'{"email":"cors-wsgi@test.example"}',
    )
    assert status.startswith("200")
    assert headers["Access-Control-Allow-Origin"] == "https://dunwell.app"


# ---------------------------------------------------------------------- #
# Headers the proxy adds
# ---------------------------------------------------------------------- #


def test_forwarded_proto_marks_the_session_cookie_secure(app) -> None:
    """Managed hosts terminate TLS at the edge; the socket is plain HTTP."""
    status, headers, _ = call(
        app,
        "POST",
        "/api/signup",
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-Proto": "https",
        },
        body=json.dumps(
            {"email": "secure@test.example", "password": "testpass123"}
        ).encode(),
    )
    assert status.startswith("200")
    assert "Secure" in headers["Set-Cookie"]


def test_cookie_header_reaches_the_application(app) -> None:
    _, headers, _ = call(
        app,
        "POST",
        "/api/signup",
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            {"email": "session@test.example", "password": "testpass123"}
        ).encode(),
    )
    cookie = headers["Set-Cookie"].split(";")[0]

    status, _, body = call(app, "GET", "/api/me", headers={"Cookie": cookie})
    assert status.startswith("200")
    assert json.loads(body)["account"]["email"] == "session@test.example"


# ---------------------------------------------------------------------- #
# Client address: the rate limiter keys on it
# ---------------------------------------------------------------------- #


def test_client_ip_prefers_the_first_forwarded_address() -> None:
    environ = {
        "REMOTE_ADDR": "10.0.0.1",
        "HTTP_X_FORWARDED_FOR": "203.0.113.7, 10.0.0.1",
    }
    assert client_ip(environ) == "203.0.113.7"


def test_client_ip_falls_back_to_the_socket_address() -> None:
    assert client_ip({"REMOTE_ADDR": "10.0.0.1"}) == "10.0.0.1"
    assert client_ip({}) == "127.0.0.1"


def test_rate_limit_counts_the_real_client_not_the_proxy(app) -> None:
    """Two visitors behind one proxy must not share a single budget."""
    app.waitlist_limiter = RateLimiter(1, 60)
    body = b'{"email":"shared@test.example"}'

    first = call(
        app,
        "POST",
        "/api/waitlist",
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-For": "203.0.113.7",
        },
        body=body,
    )
    second = call(
        app,
        "POST",
        "/api/waitlist",
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-For": "203.0.113.8",
        },
        body=body,
    )
    exhausted = call(
        app,
        "POST",
        "/api/waitlist",
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-For": "203.0.113.7",
        },
        body=body,
    )

    assert first[0].startswith("200")
    assert second[0].startswith("200")
    assert exhausted[0].startswith("429")


# ---------------------------------------------------------------------- #
# Environment wiring
# ---------------------------------------------------------------------- #


def test_create_app_uses_the_configured_database(monkeypatch, tmp_path) -> None:
    db = tmp_path / "configured.db"
    monkeypatch.setenv("DUNWELL_DB", str(db))
    application = create_app()
    try:
        assert db.exists(), "the store must be created where the env points"
        assert application.store.waitlist_count() == 0
    finally:
        application.store.close()
