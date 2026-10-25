"""WSGI entry point, for hosts that hand you a request instead of a socket.

PythonAnywhere, Gunicorn, uWSGI, and most managed Python hosts speak WSGI.
The service core is a stdlib ``BaseHTTPRequestHandler`` (see
``dunwell.service``), because that is the zero-dependency way to run it
locally. This module translates WSGI into ``Application.handle`` and back, so
the *same* application object, the same routes, and the same tests cover both
transports.

Point a host at ``dunwell.wsgi:application``::

    # gunicorn
    gunicorn dunwell.wsgi:application

PythonAnywhere imports it from a WSGI config file instead; see
``deploy/pythonanywhere_wsgi.py`` for a ready-to-paste example.

Configuration comes from the environment, exactly as in ``dunwell.serve``:

===============================  ===========================================
``DUNWELL_DB``                   SQLite file (needs a persistent disk)
``DUNWELL_STATIC_DIR``           landing page directory, if serving one
``DUNWELL_CORS_ORIGINS``         origins allowed to post the waitlist
``DUNWELL_COMPANY_NAME`` etc.    customer-visible draft copy
===============================  ===========================================
"""

from __future__ import annotations

import os
import threading
from http import HTTPStatus
from typing import Any, Callable, Iterable

from .service import (
    MAX_BODY_BYTES,
    SECURITY_HEADERS,
    Application,
    error_response,
)
from .serve import build_app

# Statuses that must never carry a body or a Content-Length.
_BODYLESS = frozenset({204, 304})

# Hop-by-hop headers are the transport's business, not the application's.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def create_app() -> Application:
    """Build the application from the environment, once per process."""
    db_path = os.environ.get("DUNWELL_DB", "dunwell.db")
    static_dir = os.environ.get("DUNWELL_STATIC_DIR") or None
    return build_app(db_path, static_dir=static_dir)


def _trusted_hops() -> int:
    """How many reverse proxies sit in front of the app (default 1)."""
    raw = (os.environ.get("DUNWELL_TRUSTED_PROXY_HOPS") or "1").strip()
    try:
        hops = int(raw)
    except ValueError:
        return 1
    return hops if hops >= 0 else 1


def client_ip(environ: dict[str, Any], trusted_hops: int | None = None) -> str:
    """The caller's address, honouring the proxy header when present.

    Managed hosts terminate TLS at their edge and forward the real client
    address. Using it matters: the waitlist and login rate limiters key on it,
    so a shared proxy address would let one visitor exhaust everyone's budget.

    The address is read from the **right** of ``X-Forwarded-For``, not the
    left. A proxy appends the address it saw, so the rightmost entry is the
    one our own proxy observed; everything to its left was supplied by the
    caller and is free to be forged. Taking the leftmost entry let a client
    pick its own rate-limit bucket by sending a fresh header per request.

    ``DUNWELL_TRUSTED_PROXY_HOPS`` says how many proxies to skip, for a stack
    deeper than one (CDN in front of a load balancer, say). Set it to 0 to
    ignore the header entirely and trust only the socket address.
    """
    hops = _trusted_hops() if trusted_hops is None else trusted_hops
    remote = environ.get("REMOTE_ADDR") or "127.0.0.1"
    if hops <= 0:
        return remote

    forwarded = environ.get("HTTP_X_FORWARDED_FOR", "")
    if not forwarded:
        return remote
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not parts:
        return remote
    # A chain shorter than the configured depth means the header is not the
    # shape we expect; the leftmost entry is then the only one that can be a
    # client address.
    index = -hops if hops <= len(parts) else 0
    return parts[index] or remote


def request_headers(environ: dict[str, Any]) -> dict[str, str]:
    """Flatten the CGI-style WSGI environ back into a header mapping."""
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            headers[key[5:].replace("_", "-").lower()] = str(value)
    # Content-Type and Content-Length are the two the CGI convention does not
    # prefix, and both matter here: JSON parsing and body framing.
    if environ.get("CONTENT_TYPE"):
        headers["content-type"] = str(environ["CONTENT_TYPE"])
    if environ.get("CONTENT_LENGTH"):
        headers["content-length"] = str(environ["CONTENT_LENGTH"])
    return headers


def request_target(environ: dict[str, Any]) -> str:
    """Reassemble the path and query that ``Application.handle`` expects."""
    path = environ.get("PATH_INFO") or "/"
    if not path.startswith("/"):
        path = "/" + path
    query = environ.get("QUERY_STRING") or ""
    return f"{path}?{query}" if query else path


def _reason(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:  # pragma: no cover - our routes only emit real codes
        return "Unknown"


class WsgiAdapter:
    """Adapt one :class:`Application` to the WSGI calling convention."""

    def __init__(self, app: Application) -> None:
        self.app = app

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: Callable[..., Any],
    ) -> Iterable[bytes]:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            length = 0

        if length > MAX_BODY_BYTES:
            # Refuse before touching wsgi.input. Reading first and rejecting
            # afterwards buffers the whole payload in the worker - the socket
            # transport checks the length before reading for exactly this
            # reason, and a 512 MB worker does not survive a few 100 MB POSTs.
            response = error_response(413, "request body too large", "too_large")
        else:
            body = b""
            if length > 0:
                stream = environ.get("wsgi.input")
                if stream is not None:
                    body = stream.read(length)

            response = self.app.handle(
                environ.get("REQUEST_METHOD", "GET"),
                request_target(environ),
                request_headers(environ),
                body,
                client_ip(environ),
            )

        headers: list[tuple[str, str]] = [
            ("Content-Type", response.content_type),
            *SECURITY_HEADERS,
        ]
        for key, value in response.headers.items():
            if key.lower() in _HOP_BY_HOP:
                continue
            headers.append((key, value))

        if response.status in _BODYLESS:
            # PEP 3333: a 204 or 304 must not carry a body, and the server is
            # told so unambiguously by omitting Content-Length.
            start_response(f"{response.status} {_reason(response.status)}", headers)
            return []

        headers.append(("Content-Length", str(len(response.body))))
        start_response(f"{response.status} {_reason(response.status)}", headers)
        return [response.body]


# ---------------------------------------------------------------------- #
# Module-level application
# ---------------------------------------------------------------------- #

_adapter: WsgiAdapter | None = None
_lock = threading.Lock()


def application(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
) -> Iterable[bytes]:
    """The callable a WSGI server imports.

    Built on first request rather than at import time, so a misconfigured
    database path surfaces as a request error the operator can read in the
    host's log, instead of an import crash that some hosts report only as
    "unhandled exception on import".
    """
    global _adapter
    if _adapter is None:
        with _lock:
            if _adapter is None:
                _adapter = WsgiAdapter(create_app())
    return _adapter(environ, start_response)
