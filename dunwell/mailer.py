"""Outbound mail for the Dunwell service.

Three implementations behind one interface:

- :class:`DryRunMailer` — default. Records the message and returns a delivery
  id without touching the network. Everything works end-to-end with no
  credentials, which is how the test suite and a local run behave.
- :class:`SmtpMailer` — delivery over ``smtplib``. Simple, but most free PaaS
  tiers block outbound ports 25/465/587.
- :class:`ApiMailer` — delivery over an HTTPS JSON API (Resend by default).
  This is the mode that works on hosts which block SMTP, because it uses 443.

``build_mailer()`` picks based on the environment, so wiring live delivery is
a config change rather than a code change.
"""

from __future__ import annotations

import json
import os
import smtplib
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from typing import Mapping, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class MailMessage:
    to: str
    subject: str
    body: str
    reply_to: str = ""
    from_name: str = ""
    # Optional rich version. When present the message is multipart/alternative,
    # so a client that blocks HTML still gets the plain-text body.
    html_body: str = ""
    # Extra RFC 5322 headers, e.g. List-Unsubscribe. A tuple keeps the frozen
    # dataclass immutable and hashable.
    headers: tuple[tuple[str, str], ...] = ()

    def as_email_message(self, sender: str) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = self.subject
        msg["From"] = formataddr((self.from_name, sender)) if self.from_name else sender
        msg["To"] = self.to
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        for name, value in self.headers:
            msg[name] = value
        msg.set_content(self.body)
        if self.html_body:
            msg.add_alternative(self.html_body, subtype="html")
        return msg


@runtime_checkable
class Mailer(Protocol):
    mode: str

    def send(self, message: MailMessage) -> str:
        """Send one message and return a delivery id."""
        ...


@dataclass
class DryRunMailer:
    """Records messages in memory. Default; needs no credentials."""

    mode: str = "dryrun"
    from_address: str = "billing@dunwell.local"
    sent: list[MailMessage] = field(default_factory=list)

    def send(self, message: MailMessage) -> str:
        if not message.to or "@" not in message.to:
            raise ValueError("refusing to send without a valid recipient")
        self.sent.append(message)
        return f"dry_{uuid.uuid4().hex[:16]}"


class SmtpMailer:
    """Real delivery over SMTP.

    Reads host/port/credentials from the environment at construction so the
    service can start before secrets exist.
    """

    mode = "smtp"

    def __init__(
        self,
        host: str,
        port: int = 587,
        username: str = "",
        password: str = "",
        use_tls: bool = True,
        from_address: str = "billing@dunwell.app",
        timeout: float = 20.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.from_address = from_address
        self.timeout = timeout

    def send(self, message: MailMessage) -> str:
        if not message.to or "@" not in message.to:
            raise ValueError("refusing to send without a valid recipient")
        email = message.as_email_message(self.from_address)
        delivery_id = f"smtp_{uuid.uuid4().hex[:16]}"
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as client:
            if self.use_tls:
                client.starttls()
            if self.username:
                client.login(self.username, self.password)
            client.send_message(email)
        return delivery_id


class MailerError(RuntimeError):
    """Delivery failed. Safe to log: the message never contains the API key."""


@dataclass
class ApiMailer:
    """Delivery over an HTTPS JSON API, for hosts that block SMTP.

    The default endpoint is Resend, whose payload is the common shape:
    ``from``, ``to``, ``subject``, ``text``, ``html``, ``reply_to``. Point
    ``DUNWELL_MAIL_API_URL`` at a compatible provider if you switch.

    The key travels in the Authorization header, never in the URL, so it does
    not end up in proxy logs or error messages.
    """

    api_url: str = "https://api.resend.com/emails"
    api_key: str = ""
    from_address: str = "billing@dunwell.app"
    timeout: float = 20.0
    mode: str = "api"

    def send(self, message: MailMessage) -> str:
        if not message.to or "@" not in message.to:
            raise ValueError("refusing to send without a valid recipient")
        if not self.api_key:
            raise MailerError("mail API key is not configured")

        sender = (
            formataddr((message.from_name, self.from_address))
            if message.from_name
            else self.from_address
        )
        payload: dict[str, object] = {
            "from": sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.body,
        }
        if message.html_body:
            payload["html"] = message.html_body
        if message.reply_to:
            payload["reply_to"] = message.reply_to

        request = Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "dunwell/1.0",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(64_000)
        except HTTPError as exc:
            # Never attach the response body: a provider may echo the request,
            # and this exception is written to the service log.
            raise MailerError(f"mail API returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise MailerError(f"mail API unreachable: {exc.reason}") from exc

        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        delivery_id = parsed.get("id") if isinstance(parsed, dict) else None
        return f"api_{delivery_id or uuid.uuid4().hex[:16]}"


def build_mailer(env: Mapping[str, str] | None = None) -> Mailer:
    """Choose a mailer from the environment.

    ``DUNWELL_MAIL_MODE`` is ``dryrun`` (default), ``smtp``, or ``api``.
    Both live modes fail safe to dry-run when their required setting is
    missing, and the fallback is visible through ``mode``.
    """
    environ = os.environ if env is None else env
    wanted = (environ.get("DUNWELL_MAIL_MODE") or "dryrun").strip().lower()
    from_address = (
        environ.get("MAIL_FROM") or environ.get("SMTP_FROM") or "billing@dunwell.app"
    )

    if wanted == "api":
        key = (
            environ.get("DUNWELL_MAIL_API_KEY")
            or environ.get("RESEND_API_KEY")
            or ""
        ).strip()
        if not key:
            # Fail safe, same as SMTP without a host: record rather than drop.
            return DryRunMailer(from_address=from_address)
        return ApiMailer(
            api_url=(
                environ.get("DUNWELL_MAIL_API_URL")
                or "https://api.resend.com/emails"
            ).strip(),
            api_key=key,
            from_address=from_address,
        )

    if wanted == "smtp":
        host = environ.get("SMTP_HOST") or ""
        if not host:
            # Fail safe: never let a misconfiguration silently drop mail.
            return DryRunMailer(from_address=from_address)
        port_raw = environ.get("SMTP_PORT") or "587"
        try:
            port = int(port_raw)
        except ValueError:
            port = 587
        return SmtpMailer(
            host=host,
            port=port,
            username=environ.get("SMTP_USER") or "",
            password=environ.get("SMTP_PASSWORD") or "",
            use_tls=(environ.get("SMTP_TLS") or "1").lower() not in {"0", "false", "no"},
            from_address=from_address,
        )

    return DryRunMailer(from_address=from_address)
