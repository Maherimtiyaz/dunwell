"""The waitlist thank-you email.

One message, two renderings:

- plain text for clients that block HTML (and for deliverability scanners)
- a table-based HTML email with inline styles, no remote images, and no
  external fonts, so it renders in Gmail, Outlook, and Apple Mail without
  asking the recipient's client to fetch anything.

Every interpolated value is escaped. The address comes from user input, and
the founder name / site URL come from environment variables, but escaping all
of them costs nothing and keeps the template safe if a future field is
attacker-controlled.
"""

from __future__ import annotations

import html
from urllib.parse import urlsplit

from .mailer import MailMessage

BRAND = "Dunwell"


def _one_line(value: object, limit: int = 200) -> str:
    """Collapse whitespace and control characters, then bound the length."""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = "".join(ch for ch in text if ch == "\t" or ord(ch) >= 32)
    return text.strip()[:limit]


def _safe_site_url(raw: object) -> str:
    """Only absolute http(s) URLs may become a link in an email."""
    url = _one_line(raw, 300).rstrip("/")
    if not url:
        return ""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def render_waitlist_welcome(
    *,
    email: str,
    site_url: str = "",
    founder: str = "Mahek",
    brand: str = BRAND,
    reply_to: str = "",
    mailing_address: str = "",
) -> tuple[str, str]:
    """Return ``(text_body, html_body)`` for a waitlist confirmation."""
    brand = _one_line(brand, 60) or BRAND
    founder = _one_line(founder, 60) or "the " + brand + " team"
    address = _one_line(mailing_address, 200)
    reply_to = _one_line(reply_to, 254)
    site = _safe_site_url(site_url)
    email = _one_line(email, 254)

    cta_url = f"{site}/#how-it-works" if site else ""
    if not cta_url and reply_to:
        cta_url = f"mailto:{reply_to}?subject=Tell%20me%20more%20about%20{brand}"

    cta_text = ""
    if site:
        cta_text = f"See how {brand} decides: {site}/#how-it-works\n\n"

    footer_bits = [f"Sent by {brand}"]
    if address:
        footer_bits.append(address)
    if reply_to:
        footer_bits.append(f"Unsubscribe: mailto:{reply_to}?subject=unsubscribe")
    footer = " · ".join(footer_bits)

    text_body = (
        "Hi,\n"
        "\n"
        f"Thanks for joining the {brand} waitlist. You're in.\n"
        "\n"
        f"{brand} reads your overdue invoices and drafts the follow-up that is\n"
        "actually justified, using only the facts in your ledger. You approve\n"
        "every message before it goes out. If the facts do not support a\n"
        "message, it holds the invoice instead of inventing one.\n"
        "\n"
        "What happens next\n"
        "  1. We are onboarding a small group of agencies and studios first.\n"
        "  2. You will get one email when your invite is ready. No newsletter,\n"
        "     no drip sequence.\n"
        "  3. Early access includes a 14-day trial with no card, and the first\n"
        "     10 customers keep 50% off for life.\n"
        "\n"
        f"{cta_text}"
        "Want us to look at your ledger before then? Just reply to this email.\n"
        "A real person reads it.\n"
        "\n"
        f"- {founder}\n"
        f"{brand}\n"
        "\n"
        f"You joined the {brand} waitlist with {email}. If this was not you,\n"
        'reply with "remove" and we will delete it.\n'
        "\n"
        f"{footer}\n"
    )

    # Colours and spacing are deliberately inline: email clients strip
    # <style> blocks, and a table layout is still the only thing Outlook
    # renders consistently.
    font = (
        "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
        "Arial,sans-serif"
    )
    mark = html.escape(brand[:1].upper() or "D")
    safe_brand = html.escape(brand)
    safe_founder = html.escape(founder)
    safe_email = html.escape(email)

    footer_html = f"Sent by {safe_brand}"
    if address:
        footer_html += f" &middot; {html.escape(address)}"
    if reply_to:
        footer_html += (
            ' &middot; <a href="mailto:'
            + html.escape(reply_to, quote=True)
            + '?subject=unsubscribe" style="color:#9ca3af;text-decoration:underline;">'
            "Unsubscribe</a>"
        )

    if cta_url:
        cta = (
            '<tr><td align="center" style="padding:26px 34px 0 34px;">'
            f'<a href="{html.escape(cta_url, quote=True)}" '
            "style=\"display:inline-block;padding:14px 26px;background-color:"
            "#6366f1;border-radius:10px;color:#ffffff;font-family:" + font + ";"
            "font-size:15px;font-weight:600;line-height:18px;text-decoration:none;"
            '">See how it decides</a></td></tr>'
        )
    else:
        cta = ""

    html_body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<title>You're on the {safe_brand} list</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f5f9;">
