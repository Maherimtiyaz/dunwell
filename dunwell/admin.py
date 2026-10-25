"""Operator commands for a deployment that does not allow self-serve signup.

With ``DUNWELL_SIGNUP_MODE=closed`` (the default) there is no HTTP route that
creates an account, which is the right posture for a public host: the signup
endpoint is unauthenticated, and an account can send mail. Accounts are then
created here, by the operator, on the host's shell - which is also how the
concierge onboarding works for the first customers.

    python -m dunwell.admin --email owner@studio.test

The password is prompted for rather than passed as a flag, so it does not end
up in shell history or in the process list.

    python -m dunwell.admin --email owner@studio.test --db /path/to/dunwell.db

``DUNWELL_DB`` is honoured, so on PythonAnywhere the same value the WSGI config
sets is the one used here.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from .auth import AuthError, hash_password, validate_email
from .store import Store, StoreError


def _prompt_password() -> str:
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise AuthError("passwords do not match")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dunwell.admin",
        description="Create an account on a deployment with signup closed.",
    )
    parser.add_argument("--email", required=True, help="Account email address")
    parser.add_argument(
        "--db",
        default=os.environ.get("DUNWELL_DB", "dunwell.db"),
        help="SQLite file (default: $DUNWELL_DB or ./dunwell.db)",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Password; prompted for when omitted (preferred)",
    )
    parser.add_argument("--company", default="", help="Company name used in drafts")
    parser.add_argument("--reply-to", default="", help="Reply-To for drafts")
    parser.add_argument("--signature", default="", help="Signature used in drafts")
    parser.add_argument("--payment-link", default="", help="Payment link used in drafts")
    args = parser.parse_args(argv)

    try:
        email = validate_email(args.email)
        password = args.password if args.password is not None else _prompt_password()
        password_hash = hash_password(password)
    except AuthError as exc:
        print(f"dunwell: {exc}", file=sys.stderr)
        return 2

    store = Store(args.db)
    try:
        account_id = store.create_account(
            email,
            password_hash,
            company_name=args.company,
            reply_to=args.reply_to,
            signature=args.signature,
            payment_link=args.payment_link,
        )
    except StoreError:
        # The constraint message embeds the email; this reads better and does
        # not depend on the wording of a driver error.
        print(f"dunwell: an account for {email} already exists", file=sys.stderr)
        return 1
    finally:
        store.close()

    print(f"created account {account_id} for {email}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
