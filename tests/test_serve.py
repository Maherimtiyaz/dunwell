"""Tests for the serve entry point and the packaged dashboard route."""

from __future__ import annotations

import json
import threading
from datetime import date
from http.client import HTTPConnection
from pathlib import Path

import pytest

from dunwell.auth import AuthService
from dunwell.mailer import DryRunMailer
from dunwell.serve import _env_port, build_app, resolve_static_dir
from dunwell.service import Application, make_server
from dunwell.store import Store

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def app(tmp_path):
    store = Store(tmp_path / "serve.db")
    application = Application(
        store=store,
        auth=AuthService(store),
        mailer=DryRunMailer(),
        now=lambda: date(2026, 10, 16),
    )
    yield application
    store.close()


def test_explicit_static_dir_is_used_when_it_exists(tmp_path) -> None:
    (tmp_path / "landing").mkdir()
    assert resolve_static_dir(str(tmp_path / "landing")) == tmp_path / "landing"


def test_explicit_static_dir_that_is_missing_falls_back_to_none(tmp_path) -> None:
    assert resolve_static_dir(str(tmp_path / "nope")) is None


def test_port_comes_from_the_environment(monkeypatch) -> None:
    """PaaS platforms set PORT; without it the app answers on the wrong port."""
    monkeypatch.setenv("PORT", "10000")
    assert _env_port() == 10000

    monkeypatch.delenv("PORT", raising=False)
    assert _env_port() == 8906

    # A junk value must not crash the boot; fall back to the default.
    monkeypatch.setenv("PORT", "not-a-number")
    assert _env_port() == 8906


def test_build_app_finds_the_sibling_landing_page(tmp_path, monkeypatch) -> None:
    """Auto-detection picks up a sibling checkout without assuming the machine layout.

    The candidate list is injected so this passes on CI and in a fresh clone,
    where no ``dunwell-landing`` directory sits next to the engine.
    """
    landing = tmp_path / "dunwell-landing"
    landing.mkdir()
    (landing / "index.html").write_text("<h1>landing</h1>", encoding="utf-8")
    monkeypatch.delenv("DUNWELL_STATIC_DIR", raising=False)
    monkeypatch.setattr(
        "dunwell.serve._CANDIDATE_STATIC_DIRS",
        (landing, tmp_path / "dunwell" / "landing"),
    )

    application = build_app(str(tmp_path / "d.db"))
    try:
        assert application.static_dir == landing
        assert (application.static_dir / "index.html").is_file()
    finally:
        application.store.close()


def test_auto_detection_returns_none_when_no_landing_exists(
    tmp_path, monkeypatch
) -> None:
    """API-only installs are valid: no landing anywhere means no static dir."""
    monkeypatch.delenv("DUNWELL_STATIC_DIR", raising=False)
    monkeypatch.setattr(
        "dunwell.serve._CANDIDATE_STATIC_DIRS",
        (tmp_path / "missing-a", tmp_path / "missing-b"),
    )
    assert resolve_static_dir() is None


def test_dashboard_is_served_even_with_no_static_dir(app, tmp_path) -> None:
    """The dashboard ships in the package, so API-only installs still get it."""
    app.static_dir = None
    res = app.handle("GET", "/dashboard")
    assert res.status == 200
    assert res.content_type.startswith("text/html")
    body = res.body.decode("utf-8")
    assert "Dunwell" in body and "/api/report" in body

    # The alias resolves too, and unknown dashboard paths do not.
    assert app.handle("GET", "/dashboard/").status == 200
    assert app.handle("GET", "/dashboard/secret").status == 404


def test_dashboard_requires_no_session(app) -> None:
    """The page loads unauthenticated; the API behind it still enforces auth."""
    assert app.handle("GET", "/dashboard").status == 200
    assert app.handle("GET", "/api/report").status == 401


def test_landing_static_still_served_alongside_dashboard(app, tmp_path) -> None:
    (tmp_path / "index.html").write_text("<h1>landing</h1>", encoding="utf-8")
    (tmp_path / "styles.css").write_text("body{}", encoding="utf-8")
    app.static_dir = tmp_path
    assert app.handle("GET", "/").status == 200
    assert app.handle("GET", "/styles.css").status == 200
    assert app.handle("GET", "/dashboard").status == 200


def test_full_dashboard_flow_over_a_real_socket(tmp_path) -> None:
    """End to end: sign up, load the demo ledger, send a draft, see history."""
    store = Store(tmp_path / "flow.db")
    app = Application(
        store=store,
        auth=AuthService(store),
        mailer=DryRunMailer(),
        now=lambda: date(2026, 10, 16),
    )
    server = make_server(app, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, payload=None, cookie=None):
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = "dunwell_session=" + cookie
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = json.loads(res.read().decode("utf-8") or "{}")
        conn.close()
        return res.status, data, res.getheader("Set-Cookie")

    try:
        status, _, set_cookie = request(
            "POST", "/api/signup", {"email": "ops@acme.test", "password": "hunter2hunter2"}
        )
        assert status == 200
        token = set_cookie.split("dunwell_session=")[1].split(";")[0]

        status, data, _ = request("POST", "/api/ledger", {"demo": True}, token)
        assert status == 200
        assert data["invoices"] > 0

        status, data, _ = request("GET", "/api/report?as_of=2026-10-16", cookie=token)
        assert status == 200
        assert data["metrics"]["drafts"] >= 1

        status, data, _ = request("GET", "/api/drafts?as_of=2026-10-16", cookie=token)
        assert status == 200
        first = data["drafts"][0]
        assert first["to"] and first["subject"]

        status, data, _ = request(
            "POST",
            f"/api/drafts/{first['invoice_id']}/send?as_of=2026-10-16",
            cookie=token,
        )
        assert status == 200 and data["ok"] is True
        assert data["delivery_id"].startswith("dry_")

        status, data, _ = request("GET", "/api/nudges", cookie=token)
        assert status == 200
        assert len(data["nudges"]) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()


def test_demo_ledger_is_optional_not_required(tmp_path) -> None:
    """A missing demo directory is a clear 400, not a crash."""
    store = Store(tmp_path / "nodemo.db")
    app = Application(
        store=store,
        auth=AuthService(store),
        mailer=DryRunMailer(),
    )
    res = app.handle(
        "POST", "/api/signup", {}, b'{"email":"a@b.test","password":"hunter2hunter2"}'
    )
    token = res.headers["Set-Cookie"].split("dunwell_session=")[1].split(";")[0]

    original = Path(__file__).resolve().parent.parent / "demo"
    assert original.is_dir(), "the repo ships a demo ledger"

    # Point the app at a directory with no demo CSVs and confirm the message.
    import dunwell.service as service_module

    real = service_module.DEMO_DIR
    service_module.DEMO_DIR = tmp_path / "missing"
    try:
        res = app.handle(
            "POST",
            "/api/ledger",
            {"Cookie": "dunwell_session=" + token},
            b'{"demo": true}',
        )
        assert res.status == 400
        assert json.loads(res.body)["error"]["code"] == "no_demo_ledger"
    finally:
        service_module.DEMO_DIR = real
        store.close()
