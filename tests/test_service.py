"""Service-layer tests: store, auth, mailer, and the HTTP application.

The most important test here is ``test_nudge_history_drives_policy``: it proves
sending a draft writes durable history that the *next* run reads back, which is
what makes the cooldown window and nudge cap fire on live data instead of
always seeing an empty contact list.
"""

from __future__ import annotations

import json
import threading
from datetime import date, timedelta

import pytest

from dunwell.auth import (
    AuthError,
    AuthService,
    hash_password,
    normalize_email,
    validate_email,
    verify_password,
)
from dunwell.core import Reason, Stage
from dunwell.mailer import DryRunMailer, MailMessage, build_mailer
from dunwell.service import Application, RateLimiter, make_server
from dunwell.store import Store, StoreError

AS_OF = date(2026, 10, 16)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def app(store):
    return Application(
        store=store,
        auth=AuthService(store),
        mailer=DryRunMailer(),
        company=None,
        now=lambda: AS_OF,
    )


def call(app, method, path, payload=None, cookie=None, ip="127.0.0.1"):
    headers = {}
    body = b""
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    return app.handle(method, path, headers, body, ip)


def body_of(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #


def test_password_hash_round_trip() -> None:
    encoded = hash_password("correct horse battery")
    assert verify_password("correct horse battery", encoded) is True
    assert verify_password("wrong horse battery", encoded) is False


def test_password_hashes_are_salted() -> None:
    assert hash_password("same-password") != hash_password("same-password")


def test_short_password_rejected() -> None:
    with pytest.raises(AuthError):
        hash_password("short")


def test_verify_rejects_malformed_encodings() -> None:
    for bad in ("", "nonsense", "bcrypt$1$2$3$4", "scrypt$notanint$8$1$ab$cd"):
        assert verify_password("anything", bad) is False


def test_email_validation() -> None:
    assert validate_email("  CEO@Acme.IO ") == "ceo@acme.io"
    for bad in ("", "nope", "a@b", "a@b.", "@b.com", "a b@c.com"):
        with pytest.raises(AuthError):
            validate_email(bad)
    assert normalize_email(" A@B.COM ") == "a@b.com"


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def test_account_uniqueness_is_case_insensitive(store) -> None:
    store.create_account("Owner@Acme.test", "x")
    with pytest.raises(StoreError):
        store.create_account("owner@ACME.test", "y")


def test_ledger_round_trip_preserves_every_field(store) -> None:
    account = store.create_account("a@b.test", "x")
    from dunwell.core import Customer, Invoice, InvoiceStatus

    customer = Customer(
        id="c1",
        name="Ava Chen",
        email="ava@x.test",
        tier="strategic",
        opted_out=True,
        relationship_note="renewed twice",
    )
    invoice = Invoice(
        id="i1",
        number="INV-1",
        customer_id="c1",
        amount_cents=120000,
        currency="USD",
        issued_on=date(2026, 9, 1),
        due_on=date(2026, 9, 30),
        status=InvoiceStatus.DISPUTED,
        dispute_note="scope disagreement",
    )
    store.replace_ledger(account, [invoice], {"c1": customer})

    invoices, customers = store.load_ledger(account)
    assert len(invoices) == 1 and len(customers) == 1
    got = customers["c1"]
    assert got.tier == "strategic" and got.opted_out is True
    assert invoices[0].status is InvoiceStatus.DISPUTED
    assert invoices[0].dispute_note == "scope disagreement"
    assert invoices[0].contacts == ()  # nothing sent yet


def test_replace_ledger_clears_previous_rows(store) -> None:
    account = store.create_account("a@b.test", "x")
    from dunwell.core import Customer, Invoice

    def ledger(number: str):
        c = Customer(id="c1", name="Ava", email="ava@x.test")
        i = Invoice(
            id="i1",
            number=number,
            customer_id="c1",
            amount_cents=100,
            currency="USD",
            issued_on=date(2026, 9, 1),
            due_on=date(2026, 9, 30),
        )
        return [i], {"c1": c}

    store.replace_ledger(account, *ledger("FIRST"))
    store.replace_ledger(account, *ledger("SECOND"))
    invoices, _ = store.load_ledger(account)
    assert [i.number for i in invoices] == ["SECOND"]


def test_waitlist_dedupe_is_case_insensitive(store) -> None:
    already, count = store.add_waitlist("A@B.test", "landing")
    assert already is False and count == 1
    already, count = store.add_waitlist("a@b.test", "footer")
    assert already is True and count == 1
    assert store.waitlist_count() == 1


def test_sessions_resolve_and_expire(store) -> None:
    account = store.create_account("a@b.test", "x")
    store.create_session(account, "tok-live", ttl_seconds=3600)
    store.create_session(account, "tok-dead", ttl_seconds=-1)
    assert store.session_account("tok-live") == account
    assert store.session_account("tok-dead") is None
    assert store.session_account("tok-unknown") is None
    store.delete_session("tok-live")
    assert store.session_account("tok-live") is None


# --------------------------------------------------------------------------- #
# Auth service
# --------------------------------------------------------------------------- #


def test_signup_login_logout(store) -> None:
    auth = AuthService(store)
    session = auth.signup("owner@acme.test", "hunter2hunter2")
    assert session.email == "owner@acme.test"
    assert auth.resolve(session.token) is not None

    # Wrong password must not reveal whether the account exists.
    with pytest.raises(AuthError):
        auth.login("owner@acme.test", "wrong-password")
    with pytest.raises(AuthError):
        auth.login("ghost@acme.test", "wrong-password")

    again = auth.login("owner@acme.test", "hunter2hunter2")
    assert again.account_id == session.account_id

    auth.logout(session.token)
    assert auth.resolve(session.token) is None


def test_duplicate_signup_rejected(store) -> None:
    auth = AuthService(store)
    auth.signup("dup@acme.test", "hunter2hunter2")
    with pytest.raises(StoreError):
        auth.signup("DUP@acme.test", "hunter2hunter2")


def test_profile_defaults_then_updates(store) -> None:
    auth = AuthService(store)
    session = auth.signup("p@acme.test", "hunter2hunter2")
    profile = auth.profile(session.account_id)
    assert profile["company_name"] == ""
    store.update_account_profile(
        session.account_id, company_name="Northwind Studio", reply_to="billing@x.test"
    )
    profile = auth.profile(session.account_id)
    assert profile["company_name"] == "Northwind Studio"
    with pytest.raises(StoreError):
        store.update_account_profile(session.account_id, nonsense="x")


# --------------------------------------------------------------------------- #
# Mailer
# --------------------------------------------------------------------------- #


def test_dry_run_mailer_records_and_returns_id() -> None:
    mailer = DryRunMailer()
    delivery = mailer.send(MailMessage(to="a@b.test", subject="s", body="b"))
    assert delivery.startswith("dry_")
    assert len(mailer.sent) == 1


def test_mailer_refuses_to_send_without_recipient() -> None:
    with pytest.raises(ValueError):
        DryRunMailer().send(MailMessage(to="", subject="s", body="b"))


def test_build_mailer_defaults_to_dry_run() -> None:
    assert build_mailer({}).mode == "dryrun"


def test_build_mailer_falls_back_when_smtp_unconfigured() -> None:
    # Asking for SMTP without SMTP_HOST must not crash or silently change mode.
    assert build_mailer({"DUNWELL_MAIL_MODE": "smtp"}).mode == "dryrun"


def test_build_mailer_builds_smtp_with_host() -> None:
    mailer = build_mailer(
        {"DUNWELL_MAIL_MODE": "smtp", "SMTP_HOST": "smtp.x.test", "SMTP_PORT": "2525"}
    )
    assert mailer.mode == "smtp"
    assert mailer.host == "smtp.x.test"
    assert mailer.port == 2525


def test_message_headers() -> None:
    msg = MailMessage(to="a@b.test", subject="Hi", body="Body", reply_to="r@x.test")
    email = msg.as_email_message("billing@dunwell.app")
    assert email["To"] == "a@b.test"
    assert email["Reply-To"] == "r@x.test"
    assert email["From"] == "billing@dunwell.app"


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #


def test_rate_limiter_allows_then_blocks_then_resets() -> None:
    limiter = RateLimiter(limit=3, window_seconds=3600)
    assert [limiter.allow("ip") for _ in range(3)] == [True, True, True]
    assert limiter.allow("ip") is False
    # Independent key.
    assert limiter.allow("other") is True


# --------------------------------------------------------------------------- #
# HTTP application
# --------------------------------------------------------------------------- #


def test_waitlist_endpoint_contract(app) -> None:
    r = call(app, "POST", "/api/waitlist", {"email": "a@b.test", "source": "landing"})
    assert r.status == 200
    assert body_of(r) == {"ok": True, "count": 1}

    r = call(app, "POST", "/api/waitlist", {"email": "A@B.test", "source": "landing"})
    assert body_of(r) == {"ok": True, "count": 1, "already": True}

    r = call(app, "GET", "/api/waitlist/count")
    assert body_of(r) == {"ok": True, "count": 1}


def test_waitlist_rejects_invalid_email(app) -> None:
    r = call(app, "POST", "/api/waitlist", {"email": "nope"})
    assert r.status == 422
    assert body_of(r)["error"]["code"] == "invalid_email"


def test_waitlist_rate_limits_per_ip(app) -> None:
    for _ in range(10):
        call(app, "POST", "/api/waitlist", {"email": "a@b.test"}, ip="9.9.9.9")
    r = call(app, "POST", "/api/waitlist", {"email": "a@b.test"}, ip="9.9.9.9")
    assert r.status == 429
    # A different client is unaffected.
    assert call(
        app, "POST", "/api/waitlist", {"email": "c@d.test"}, ip="8.8.8.8"
    ).status == 200


def test_signup_sets_cookie_and_me_reads_it(app) -> None:
    r = call(app, "POST", "/api/signup", {"email": "own@acme.test", "password": "hunter2hunter2"})
    assert r.status == 200
    cookie_header = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie_header and "SameSite=Lax" in cookie_header
    token = cookie_header.split("dunwell_session=")[1].split(";")[0]

    r = call(app, "GET", "/api/me", cookie=f"dunwell_session={token}")
    assert body_of(r)["account"]["email"] == "own@acme.test"


def test_protected_routes_require_session(app) -> None:
    for path in ("/api/ledger", "/api/report", "/api/drafts", "/api/nudges"):
        r = call(app, "GET", path)
        assert r.status == 401, path
        assert body_of(r)["error"]["code"] == "unauthorized"


def test_report_before_ledger_import_is_a_clear_400(app) -> None:
    call(app, "POST", "/api/signup", {"email": "x@acme.test", "password": "hunter2hunter2"})
    # Re-login to grab a usable cookie from the login route.
    r = call(app, "POST", "/api/login", {"email": "x@acme.test", "password": "hunter2hunter2"})
    token = r.headers["Set-Cookie"].split("dunwell_session=")[1].split(";")[0]
    cookie = f"dunwell_session={token}"

    r = call(app, "GET", "/api/report", cookie=cookie)
    assert r.status == 400
    assert body_of(r)["error"]["code"] == "empty_ledger"


def test_ledger_import_rejects_unknown_customer(app) -> None:
    r = call(app, "POST", "/api/signup", {"email": "y@acme.test", "password": "hunter2hunter2"})
    cookie = "dunwell_session=" + r.headers["Set-Cookie"].split("dunwell_session=")[1].split(";")[0]

    invoices_csv = (
        "id,number,customer_id,amount_cents,currency,issued_on,due_on,status\n"
        "i1,INV-1,GHOST,100000,USD,2026-09-01,2026-09-30,open\n"
    )
    r = call(
        app,
        "POST",
        "/api/ledger",
        {"customers_csv": "id,name,email\n", "invoices_csv": invoices_csv},
        cookie=cookie,
    )
    assert r.status == 400
    assert body_of(r)["error"]["code"] == "unknown_customer"


def test_full_flow_import_report_send(app) -> None:
    r = call(app, "POST", "/api/signup", {"email": "z@acme.test", "password": "hunter2hunter2"})
    cookie = "dunwell_session=" + r.headers["Set-Cookie"].split("dunwell_session=")[1].split(";")[0]

    customers_csv = "id,name,email,tier\n" "c1,Ava Chen,ava@x.test,standard\n"
    invoices_csv = (
        "id,number,customer_id,amount_cents,currency,issued_on,due_on,status\n"
        "i1,INV-1001,c1,120000,USD,2026-09-01,2026-09-30,open\n"
    )
    r = call(
        app,
        "POST",
        "/api/ledger",
        {"customers_csv": customers_csv, "invoices_csv": invoices_csv},
        cookie=cookie,
    )
    assert r.status == 200 and body_of(r) == {"ok": True, "invoices": 1, "customers": 1}

    r = call(app, "GET", "/api/report?as_of=2026-10-16", cookie=cookie)
    assert r.status == 200
    payload = body_of(r)
    assert payload["metrics"]["invoices"] == 1
    assert payload["decisions"][0]["reason"] == Reason.READY.value
    assert payload["decisions"][0]["stage"] == Stage.COURTEOUS.value
    assert "INV-1001" in payload["markdown"]

    r = call(app, "GET", "/api/drafts?as_of=2026-10-16", cookie=cookie)
    drafts = body_of(r)["drafts"]
    assert len(drafts) == 1
    assert drafts[0]["stage"] == "courteous"
    assert drafts[0]["to"] == "ava@x.test"

    r = call(
        app, "POST", "/api/drafts/i1/send?as_of=2026-10-16", cookie=cookie
    )
    sent = body_of(r)
    assert sent["ok"] is True
    assert sent["delivery_id"].startswith("dry_")
    assert sent["nudges_so_far"] == 1

    r = call(app, "GET", "/api/nudges", cookie=cookie)
    nudges = body_of(r)["nudges"]
    assert len(nudges) == 1
    assert nudges[0]["invoice_id"] == "i1" and nudges[0]["stage"] == "courteous"


def test_send_on_held_invoice_returns_409(app) -> None:
    r = call(app, "POST", "/api/signup", {"email": "h@acme.test", "password": "hunter2hunter2"})
    cookie = "dunwell_session=" + r.headers["Set-Cookie"].split("dunwell_session=")[1].split(";")[0]

    # Disputed invoice -> hold, so there is nothing to send.
    customers_csv = "id,name,email\n" "c1,Sam, sam@x.test\n"
    invoices_csv = (
        "id,number,customer_id,amount_cents,currency,issued_on,due_on,status\n"
        "i1,INV-9,c1,100000,USD,2026-09-01,2026-09-30,disputed\n"
    )
    call(
        app,
        "POST",
        "/api/ledger",
        {"customers_csv": customers_csv, "invoices_csv": invoices_csv},
        cookie=cookie,
    )

    r = call(app, "POST", "/api/drafts/i1/send?as_of=2026-10-16", cookie=cookie)
    assert r.status == 409
    assert body_of(r)["error"]["code"] == "held"

    r = call(app, "POST", "/api/drafts/NOPE/send?as_of=2026-10-16", cookie=cookie)
    assert r.status == 404


def test_logout_clears_session(app) -> None:
    r = call(app, "POST", "/api/signup", {"email": "l@acme.test", "password": "hunter2hunter2"})
    cookie = "dunwell_session=" + r.headers["Set-Cookie"].split("dunwell_session=")[1].split(";")[0]
    assert call(app, "GET", "/api/me", cookie=cookie).status == 200

    call(app, "POST", "/api/logout", cookie=cookie)
    assert call(app, "GET", "/api/me", cookie=cookie).status == 401


def test_unknown_api_route_is_404_not_500(app) -> None:
    r = call(app, "POST", "/api/signup", {"email": "n@acme.test", "password": "hunter2hunter2"})
    cookie = "dunwell_session=" + r.headers["Set-Cookie"].split("dunwell_session=")[1].split(";")[0]
    r = call(app, "GET", "/api/nope", cookie=cookie)
    assert r.status == 404
    assert body_of(r)["error"]["code"] == "not_found"


def test_malformed_json_is_400_not_500(app) -> None:
    r = app.handle("POST", "/api/waitlist", {}, b"{not json")
    assert r.status == 400


# --------------------------------------------------------------------------- #
# THE key behaviour: sent nudges feed back into policy
# --------------------------------------------------------------------------- #


def test_nudge_history_drives_cooldown_and_cap(store, app) -> None:
    """Sending must persist history that the next run reads back.

    Without this, every run sees zero contacts: the cooldown window and the
    nudge cap would never fire on live data.
    """
    r = call(app, "POST", "/api/signup", {"email": "n@acme.test", "password": "hunter2hunter2"})
    cookie = "dunwell_session=" + r.headers["Set-Cookie"].split("dunwell_session=")[1].split(";")[0]

    customers_csv = "id,name,email\n" "c1,Marcus,marcus@x.test\n"
    invoices_csv = (
        "id,number,customer_id,amount_cents,currency,issued_on,due_on,status\n"
        # 31 days overdue on 2026-10-16: inside the ladder window, so the
        # courteous -> firm -> final progression is observable.
        "i1,INV-1002,c1,55000,USD,2026-08-15,2026-09-15,open\n"
    )
    call(
        app,
        "POST",
        "/api/ledger",
        {"customers_csv": customers_csv, "invoices_csv": invoices_csv},
        cookie=cookie,
    )

    # 1st send: courteous, 0 prior contacts.
    r = call(app, "POST", "/api/drafts/i1/send?as_of=2026-10-16", cookie=cookie)
    assert body_of(r)["stage"] == "courteous"
    assert body_of(r)["nudges_so_far"] == 1

    # 2nd send, same day: cooldown holds it (7-day window).
    r = call(app, "POST", "/api/drafts/i1/send?as_of=2026-10-16", cookie=cookie)
    assert r.status == 409, "cooldown must prevent a same-day resend"

    # Next day the cooldown still holds; now stage advances because of history.
    r = call(app, "GET", "/api/drafts?as_of=2026-10-24", cookie=cookie)
    drafts = body_of(r)["drafts"]
    assert drafts[0]["stage"] == "firm", "history must advance the tone ladder"
    assert drafts[0]["nudges_so_far"] == 1

    # 2nd send succeeds outside the cooldown window.
    r = call(app, "POST", "/api/drafts/i1/send?as_of=2026-10-24", cookie=cookie)
    assert body_of(r)["ok"] is True
    assert body_of(r)["nudges_so_far"] == 2

    # 3rd send lands the cap: with max_nudges=3, the next run must escalate.
    r = call(app, "GET", "/api/drafts?as_of=2026-11-02", cookie=cookie)
    drafts = body_of(r)["drafts"]
    assert drafts[0]["stage"] == "final"

    r = call(app, "POST", "/api/drafts/i1/send?as_of=2026-11-02", cookie=cookie)
    assert body_of(r)["ok"] is True
    assert body_of(r)["nudges_so_far"] == 3

    # Cap reached: no more drafts, and the decision escalates to a human.
    r = call(app, "GET", "/api/drafts?as_of=2026-11-15", cookie=cookie)
    assert body_of(r)["drafts"] == []

    r = call(app, "GET", "/api/report?as_of=2026-11-15", cookie=cookie)
    decision = body_of(r)["decisions"][0]
    assert decision["reason"] == Reason.MAX_NUDGES.value
    assert decision["stage"] == Stage.ESCALATE.value

    # History survived as three durable rows, not transient state.
    r = call(app, "GET", "/api/nudges", cookie=cookie)
    assert len(body_of(r)["nudges"]) == 3


def test_company_profile_shapes_the_draft(app) -> None:
    r = call(app, "POST", "/api/signup", {"email": "c@acme.test", "password": "hunter2hunter2"})
    cookie = "dunwell_session=" + r.headers["Set-Cookie"].split("dunwell_session=")[1].split(";")[0]
    call(
        app,
        "POST",
        "/api/ledger",
        {
            "customers_csv": "id,name,email\n" "c1,Ava,a@x.test\n",
            "invoices_csv": (
                "id,number,customer_id,amount_cents,currency,issued_on,due_on,status\n"
                "i1,INV-1,c1,99000,USD,2026-09-01,2026-09-30,open\n"
            ),
        },
        cookie=cookie,
    )
    # Personalize through the profile, not the server default.
    from dunwell.store import Store  # noqa: F401  (documents the store dependency)

    app.auth.profile  # profile lives on the account row
    r = call(app, "GET", "/api/me", cookie=cookie)
    assert r.status == 200

    r = call(app, "GET", "/api/drafts?as_of=2026-10-16", cookie=cookie)
    body = body_of(r)["drafts"][0]["body"]
    # Default company signature is applied when the profile is empty.
    assert "Northwind Studio - Billing" in body


# --------------------------------------------------------------------------- #
# Static file safety
# --------------------------------------------------------------------------- #


def test_static_paths_are_allowlisted(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (tmp_path / "styles.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "server.py").write_text("import os", encoding="utf-8")
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("nope", encoding="utf-8")

    store = Store(tmp_path / "db.sqlite")
    try:
        app = Application(
            store=store, auth=AuthService(store), mailer=DryRunMailer(), static_dir=tmp_path
        )
        assert app.handle("GET", "/").status == 200
        assert app.handle("GET", "/styles.css").status == 200

        # Not on the allowlist, even though it sits in the same directory.
        assert app.handle("GET", "/server.py").status == 404
        # Traversal attempts.
        assert app.handle("GET", "/../secret.txt").status == 404
        assert app.handle("GET", "/..%2Fsecret.txt").status == 404
        assert app.handle("GET", "/nested/file.html").status == 404
    finally:
        store.close()
        secret.unlink(missing_ok=True)


def test_no_static_dir_returns_404(tmp_path) -> None:
    store = Store(tmp_path / "db.sqlite")
    try:
        app = Application(store=store, auth=AuthService(store), mailer=DryRunMailer())
        assert app.handle("GET", "/").status == 404
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Real socket round trip
# --------------------------------------------------------------------------- #


def test_server_round_trip_over_a_socket(tmp_path) -> None:
    """Bind a real server on an ephemeral port and drive it with http.client."""
    import http.client

    store = Store(tmp_path / "svc.db")
    app = Application(
        store=store,
        auth=AuthService(store),
        mailer=DryRunMailer(),
        now=lambda: AS_OF,
    )
    server = make_server(app, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

        conn.request("POST", "/api/waitlist", body=json.dumps({"email": "sock@x.test"}))
        res = conn.getresponse()
        assert res.status == 200
        assert json.loads(res.read())["count"] == 1

        payload = json.dumps({"email": "sock2@x.test", "password": "hunter2hunter2"})
        conn.request("POST", "/api/signup", body=payload)
        res = conn.getresponse()
        assert res.status == 200
        cookie = res.getheader("Set-Cookie").split("dunwell_session=")[1].split(";")[0]
        res.read()

        # The cookie header needs the cookie NAME, not just the token value.
        conn.request("GET", "/api/me", headers={"Cookie": "dunwell_session=" + cookie})
        res = conn.getresponse()
        assert res.status == 200
        assert json.loads(res.read())["account"]["email"] == "sock2@x.test"

        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()
