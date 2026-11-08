"""The waitlist confirmation email.

Covers the two failure modes that made the old form feel broken: no message
was ever composed, and an address that signed up before the feature existed
(or whose first send failed) never got a second chance.
"""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

from dunwell.auth import AuthService
from dunwell.emails import render_waitlist_welcome, waitlist_welcome
from dunwell.mailer import DryRunMailer, MailerError, build_mailer
from dunwell.service import Application
from dunwell.store import Store


@pytest.fixture(autouse=True)
def _clean_mail_env(monkeypatch):
    for name in (
        "DUNWELL_MAIL_MODE",
        "SMTP_HOST",
        "MAIL_FROM",
        "DUNWELL_SITE_URL",
        "DUNWELL_FOUNDER_NAME",
        "DUNWELL_BRAND_NAME",
        "DUNWELL_WAITLIST_REPLY_TO",
        "DUNWELL_MAILING_ADDRESS",
        "DUNWELL_WAITLIST_WELCOME",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "waitlist-email.db")
    yield s
    s.close()


def make_app(store, mailer=None) -> Application:
    return Application(
        store=store,
        auth=AuthService(store),
        mailer=mailer or DryRunMailer(),
        signup_mode="open",
    )


def post(app, payload):
    return app.handle(
        "POST",
        "/api/waitlist",
        {"Content-Type": "application/json"},
        json.dumps(payload).encode("utf-8"),
        "203.0.113.9",
    )


def body_of(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_new_signup_sends_one_welcome_email(store) -> None:
    mailer = DryRunMailer()
    app = make_app(store, mailer)

    response = post(app, {"email": "Owner@Example.com", "source": "landing"})

    assert response.status == 200
    assert body_of(response)["welcome"] is True
    # Dry-run is honest about it: the message was built, not delivered.
    assert body_of(response)["mail"] == "recorded"
    assert len(mailer.sent) == 1
    message = mailer.sent[0]
    assert message.to == "owner@example.com"
    assert "You're on the Dunwell list" == message.subject
    assert "You're on the list." in message.html_body
    assert "14-day trial" in message.body
    assert message.html_body.startswith("<!doctype html>")


def test_duplicate_signup_does_not_send_a_second_email(store) -> None:
    mailer = DryRunMailer()
    app = make_app(store, mailer)

    post(app, {"email": "owner@example.com"})
    response = post(app, {"email": "owner@example.com"})

    assert body_of(response)["already"] is True
    assert "welcome" not in body_of(response)
    assert len(mailer.sent) == 1


def test_existing_address_without_a_welcome_gets_one_on_retry(store) -> None:
    """An address stored before this feature shipped is still owed an email."""
    store.add_waitlist("old@example.com", "landing")
    mailer = DryRunMailer()
    app = make_app(store, mailer)

    response = post(app, {"email": "old@example.com"})

    assert body_of(response)["already"] is True
    assert body_of(response)["welcome"] is True
    assert [m.to for m in mailer.sent] == ["old@example.com"]


def test_failed_send_does_not_break_the_signup_and_is_retryable(store) -> None:
    class BrokenMailer(DryRunMailer):
        def send(self, message):
            raise RuntimeError("mail server unavailable")

    broken = BrokenMailer()
    app = make_app(store, broken)

    first = post(app, {"email": "retry@example.com"})
    assert first.status == 200
    assert body_of(first)["ok"] is True

    healthy = DryRunMailer()
    app.mailer = healthy
    second = post(app, {"email": "retry@example.com"})

    assert body_of(second)["welcome"] is True
    assert [m.to for m in healthy.sent] == ["retry@example.com"]


def test_api_mode_delivers_off_the_request_thread(store) -> None:
    """An HTTP mail API must not hold the signup response open."""
    import threading

    class ApiFake:
        mode = "api"

        def __init__(self):
            self.sent = []
            self.done = threading.Event()

        def send(self, message):
            self.sent.append(message)
            self.done.set()
            return "api_test"

    mailer = ApiFake()
    app = make_app(store, mailer)

    response = post(app, {"email": "async@example.com"})

    assert response.status == 200
    assert body_of(response)["welcome"] is True
    assert body_of(response)["mail"] == "sent"
    assert mailer.done.wait(2.0)
    assert [m.to for m in mailer.sent] == ["async@example.com"]


def test_welcome_can_be_disabled(store, monkeypatch) -> None:
    monkeypatch.setenv("DUNWELL_WAITLIST_WELCOME", "0")
    mailer = DryRunMailer()
    app = make_app(store, mailer)

    response = post(app, {"email": "quiet@example.com"})

    assert response.status == 200
    assert "welcome" not in body_of(response)
    assert mailer.sent == []


def test_html_is_multipart_with_a_plain_text_fallback() -> None:
    message = waitlist_welcome(
        email="a@example.com",
        site_url="https://dunwell.test",
        reply_to="hello@dunwell.test",
    )
    email = message.as_email_message("hello@dunwell.test")

    assert email.is_multipart()
    parts = {part.get_content_type() for part in email.iter_parts()}
    assert parts == {"text/plain", "text/html"}
    assert "hello@dunwell.test" in email["From"]
    assert email["List-Unsubscribe"] == "<mailto:hello@dunwell.test?subject=unsubscribe>"


def test_template_escapes_dynamic_values_and_rejects_unsafe_links() -> None:
    text_body, html_body = render_waitlist_welcome(
        email="a@example.com",
        site_url="javascript:alert(1)",
        founder="<script>alert(1)</script>",
    )

    assert "<script>" not in html_body
    assert "javascript:alert" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "javascript:alert" not in text_body


def test_build_mailer_uses_the_https_api_when_a_key_is_present() -> None:
    mailer = build_mailer(
        {"DUNWELL_MAIL_MODE": "api", "DUNWELL_MAIL_API_KEY": "re_test"}
    )

    assert mailer.mode == "api"
    assert mailer.api_url == "https://api.resend.com/emails"
    assert mailer.api_key == "re_test"


def test_build_mailer_falls_back_when_the_api_key_is_missing() -> None:
    assert build_mailer({"DUNWELL_MAIL_MODE": "api"}).mode == "dryrun"


def test_api_mailer_posts_text_html_and_reply_to(monkeypatch) -> None:
    from dunwell import mailer as mailer_module

    captured: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit=-1):
            return b'{"id":"abc123"}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {
            key.lower(): value for key, value in request.header_items()
        }
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(mailer_module, "urlopen", fake_urlopen)
    mailer = mailer_module.ApiMailer(
        api_url="https://api.resend.test/emails",
        api_key="re_secret",
        from_address="hello@dunwell.test",
    )
    message = waitlist_welcome(
        email="a@example.com",
        site_url="https://dunwell.test",
        reply_to="hi@dunwell.test",
    )

    delivery = mailer.send(message)

    assert delivery == "api_abc123"
    assert captured["url"] == "https://api.resend.test/emails"
    assert captured["headers"]["authorization"] == "Bearer re_secret"
    assert captured["payload"]["to"] == ["a@example.com"]
    assert captured["payload"]["reply_to"] == "hi@dunwell.test"
    assert "Mahek from Dunwell" in captured["payload"]["from"]
    assert captured["payload"]["html"].startswith("<!doctype html>")
    assert "14-day trial" in captured["payload"]["text"]


