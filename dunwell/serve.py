"""Run the Dunwell service: landing page, dashboard, and API on one port.

    python -m dunwell.serve --port 8906

Serves three things from one process:

- ``/``            the marketing landing page (if a static dir is found)
- ``/dashboard``   the operator UI for importing a ledger and sending drafts
- ``/api/**``      the JSON API the dashboard and any client use

The static directory is resolved in this order: ``--static``, then
``DUNWELL_STATIC_DIR``, then the sibling ``dunwell-landing`` checkout, then
nothing (API-only). SMTP is off until the env asks for it, so the service is
fully usable with no credentials at all.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .auth import AuthService
from .mailer import build_mailer
from .service import Application, make_server, serve_forever
from .store import Store

_PKG_ROOT = Path(__file__).resolve().parent.parent
_CANDIDATE_STATIC_DIRS = (
    _PKG_ROOT.parent / "dunwell-landing",  # sibling checkout: D:\dunwell-landing
    _PKG_ROOT / "landing",                 # vendored copy, if you prefer one repo
)


def resolve_static_dir(explicit: str | None = None) -> Path | None:
    """Find the landing page, or return None to run API-only."""
    if explicit:
        path = Path(explicit)
        return path if path.is_dir() else None

    from_env = os.environ.get("DUNWELL_STATIC_DIR")
    if from_env:
        path = Path(from_env)
        if path.is_dir():
            return path
        print(
            f"dunwell: DUNWELL_STATIC_DIR={from_env} is not a directory; "
            "serving API only",
            file=sys.stderr,
        )

    for candidate in _CANDIDATE_STATIC_DIRS:
        if candidate.is_dir():
            return candidate
    return None


def _env_port() -> int:
    """Read PORT from the environment, ignoring anything unusable."""
    raw = os.environ.get("PORT", "").strip()
    if not raw:
        return 8906
    try:
        port = int(raw)
    except ValueError:
        print(f"dunwell: PORT={raw!r} is not a number; using 8906", file=sys.stderr)
        return 8906
    # 0 asks the OS for a free port, which is legal and useful for tests.
    return port if 0 <= port <= 65535 else 8906


def build_app(
    db_path: str,
    *,
    static_dir: str | None = None,
    signup_mode: str | None = None,
) -> Application:
    """Assemble the application from the environment and a database path."""
    store = Store(db_path)
    mailer = build_mailer()
    return Application(
        store=store,
        auth=AuthService(store),
        mailer=mailer,
        static_dir=resolve_static_dir(static_dir),
        signup_mode=signup_mode,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dunwell serve",
        description="Serve the Dunwell dashboard, landing page, and API.",
    )
    # PaaS platforms set PORT and expect a bind on 0.0.0.0, not 127.0.0.1.
    # The flags still win, so an explicit --port always beats the environment.
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_env_port())
    parser.add_argument(
        "--db",
        default=os.environ.get("DUNWELL_DB", "dunwell.db"),
        help="SQLite file for accounts, ledgers, and nudge history",
    )
    parser.add_argument(
        "--static",
        default=None,
        help="Directory holding index.html (default: auto-detect)",
    )
    args = parser.parse_args(argv)

    app = build_app(args.db, static_dir=args.static)
    server = make_server(app, args.host, args.port)
    port = server.server_address[1]

    static = str(app.static_dir) if app.static_dir else "(none - API only)"
    print(f"Dunwell listening on http://{args.host}:{port}")
    print(f"  dashboard   http://{args.host}:{port}/dashboard")
    print(f"  landing     {static}")
    print(f"  database    {args.db}")
    print(f"  mailer      {app.mailer.mode}")
    if app.signup_mode != "open":
        print(
            "  signup      closed (set DUNWELL_SIGNUP_MODE=open for self-serve,\n"
            "              or create an account with: python -m dunwell.admin)"
        )
    else:
        print("  signup      open (anyone can create an account)")
    if app.mailer.mode != "smtp":
        print(
            "  note: drafts are recorded, not delivered. Set DUNWELL_MAIL_MODE=smtp\n"
            "        and SMTP_HOST to send for real."
        )

    try:
        serve_forever(server)
    finally:
        app.store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
