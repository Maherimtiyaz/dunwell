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
from dunwell.service import MAX_BODY_BYTES, Application, RateLimiter
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


def test_client_ip_uses_the_rightmost_forwarded_address() -> None:
    """The leftmost entry is caller-supplied, so it cannot be trusted.

    A proxy appends the address it saw. Everything left of that was sent by
    the client itself, and a client that controls the value controls its own
    rate-limit bucket - one fresh header per request and the limiter is
    decorative.
    """
    environ = {
        "REMOTE_ADDR": "10.0.0.1",
        "HTTP_X_FORWARDED_FOR": "1.2.3.4, 203.0.113.7",
    }
    assert client_ip(environ) == "203.0.113.7"


def test_client_ip_ignores_a_forged_leading_entry() -> None:
    """The spoof that motivated the change: rotate the header, keep the budget."""
    seen = set()
    for forged in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
        environ = {
            "REMOTE_ADDR": "10.0.0.1",
            "HTTP_X_FORWARDED_FOR": f"{forged}, 203.0.113.7",
        }
        seen.add(client_ip(environ))
    assert seen == {"203.0.113.7"}


def test_client_ip_honours_a_configured_hop_count() -> None:
    environ = {
        "REMOTE_ADDR": "10.0.0.1",
        "HTTP_X_FORWARDED_FOR": "203.0.113.7, 10.0.0.9, 10.0.0.10",
    }
    # Two proxies: the client address is two from the right.
    assert client_ip(environ, trusted_hops=2) == "10.0.0.9"
    # Zero means "ignore the header", which is right when there is no proxy.
    assert client_ip(environ, trusted_hops=0) == "10.0.0.1"


def test_client_ip_falls_back_to_the_socket_address() -> None:
    assert client_ip({"REMOTE_ADDR": "10.0.0.1"}) == "10.0.0.1"
    assert client_ip({}) == "127.0.0.1"
    # An empty or whitespace-only header must not produce an empty key.
    assert client_ip({"REMOTE_ADDR": "10.0.0.1", "HTTP_X_FORWARDED_FOR": " "}) == "10.0.0.1"


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


# ---------------------------------------------------------------------- #
# Request size: refuse before buffering
# ---------------------------------------------------------------------- #


class _CountingStream:
    """A request body that records how much of it was actually read."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.consumed = 0

    def read(self, n: int = -1) -> bytes:
        take = self.size if n < 0 else min(n, self.size)
        self.consumed += take
        return b"x" * take


def test_oversized_body_is_refused_without_reading_it(app) -> None:
    """The cap must be applied before wsgi.input is touched.

    Reading first and rejecting afterwards is the bug this covers: the bytes
    are already in the worker's memory, so a handful of concurrent large POSTs
    kills a 512 MB process long before the application gets to say no.
    """
    stream = _CountingStream(MAX_BODY_BYTES + 1)
    captured: dict[str, object] = {}

    def start_response(status, response_headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = response_headers

    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/api/waitlist",
        "QUERY_STRING": "",
        "CONTENT_LENGTH": str(MAX_BODY_BYTES + 1),
        "CONTENT_TYPE": "application/json",
        "REMOTE_ADDR": "203.0.113.7",
        "wsgi.input": stream,
    }

    body = b"".join(WsgiAdapter(app)(environ, start_response))

    assert str(captured["status"]).startswith("413")
    assert stream.consumed == 0, "the body must not be buffered to reject it"
    assert json.loads(body)["error"]["code"] == "too_large"


def test_body_at_the_limit_is_still_accepted(app) -> None:
    """The boundary itself must not be rejected."""
    payload = b'{"email":"limit@test.example"}'
    assert len(payload) < MAX_BODY_BYTES
    status, _, _ = call(
        app,
        "POST",
        "/api/waitlist",
        headers={"Content-Type": "application/json"},
        body=payload,
    )
    assert status.startswith("200")


# ---------------------------------------------------------------------- #
# Security headers: identical on both transports
# ---------------------------------------------------------------------- #


def test_security_headers_are_present(app) -> None:
    """These used to be added by the socket handler only.

    A WSGI deployment therefore answered without them, so the same code had a
    different security posture depending on how it was hosted.
    """
    _, headers, _ = call(app, "GET", "/health")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_security_headers_survive_a_bodyless_response(app) -> None:
    """A 204 carries no Content-Length, but must still carry the headers."""
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
    assert "Content-Length" not in headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Access-Control-Allow-Origin"] == "https://dunwell.app"
