"""Outbound mail for the Dunwell service.

Two implementations behind one interface:

- :class:`DryRunMailer` — default. Records the message and returns a delivery
  id without touching the network. Everything works end-to-end with no
  credentials, which is how the test suite and a local run behave.
- :class:`SmtpMailer` — real delivery over ``smtplib`` when SMTP env vars are
  set.

``build_mailer()`` picks based on the environment, so wiring live delivery is
a config change rather than a code change.
"""

from __future__ import annotations

import os
import smtplib
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class MailMessage:
    to: str
    subject: str
    body: str
    reply_to: str = ""
    from_name: str = ""

    def as_email_message(self, sender: str) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = self.subject
        msg["From"] = sender
        msg["To"] = self.to
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        msg.set_content(self.body)
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


def build_mailer(env: Mapping[str, str] | None = None) -> Mailer:
    """Choose a mailer from the environment.

    ``DUNWELL_MAIL_MODE`` is ``dryrun`` (default) or ``smtp``. SMTP mode
    requires ``SMTP_HOST``; without it we fall back to dry-run rather than
    failing at startup, and the fallback is reported through ``mode``.
    """
    environ = os.environ if env is None else env
    wanted = (environ.get("DUNWELL_MAIL_MODE") or "dryrun").strip().lower()
    from_address = (
        environ.get("MAIL_FROM") or environ.get("SMTP_FROM") or "billing@dunwell.app"
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
