"""The operator CLI: the only way to create an account when signup is closed.

Closing self-serve signup is only workable if provisioning is possible
somewhere. These tests prove the account it creates is a real one - it can log
in through the same AuthService the HTTP routes use.
"""

from __future__ import annotations

from dunwell import admin
from dunwell.auth import AuthService
from dunwell.store import Store


def test_creates_an_account_that_can_log_in(tmp_path) -> None:
    db = tmp_path / "admin.db"
    code = admin.main(
        ["--email", "Owner@Studio.test", "--password", "hunter2hunter2", "--db", str(db)]
    )
    assert code == 0

    store = Store(db)
    try:
        row = store.account_by_email("owner@studio.test")
        assert row is not None, "the email is normalised before it is stored"
        session = AuthService(store).login("owner@studio.test", "hunter2hunter2")
        assert session.email == "owner@studio.test"
    finally:
        store.close()


def test_stores_the_company_profile(tmp_path) -> None:
    """These strings are what the customer sees in the drafted email."""
    db = tmp_path / "profile.db"
    admin.main(
        [
            "--email", "owner@studio.test",
            "--password", "hunter2hunter2",
            "--db", str(db),
            "--company", "Studio Nine",
            "--reply-to", "billing@studionine.test",
            "--payment-link", "https://pay.studionine.test/inv",
        ]
    )

    store = Store(db)
    try:
        row = store.account_by_email("owner@studio.test")
        assert row["company_name"] == "Studio Nine"
        assert row["reply_to"] == "billing@studionine.test"
        assert row["payment_link"] == "https://pay.studionine.test/inv"
    finally:
        store.close()


def test_duplicate_is_a_clear_failure(tmp_path, capsys) -> None:
    db = tmp_path / "dup.db"
    args = ["--email", "owner@studio.test", "--password", "hunter2hunter2", "--db", str(db)]
    assert admin.main(args) == 0

    assert admin.main(args) == 1
    assert "already exists" in capsys.readouterr().err


def test_short_password_is_refused(tmp_path) -> None:
    db = tmp_path / "short.db"
    code = admin.main(["--email", "a@b.test", "--password", "short", "--db", str(db)])
    assert code == 2
    store = Store(db)
    try:
        assert store.account_by_email("a@b.test") is None
    finally:
        store.close()


def test_bad_email_is_refused(tmp_path) -> None:
    db = tmp_path / "bad.db"
    code = admin.main(["--email", "not-an-email", "--password", "hunter2hunter2", "--db", str(db)])
    assert code == 2