def test_api_mailer_error_does_not_leak_the_key(monkeypatch) -> None:
    from dunwell import mailer as mailer_module

    def fake_urlopen(request, timeout):
        raise HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"message":"bad key re_secret"}'),
        )

    monkeypatch.setattr(mailer_module, "urlopen", fake_urlopen)
    mailer = mailer_module.ApiMailer(
        api_key="re_secret", from_address="hello@dunwell.test"
    )

    with pytest.raises(MailerError) as caught:
        mailer.send(waitlist_welcome(email="a@example.com"))

    assert "re_secret" not in str(caught.value)
    assert "401" in str(caught.value)


def test_mailtest_preview_writes_html_and_text(tmp_path, monkeypatch) -> None:
    from dunwell import mailtest

    monkeypatch.chdir(tmp_path)
    code = mailtest.main(["--preview", "out/welcome.html"])

    assert code == 0
    html_path = tmp_path / "out" / "welcome.html"
    text_path = tmp_path / "out" / "welcome.txt"
    assert "You're on the list." in html_path.read_text(encoding="utf-8")
    assert "14-day trial" in text_path.read_text(encoding="utf-8")


def test_mailtest_refuses_to_pretend_when_smtp_is_unset(tmp_path, monkeypatch) -> None:
    from dunwell import mailtest

    monkeypatch.chdir(tmp_path)
    assert mailtest.main(["--to", "a@example.com"]) == 2
    assert (tmp_path / "out" / "waitlist-welcome.html").is_file()