<div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;color:#f4f5f9;">Thanks for joining. Here is what happens next.</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f5f9;">
  <tr>
    <td align="center" style="padding:36px 14px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background-color:#ffffff;border:1px solid #e5e7eb;border-radius:16px;">
        <tr>
          <td style="padding:30px 34px 0 34px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td width="36" height="36" align="center" valign="middle" style="background-color:#6366f1;border-radius:10px;color:#ffffff;font-family:{font};font-size:18px;font-weight:700;line-height:36px;">{mark}</td>
                <td style="padding-left:11px;font-family:{font};font-size:17px;font-weight:700;color:#111827;letter-spacing:-0.01em;">{safe_brand}</td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:26px 34px 0 34px;">
            <p style="margin:0 0 8px 0;font-family:{font};font-size:11px;font-weight:700;line-height:16px;letter-spacing:0.12em;text-transform:uppercase;color:#6366f1;">Early access</p>
            <h1 style="margin:0 0 14px 0;font-family:{font};font-size:30px;font-weight:700;line-height:38px;letter-spacing:-0.02em;color:#111827;">You're on the list.</h1>
            <p style="margin:0 0 14px 0;font-family:{font};font-size:16px;line-height:26px;color:#374151;">Thanks for asking to see {safe_brand} early. We are building the receivables agent that decides <em>whether</em> an invoice should be chased, not just how to phrase the reminder.</p>
            <p style="margin:0 0 22px 0;font-family:{font};font-size:16px;line-height:26px;color:#374151;">Every follow-up is grounded in your ledger and approved by you before it sends. If the facts do not support a message, {safe_brand} holds the invoice instead of inventing one.</p>
          </td>
        </tr>
        <tr>
          <td style="padding:0 34px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f8f9ff;border:1px solid #e0e7ff;border-radius:12px;">
              <tr><td style="padding:18px 20px 4px 20px;font-family:{font};font-size:12px;font-weight:700;line-height:18px;letter-spacing:0.08em;text-transform:uppercase;color:#4f46e5;">What happens next</td></tr>
              <tr><td style="padding:0 20px 16px 20px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td width="24" valign="top" style="padding:5px 0;font-family:{font};font-size:13px;font-weight:700;line-height:20px;color:#6366f1;">1</td>
                    <td style="padding:5px 0;font-family:{font};font-size:14px;line-height:21px;color:#374151;">We are onboarding a small group of agencies and studios first.</td>
                  </tr>
                  <tr>
                    <td width="24" valign="top" style="padding:5px 0;font-family:{font};font-size:13px;font-weight:700;line-height:20px;color:#6366f1;">2</td>
                    <td style="padding:5px 0;font-family:{font};font-size:14px;line-height:21px;color:#374151;">You will get one email when your invite is ready. No newsletter, no drip sequence.</td>
                  </tr>
                  <tr>
                    <td width="24" valign="top" style="padding:5px 0;font-family:{font};font-size:13px;font-weight:700;line-height:20px;color:#6366f1;">3</td>
                    <td style="padding:5px 0;font-family:{font};font-size:14px;line-height:21px;color:#374151;">Early access includes a 14-day trial with no card, and the first 10 customers keep 50% off for life.</td>
                  </tr>
                </table>
              </td></tr>
            </table>
          </td>
        </tr>
        {cta}
        <tr>
          <td style="padding:24px 34px 0 34px;">
            <p style="margin:0;font-family:{font};font-size:15px;line-height:24px;color:#374151;">Want us to look at your ledger before then? Just reply to this email. A real person reads it.</p>
            <p style="margin:18px 0 0 0;font-family:{font};font-size:15px;line-height:24px;color:#111827;">&mdash; {safe_founder}</p>
            <p style="margin:0;font-family:{font};font-size:13px;line-height:20px;color:#6b7280;">{safe_brand}</p>
          </td>
        </tr>
        <tr>
          <td style="padding:26px 0 0 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #e5e7eb;">
              <tr><td style="padding:18px 34px 26px 34px;">
                <p style="margin:0 0 6px 0;font-family:{font};font-size:12px;line-height:19px;color:#6b7280;">You joined the {safe_brand} waitlist with <strong style="color:#374151;">{safe_email}</strong>. If this was not you, reply with &ldquo;remove&rdquo; and we will delete it.</p>
                <p style="margin:0;font-family:{font};font-size:12px;line-height:19px;color:#9ca3af;">{footer_html}</p>
              </td></tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""

    return text_body, html_body


def waitlist_welcome(
    *,
    email: str,
    site_url: str = "",
    founder: str = "Mahek",
    brand: str = BRAND,
    reply_to: str = "",
    mailing_address: str = "",
) -> MailMessage:
    """Build the confirmation message sent after a waitlist signup."""
    text_body, html_body = render_waitlist_welcome(
        email=email,
        site_url=site_url,
        founder=founder,
        brand=brand,
        reply_to=reply_to,
        mailing_address=mailing_address,
    )
    brand = _one_line(brand, 60) or BRAND
    founder = _one_line(founder, 60)
    headers: list[tuple[str, str]] = [
        # A confirmation is transactional, but this stops vacation responders
        # from bouncing a reply back into the inbox.
        ("X-Auto-Response-Suppress", "All"),
    ]
    if reply_to:
        headers.append(
            ("List-Unsubscribe", f"<mailto:{_one_line(reply_to, 254)}?subject=unsubscribe>")
        )
    return MailMessage(
        to=email,
        subject=f"You're on the {brand} list",
        body=text_body,
        html_body=html_body,
        reply_to=reply_to,
        from_name=f"{founder} from {brand}" if founder else brand,
        headers=tuple(headers),
    )
