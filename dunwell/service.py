"""Dunwell service: HTTP API, session auth, and static landing in one process.

Standard library only (``http.server``). The application is split from the
transport so it can be driven either through a real socket or directly in
tests:

    app = Application(store=..., auth=..., mailer=..., static_dir=...)
    status, ctype, body, headers = app.handle(...)

Endpoints
---------
Public
    GET  /                      landing page
    GET  /privacy.html | /terms.html
    GET  /dashboard             authenticated app UI (redirects if no session)
    POST /api/waitlist          {email, source} -> {ok, count, already?}
    GET  /api/waitlist/count    -> {ok, count}
    POST /api/signup            {email, password} -> sets session cookie
    POST /api/login             {email, password} -> sets session cookie
    POST /api/logout            clears the session cookie
    GET  /api/me                -> {ok, account} or 401

Authenticated (session cookie)
    GET    /api/ledger          -> {ok, invoices, customers}
    POST   /api/ledger          {customers_csv, invoices_csv} | {demo: true}
    GET    /api/report?as_of=   -> metrics + decisions + grounded drafts
    GET    /api/drafts          -> drafts pending approval
    POST   /api/drafts/<id>/send-> deliver one draft, record the nudge
    GET    /api/nudges          -> sending history

Sending is deliberately explicit: a draft is only ever delivered by a
``POST .../send``. Nothing goes out as a side effect of reading a report.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .agent import run
from .auth import AuthError, AuthService, Session, validate_email
from .core import REASON_LABELS, Company
from .io import parse_customers_csv, parse_invoices_csv, render_markdown
from .mailer import MailMessage, Mailer
from .store import Store, StoreError

MAX_BODY_BYTES = 1_000_000
SESSION_COOKIE = "dunwell_session"
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"

# Static filenames the public server is willing to read, and nothing else.
STATIC_ALLOWLIST = frozenset(
    {"index.html", "styles.css", "privacy.html", "terms.html", "favicon.ico"}
)

_SEND_ROUTE = re.compile(r"^/api/drafts/([^/]+)/send$")

# The only routes a separately hosted landing page may call cross-origin.
_CORS_PATHS = frozenset({"/api/waitlist", "/api/waitlist/count"})

# Packaged with the code, not the landing directory: the dashboard works even
# on an API-only install with no marketing assets next to it.
DASHBOARD_PATH = Path(__file__).resolve().parent / "static" / "dashboard.html"


def default_company() -> Company:
    """The company profile baked into drafts for accounts that set none.

    Every draft body carries the name, reply-to, and payment link, so these
    values are visible to the customer. They are read from the environment
    first, because a placeholder shipping to a real customer looks like a
    phishing mail rather than a billing notice.
    """
    name = os.environ.get("DUNWELL_COMPANY_NAME", "Northwind Studio")
    return Company(
        name=name,
        reply_to=os.environ.get("DUNWELL_REPLY_TO", "billing@northwind.example"),
        signature=os.environ.get("DUNWELL_SIGNATURE", f"{name} - Billing"),
        payment_link=os.environ.get(
            "DUNWELL_PAYMENT_LINK", "https://pay.northwind.example/inv"
        ),
    )


def cors_origins_from_env() -> frozenset[str]:
    """Origins allowed to call the public waitlist routes from a browser.

    A landing page hosted separately (GitHub Pages, Netlify, a custom
    domain) posts the sign-up form across origins, which the browser only
    permits when the service answers with the right CORS headers. The list
    is explicit: an empty set means no cross-origin access at all, which is
    the safe default for an installation that serves its own landing page.
    """
    raw = os.environ.get("DUNWELL_CORS_ORIGINS", "")
    return frozenset(
        part.strip().rstrip("/") for part in raw.split(",") if part.strip()
    )


class RateLimiter:
    """Fixed-window per-key limiter, best-effort and in-memory."""

    def __init__(self, limit: int = 10, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = int(time.time())
        with self._lock:
            window_start, count = self._hits.get(key, (now, 0))
            if now - window_start >= self.window:
                window_start, count = now, 0
            count += 1
            self._hits[key] = (window_start, count)
            if len(self._hits) > 10_000:  # bound memory
                self._hits = {k: v for k, v in self._hits.items() if v[0] == window_start}
            return count <= self.limit


@dataclass
class Response:
    status: int = 200
    content_type: str = "application/json; charset=utf-8"
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


def json_response(payload: dict, status: int = 200, **headers: str) -> Response:
    return Response(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )


def error_response(status: int, message: str, code: str) -> Response:
    return json_response({"ok": False, "error": {"message": message, "code": code}}, status)


class Application:
    """Route table over the store, auth service, and mailer."""

    def __init__(
        self,
        store: Store,
        auth: AuthService,
        mailer: Mailer,
        *,
        static_dir: str | Path | None = None,
        company: Company | None = None,
        waitlist_limiter: RateLimiter | None = None,
        now: Callable[[], date] | None = None,
    ) -> None:
        self.store = store
        self.auth = auth
        self.mailer = mailer
        self.static_dir = Path(static_dir) if static_dir else None
        self.company = company or default_company()
        self.cors_origins = cors_origins_from_env()
        self.waitlist_limiter = waitlist_limiter or RateLimiter(10, 60)
        self._now = now or date.today

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    def handle(
        self,
        method: str,
        target: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        client_ip: str = "127.0.0.1",
    ) -> Response:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        parts = urlsplit(target)
        path = parts.path or "/"
        query = parse_qs(parts.query)
        method = method.upper()

        try:
            return self._route(method, path, query, headers, body, client_ip)
        except AuthError as exc:
            return error_response(401, str(exc), "unauthorized")
        except (StoreError, ValueError, KeyError) as exc:
            return error_response(400, str(exc), "bad_request")
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            return error_response(500, f"internal error: {exc}", "server_error")

    def _route(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
        client_ip: str,
    ) -> Response:
        # The waitlist is the only cross-origin surface: a landing page on
        # another host posts the sign-up form here. Authenticated routes must
        # never be reachable cross-origin, so CORS is applied to these two
        # routes only.
        if method == "OPTIONS" and path in _CORS_PATHS:
            return self._preflight(headers)
        if method == "POST" and path == "/api/waitlist":
            return self._cors(headers, self._waitlist_signup(headers, body, client_ip))
        if method == "GET" and path == "/api/waitlist/count":
            return self._cors(
                headers,
                json_response({"ok": True, "count": self.store.waitlist_count()}),
            )

        # Public, side-effect free liveness probe. PaaS health checks need a
        # route that exists in the app: some platforms answer /robots.txt
        # themselves, so a probe pointed there never reaches us.
        if method == "GET" and path == "/health":
            return json_response({"ok": True, "status": "up"})

        if method == "POST" and path == "/api/signup":
            return self._signup(headers, body)
        if method == "POST" and path == "/api/login":
            return self._login(headers, body)
        if method == "POST" and path == "/api/logout":
            return self._logout(headers)
        if method == "GET" and path == "/api/me":
            return self._me(headers)

        # Everything below requires a session.
        if path.startswith("/api/"):
            session = self._session(headers)
            if session is None:
                return error_response(401, "sign in to continue", "unauthorized")

            if method == "GET" and path == "/api/ledger":
                return self._get_ledger(session)
            if method == "POST" and path == "/api/ledger":
                return self._post_ledger(session, body)
            if method == "GET" and path == "/api/report":
                return self._report(session, query)
            if method == "GET" and path == "/api/drafts":
                return self._drafts(session, query)
            match = _SEND_ROUTE.match(path)
            if method == "POST" and match:
                return self._send_draft(session, match.group(1), query)
            if method == "GET" and path == "/api/nudges":
                return self._nudges(session, query)

            return error_response(404, f"no route for {method} {path}", "not_found")

        # The dashboard ships inside the package, so it works even when no
        # landing directory was found (API-only installs).
        if method == "GET" and path in ("/dashboard", "/dashboard/"):
            return self._dashboard()

        if method == "GET":
            return self._static(path, headers)
        return error_response(405, "method not allowed", "method_not_allowed")

    # ------------------------------------------------------------------ #
    # CORS
    # ------------------------------------------------------------------ #

    def _allowed_origin(self, headers: dict[str, str]) -> str:
        """The request's Origin, but only when it is explicitly allowed."""
        origin = headers.get("origin", "").strip().rstrip("/")
        if not origin or origin not in self.cors_origins:
            return ""
        return origin

    def _cors(self, headers: dict[str, str], response: Response) -> Response:
        origin = self._allowed_origin(headers)
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    def _preflight(self, headers: dict[str, str]) -> Response:
        """Answer a CORS preflight for the waitlist routes.

        A JSON POST is not a "simple" request, so the browser sends OPTIONS
        first. Without this the sign-up is blocked before it is ever sent.
        """
        origin = self._allowed_origin(headers)
        if not origin:
            return error_response(403, "origin not allowed", "forbidden_origin")
        return Response(
            status=204,
            body=b"",
            headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
            },
        )

    # ------------------------------------------------------------------ #
    # Waitlist
    # ------------------------------------------------------------------ #

    def _waitlist_signup(
        self, headers: dict[str, str], body: bytes, client_ip: str
    ) -> Response:
        if not self.waitlist_limiter.allow(client_ip):
            return error_response(429, "too many attempts", "rate_limited")
        payload = _json_body(body)
        try:
            email = validate_email(str(payload.get("email", "")))
        except AuthError:
            return error_response(422, "invalid email address", "invalid_email")
        source = str(payload.get("source") or "landing").strip()[:64] or "landing"
        already, count = self.store.add_waitlist(email, source)
        out: dict = {"ok": True, "count": count}
        if already:
            out["already"] = True
        return json_response(out)

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    def _session(self, headers: dict[str, str]) -> Session | None:
        raw = headers.get("cookie", "")
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:  # noqa: BLE001
            return None
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return None
        return self.auth.resolve(morsel.value)

    def _is_secure(self, headers: dict[str, str]) -> bool:
        """True when the request reached us over HTTPS.

        Behind a TLS-terminating proxy the socket is plain HTTP, so the only
        signal is the header the proxy sets. Without it the cookie would
        travel in clear text on a public deploy.
        """
        proto = headers.get("x-forwarded-proto", "")
        return proto.split(",")[0].strip().lower() == "https"

    def _set_cookie(self, token: str, max_age: int, secure: bool = False) -> str:
        flags = "Path=/; HttpOnly; SameSite=Lax"
        if secure:
            flags += "; Secure"
        return f"{SESSION_COOKIE}={token}; {flags}; Max-Age={max_age}"

    def _clear_cookie(self, secure: bool = False) -> str:
        flags = "Path=/; HttpOnly; SameSite=Lax"
        if secure:
            flags += "; Secure"
        return f"{SESSION_COOKIE}=; {flags}; Max-Age=0"

    def _signup(self, headers: dict[str, str], body: bytes) -> Response:
        payload = _json_body(body)
        password = str(payload.get("password", ""))
        try:
            session = self.auth.signup(
                str(payload.get("email", "")),
                password,
                company_name=str(payload.get("company_name") or ""),
                reply_to=str(payload.get("reply_to") or ""),
                signature=str(payload.get("signature") or ""),
                payment_link=str(payload.get("payment_link") or ""),
            )
        except AuthError as exc:
            status = 422 if str(exc) == "invalid_email" else 400
            return error_response(status, str(exc), str(exc))
        return json_response(
            {"ok": True, "account": {"email": session.email, "id": session.account_id}},
            **{
                "Set-Cookie": self._set_cookie(
                    session.token, self.auth.ttl_seconds, self._is_secure(headers)
                )
            },
        )

    def _login(self, headers: dict[str, str], body: bytes) -> Response:
        payload = _json_body(body)
        session = self.auth.login(
            str(payload.get("email", "")), str(payload.get("password", ""))
        )
        return json_response(
            {"ok": True, "account": {"email": session.email, "id": session.account_id}},
            **{
                "Set-Cookie": self._set_cookie(
                    session.token, self.auth.ttl_seconds, self._is_secure(headers)
                )
            },
        )

    def _logout(self, headers: dict[str, str]) -> Response:
        session = self._session(headers)
        if session is not None:
            self.auth.logout(session.token)
        return json_response(
            {"ok": True},
            **{"Set-Cookie": self._clear_cookie(self._is_secure(headers))},
        )

    def _me(self, headers: dict[str, str]) -> Response:
        session = self._session(headers)
        if session is None:
            return error_response(401, "sign in to continue", "unauthorized")
        return json_response({"ok": True, "account": self.auth.profile(session.account_id)})

    # ------------------------------------------------------------------ #
    # Ledger
    # ------------------------------------------------------------------ #

    def _get_ledger(self, session: Session) -> Response:
        invoices, customers = self.store.load_ledger(session.account_id)
        return json_response(
            {
                "ok": True,
                "invoices": len(invoices),
                "customers": len(customers),
                "rows": [
                    {
                        "id": i.id,
                        "number": i.number,
                        "customer": customers.get(i.customer_id).name
                        if customers.get(i.customer_id)
                        else i.customer_id,
                        "amount_cents": i.amount_cents,
                        "currency": i.currency,
                        "due_on": i.due_on.isoformat(),
                        "status": i.status.value,
                        "contacts": len(i.contacts),
                    }
                    for i in invoices
                ],
            }
        )

    def _post_ledger(self, session: Session, body: bytes) -> Response:
        payload = _json_body(body)
        if payload.get("demo"):
            customers_path = DEMO_DIR / "customers.csv"
            invoices_path = DEMO_DIR / "invoices.csv"
            if not customers_path.exists() or not invoices_path.exists():
                return error_response(
                    400, "demo ledger is not bundled with this install", "no_demo_ledger"
                )
        else:
            try:
                customers_text = str(payload["customers_csv"])
                invoices_text = str(payload["invoices_csv"])
            except KeyError as exc:
                return error_response(
                    400,
                    "provide customers_csv and invoices_csv, or {demo: true}",
                    "missing_payload",
                )
            customers_path = None
            invoices_path = None

        try:
            if customers_path is not None:
                customers = parse_customers_csv(customers_path.read_text(encoding="utf-8"))
                invoices = parse_invoices_csv(invoices_path.read_text(encoding="utf-8"))
            else:
                customers = parse_customers_csv(customers_text)
                invoices = parse_invoices_csv(invoices_text)
        except (ValueError, KeyError) as exc:
            return error_response(400, f"could not parse ledger: {exc}", "invalid_csv")

        # Every invoice must reference a known customer or decide() will raise.
        unknown = sorted({i.customer_id for i in invoices} - set(customers))
        if unknown:
            return error_response(
                400,
                f"invoices reference unknown customers: {', '.join(unknown[:5])}",
                "unknown_customer",
            )

        n_invoices, n_customers = self.store.replace_ledger(
            session.account_id, invoices, customers
        )
        return json_response(
            {"ok": True, "invoices": n_invoices, "customers": n_customers}
        )

    # ------------------------------------------------------------------ #
    # Report & drafts
    # ------------------------------------------------------------------ #

    def _company_for(self, session: Session) -> Company:
        profile = self.auth.profile(session.account_id)
        return Company(
            name=profile["company_name"] or self.company.name,
            reply_to=profile["reply_to"] or self.company.reply_to,
            signature=profile["signature"] or self.company.signature,
            payment_link=profile["payment_link"] or self.company.payment_link,
        )

    def _as_of(self, query: dict[str, list[str]]) -> date:
        raw = (query.get("as_of") or [""])[0]
        if raw:
            return date.fromisoformat(raw)
        return self._now()

    def _report_payload(self, report) -> dict:
        as_of = report.as_of
        return {
            "ok": True,
            "metrics": report.metrics(),
            "aging": report.aging(),
            "markdown": render_markdown(report),
            "decisions": [
                {
                    "invoice_id": a.invoice.id,
                    "number": a.invoice.number,
                    "customer": a.customer.name,
                    "amount_cents": a.invoice.amount_cents,
                    "currency": a.invoice.currency,
                    "days_overdue": a.invoice.days_overdue(as_of),
                    "action": a.decision.action,
                    "stage": a.decision.stage.value,
                    "reason": a.decision.reason.value,
                    "reason_label": REASON_LABELS[a.decision.reason],
                    "notes": list(a.decision.notes),
                }
                for a in report.actions
            ],
        }

    def _report(self, session: Session, query: dict[str, list[str]]) -> Response:
        invoices, _ = self.store.load_ledger(session.account_id)
        if not invoices:
            return error_response(400, "no ledger imported yet", "empty_ledger")
        return json_response(self._report_payload(self._report_run(session, query)))

    def _report_run(self, session: Session, query: dict[str, list[str]]):
        invoices, customers = self.store.load_ledger(session.account_id)
        return run(invoices, customers, self._as_of(query), self._company_for(session))

    def _drafts(self, session: Session, query: dict[str, list[str]]) -> Response:
        invoices, _ = self.store.load_ledger(session.account_id)
        if not invoices:
            return error_response(400, "no ledger imported yet", "empty_ledger")
        report = self._report_run(session, query)
        as_of = report.as_of
        return json_response(
            {
                "ok": True,
                "as_of": as_of.isoformat(),
                "drafts": [
                    {
                        "invoice_id": a.invoice.id,
                        "number": a.invoice.number,
                        "customer": a.customer.name,
                        "amount_cents": a.invoice.amount_cents,
                        "stage": a.draft.stage.value,
                        "to": a.draft.to,
                        "subject": a.draft.subject,
                        "body": a.draft.body,
                        "facts": list(a.draft.facts),
                        "nudges_so_far": len(a.invoice.contacts),
                    }
                    for a in report.drafts
                    if a.draft is not None
                ],
            }
        )

    def _send_draft(
        self, session: Session, invoice_id: str, query: dict[str, list[str]]
    ) -> Response:
        report = self._report_run(session, query)
        match = next((a for a in report.drafts if a.invoice.id == invoice_id), None)
        if match is None:
            # Distinguish "no such invoice" from "held, so nothing to send".
            known = any(a.invoice.id == invoice_id for a in report.actions)
            if not known:
                return error_response(404, "unknown invoice", "unknown_invoice")
            return error_response(
                409, "this invoice is held, nothing to send", "held"
            )

        draft = match.draft
        assert draft is not None
        delivery_id = self.mailer.send(
            MailMessage(
                to=draft.to,
                subject=draft.subject,
                body=draft.body,
                reply_to=report.company.reply_to,
                from_name=report.company.name,
            )
        )
        # sent_on is the run date so the cooldown window is measured against
        # the same clock the decisions used.
        self.store.record_nudge(
            session.account_id,
            invoice_id=invoice_id,
            stage=draft.stage.value,
            to_email=draft.to,
            subject=draft.subject,
            delivery_id=delivery_id,
            sent_on=report.as_of,
        )
        return json_response(
            {
                "ok": True,
                "delivery_id": delivery_id,
                "stage": draft.stage.value,
                "to": draft.to,
                "nudges_so_far": len(match.invoice.contacts) + 1,
                "mailer": self.mailer.mode,
            }
        )

    def _nudges(self, session: Session, query: dict[str, list[str]]) -> Response:
        invoice_id = (query.get("invoice_id") or [None])[0]
        return json_response(
            {
                "ok": True,
                "nudges": self.store.nudges_for(session.account_id, invoice_id),
            }
        )

    # ------------------------------------------------------------------ #
    # Dashboard & static
    # ------------------------------------------------------------------ #

    def _dashboard(self) -> Response:
        if not DASHBOARD_PATH.is_file():
            return error_response(404, "dashboard is not installed", "not_found")
        return Response(
            status=200,
            content_type="text/html; charset=utf-8",
            body=DASHBOARD_PATH.read_bytes(),
            headers={"Cache-Control": "no-cache"},
        )

    def _static(self, path: str, headers: dict[str, str]) -> Response:
        if self.static_dir is None:
            return error_response(404, "not found", "not_found")

        route = "index.html" if path in ("/", "") else path.lstrip("/")
        # Allowlist, not prefix matching: only known basenames are readable.
        if "/" in route or "\\" in route or route not in STATIC_ALLOWLIST:
            return error_response(404, "not found", "not_found")

        target = (self.static_dir / route).resolve()
        if not target.is_file() or self.static_dir.resolve() not in target.parents:
            return error_response(404, "not found", "not_found")

        ctype = mimetypes.guess_type(route)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        # The landing is small and iterated often, so revalidate rather than
        # risk serving a stale stylesheet against freshly edited HTML.
        cache = "no-cache"
        return Response(
            status=200,
            content_type=ctype,
            body=target.read_bytes(),
            headers={"Cache-Control": cache},
        )


def _json_body(body: bytes) -> dict:
    if not body:
        return {}
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    """Thin adapter: HTTP in, Application out. No logic of its own."""

    app: Application  # injected by make_server
    server_version = "Dunwell/0.1"
    sys_version = ""

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._write(
                Response(status=413, body=b'{"ok":false,"error":{"code":"too_large"}}')
            )
            return
        body = self.rfile.read(length) if length else b""
        client_ip = self.client_address[0] if self.client_address else "0.0.0.0"
        response = self.app.handle(
            self.command, self.path, dict(self.headers.items()), body, client_ip
        )
        self._write(response)

    def _write(self, response: Response) -> None:
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected mid-response; nothing to do

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        # CORS preflight for the public waitlist routes. Without this method
        # the base handler answers 501 and a browser blocks the sign-up
        # before it is ever sent.
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass  # quiet by default; the CLI reports what matters


def make_server(
    app: Application, host: str = "127.0.0.1", port: int = 8906
) -> ThreadingHTTPServer:
    """Bind the application to a threaded HTTP server."""
    handler = type("BoundHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve_forever(server: ThreadingHTTPServer) -> None:
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.shutdown()
        server.server_close()
