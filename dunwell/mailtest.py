"""Send or preview the waitlist thank-you email.

Useful on a host where the web app is already running, because it exercises
exactly the message the service would send:

    python -m dunwell.mailtest --to you@example.com

If SMTP is not configured, nothing is sent and the HTML is written to disk so
the design can be checked locally:

    python -m dunwell.mailtest --preview out/waitlist-welcome.html

Exit codes: 0 sent / previewed, 1 delivery failed, 2 delivery not configured.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .emails import render_waitlist_welcome, waitlist_welcome
from .mailer import build_mailer


def _settings(to: str, site_url: str) -> dict:
    return {
        "email": to,
        "site_url": site_url,
        "founder": os.environ.get("DUNWELL_FOUNDER_NAME", "Mahek"),
        "brand": os.environ.get("DUNWELL_BRAND_NAME", "Dunwell"),
        "reply_to": (
            os.environ.get("DUNWELL_WAITLIST_REPLY_TO")
            or os.environ.get("MAIL_FROM")
            or ""
        ),
        "mailing_address": os.environ.get("DUNWELL_MAILING_ADDRESS", ""),
    }


def _write_preview(path: Path, text_body: str, html_body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_body, encoding="utf-8")
    text_path = path.with_suffix(".txt")
    text_path.write_text(text_body, encoding="utf-8")
    print(f"preview written: {path}")
    print(f"plain text:      {text_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dunwell.mailtest",
        description="Send or preview the Dunwell waitlist confirmation email.",
    )
    parser.add_argument(
        "--to",
        default="",
        help="recipient address; sends a real email when SMTP is configured",
    )
    parser.add_argument(
        "--preview",
        default="",
        help="write the HTML/text to this path instead of sending",
    )
    parser.add_argument(
        "--site-url",
        default=os.environ.get("DUNWELL_SITE_URL", ""),
        help="public landing URL used for the CTA (default: $DUNWELL_SITE_URL)",
    )
    args = parser.parse_args(argv)

    if not args.to and not args.preview:
        parser.error("give --to to send, or --preview to write the files")

    to = args.to or "you@example.com"
    settings = _settings(to, args.site_url)

    if args.preview:
        text_body, html_body = render_waitlist_welcome(**settings)
        _write_preview(Path(args.preview), text_body, html_body)
        return 0

    mailer = build_mailer()
    if mailer.mode == "dryrun":
        print(
            "Email delivery is not configured, so nothing was sent.\n"
            "Use DUNWELL_MAIL_MODE=api with DUNWELL_MAIL_API_KEY (recommended,\n"
            "works where SMTP is blocked), or DUNWELL_MAIL_MODE=smtp with\n"
            "SMTP_HOST/SMTP_USER/SMTP_PASSWORD. Writing a preview instead.",
            file=sys.stderr,
        )
        text_body, html_body = render_waitlist_welcome(**settings)
        _write_preview(Path("out/waitlist-welcome.html"), text_body, html_body)
        return 2

    message = waitlist_welcome(**settings)
    try:
        delivery_id = mailer.send(message)
    except Exception as exc:  # noqa: BLE001 - print the provider's reason
        print(f"send failed: {exc}", file=sys.stderr)
        return 1

    destination = getattr(mailer, "host", None) or getattr(mailer, "api_url", "api")
    print(f"sent to {to} via {destination} ({delivery_id})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
