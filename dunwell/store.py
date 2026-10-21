"""SQLite persistence for the Dunwell service.

Standard library only (``sqlite3``). Every row is scoped by ``account_id`` and
all access goes through this module, so one tenant can never read another's
ledger.

The important design point is :meth:`Store.load_ledger`: it hydrates
``Invoice.contacts`` from the ``nudges`` table. That is what makes the
``cooldown`` and ``max_nudges`` policy rules fire on live data instead of
always seeing an empty history.

Connections: a single connection guarded by an ``RLock``. The HTTP service is
threaded, and SQLite writes serialize on the filesystem anyway, so one
connection plus a lock is both correct and simpler than a pool.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from .core import Customer, Invoice, InvoiceStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash  TEXT    NOT NULL,
    company_name   TEXT    NOT NULL DEFAULT '',
    reply_to       TEXT    NOT NULL DEFAULT '',
    signature      TEXT    NOT NULL DEFAULT '',
    payment_link   TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash     TEXT    PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at     TEXT    NOT NULL,
    expires_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    account_id         INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    id                 TEXT    NOT NULL,
    name               TEXT    NOT NULL,
    email              TEXT,
    tier               TEXT    NOT NULL DEFAULT 'standard',
    opted_out          INTEGER NOT NULL DEFAULT 0,
    relationship_note  TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (account_id, id)
);

CREATE TABLE IF NOT EXISTS invoices (
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    id           TEXT    NOT NULL,
    number       TEXT    NOT NULL,
    customer_id  TEXT    NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency     TEXT    NOT NULL,
    issued_on    TEXT    NOT NULL,
    due_on       TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    paid_on      TEXT,
    dispute_note TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (account_id, id)
);

-- One row per follow-up actually delivered. This is the durable nudge history
-- that feeds Invoice.contacts, so policy caps survive a redeploy.
--
-- The primary key is a generated id, not (account_id, invoice_id, sent_at):
-- two sends landing in the same clock tick must not collide, and ordering is
-- carried by sent_at's microsecond precision instead of by uniqueness.
CREATE TABLE IF NOT EXISTS nudges (
    nudge_id    TEXT    PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    invoice_id  TEXT    NOT NULL,
    stage       TEXT    NOT NULL,
    to_email    TEXT    NOT NULL,
    subject     TEXT    NOT NULL,
    delivery_id TEXT    NOT NULL,
    sent_at     TEXT    NOT NULL,
    sent_on     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS waitlist (
    email      TEXT PRIMARY KEY COLLATE NOCASE,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);
CREATE INDEX IF NOT EXISTS idx_nudges_invoice ON nudges(account_id, invoice_id);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _iso_precise(dt: datetime) -> str:
    """Microsecond ISO timestamp for rows whose order must survive a burst."""
    return dt.isoformat(timespec="microseconds")


def token_hash(token: str) -> str:
    """SHA-256 of a session token. We only ever store the digest."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class StoreError(RuntimeError):
    """Raised for constraint violations and unknown rows."""


class Store:
    """Thread-safe data access object over one SQLite file."""

    def __init__(self, path: str | Path = "dunwell.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            parent = Path(self.path).parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        # Row factories keep this module readable at the call sites.
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # Accounts
    # ------------------------------------------------------------------ #

    def create_account(
        self,
        email: str,
        password_hash: str,
        *,
        company_name: str = "",
        reply_to: str = "",
        signature: str = "",
        payment_link: str = "",
    ) -> int:
        now = _iso(_utc_now())
        try:
            with self._lock:
                cur = self._conn.execute(
                    """
                    INSERT INTO accounts
                        (email, password_hash, company_name, reply_to,
                         signature, payment_link, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        email,
                        password_hash,
                        company_name,
                        reply_to,
                        signature,
                        payment_link,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StoreError(f"account already exists: {email}") from exc
        return int(cur.lastrowid)

    def account_by_email(self, email: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM accounts WHERE email = ?", (email,)
            ).fetchone()

    def account_by_id(self, account_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()

    def update_account_profile(self, account_id: int, **fields: str) -> None:
        allowed = {"company_name", "reply_to", "signature", "payment_link"}
        unknown = set(fields) - allowed
        if unknown:
            raise StoreError(f"unknown profile fields: {sorted(unknown)}")
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE accounts SET {cols} WHERE id = ?",
                (*fields.values(), account_id),
            )

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #

    def create_session(self, account_id: int, token: str, ttl_seconds: int) -> str:
        now = _utc_now()
        expires = datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc)
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (token_hash, account_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash(token), account_id, _iso(now), _iso(expires)),
            )
        return token

    def session_account(self, token: str) -> int | None:
        """Return the owning account id for a live token, else None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT account_id, expires_at FROM sessions WHERE token_hash = ?",
                (token_hash(token),),
            ).fetchone()
        if row is None:
            return None
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= _utc_now():
            self.delete_session(token)
            return None
        return int(row["account_id"])

    def delete_session(self, token: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),)
            )

    # ------------------------------------------------------------------ #
    # Ledger
    # ------------------------------------------------------------------ #

    def replace_ledger(
        self,
        account_id: int,
        invoices: list[Invoice],
        customers: dict[str, Customer],
    ) -> tuple[int, int]:
        """Replace the account's whole ledger atomically."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "DELETE FROM invoices WHERE account_id = ?", (account_id,)
                )
                self._conn.execute(
                    "DELETE FROM customers WHERE account_id = ?", (account_id,)
                )
                for c in customers.values():
                    self._conn.execute(
                        """
                        INSERT INTO customers
                            (account_id, id, name, email, tier, opted_out,
                             relationship_note)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_id,
                            c.id,
                            c.name,
                            c.email,
                            c.tier,
                            1 if c.opted_out else 0,
                            c.relationship_note,
                        ),
                    )
                for i in invoices:
                    self._conn.execute(
                        """
                        INSERT INTO invoices
                            (account_id, id, number, customer_id, amount_cents,
                             currency, issued_on, due_on, status, paid_on,
                             dispute_note)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_id,
                            i.id,
                            i.number,
                            i.customer_id,
                            i.amount_cents,
                            i.currency,
                            i.issued_on.isoformat(),
                            i.due_on.isoformat(),
                            i.status.value,
                            i.paid_on.isoformat() if i.paid_on else None,
                            i.dispute_note,
                        ),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(invoices), len(customers)

    def load_ledger(self, account_id: int) -> tuple[list[Invoice], dict[str, Customer]]:
        """Load the ledger with ``contacts`` hydrated from nudge history.

        Hydrating contacts here is what lets ``decide()`` apply the cooldown
        window and the nudge cap using real sending history.
        """
        with self._lock:
            cust_rows = self._conn.execute(
                "SELECT * FROM customers WHERE account_id = ?", (account_id,)
            ).fetchall()
            inv_rows = self._conn.execute(
                "SELECT * FROM invoices WHERE account_id = ? ORDER BY due_on", 
                (account_id,),
            ).fetchall()
            nudge_rows = self._conn.execute(
                "SELECT invoice_id, sent_on FROM nudges "
                "WHERE account_id = ? ORDER BY sent_at",
                (account_id,),
            ).fetchall()

        customers = {
            r["id"]: Customer(
                id=r["id"],
                name=r["name"],
                email=r["email"],
                tier=r["tier"],
                opted_out=bool(r["opted_out"]),
                relationship_note=r["relationship_note"],
            )
            for r in cust_rows
        }

        contacts: dict[str, list[date]] = {}
        for r in nudge_rows:
            contacts.setdefault(r["invoice_id"], []).append(
                date.fromisoformat(r["sent_on"])
            )

        invoices: list[Invoice] = []
        for r in inv_rows:
            invoices.append(
                Invoice(
                    id=r["id"],
                    number=r["number"],
                    customer_id=r["customer_id"],
                    amount_cents=r["amount_cents"],
                    currency=r["currency"],
                    issued_on=date.fromisoformat(r["issued_on"]),
                    due_on=date.fromisoformat(r["due_on"]),
                    status=InvoiceStatus(r["status"]),
                    paid_on=date.fromisoformat(r["paid_on"]) if r["paid_on"] else None,
                    dispute_note=r["dispute_note"],
                    # Critical: previously always () for live data.
                    contacts=tuple(contacts.get(r["id"], ())),
                )
            )
        return invoices, customers

    # ------------------------------------------------------------------ #
    # Nudge history
    # ------------------------------------------------------------------ #

    def record_nudge(
        self,
        account_id: int,
        *,
        invoice_id: str,
        stage: str,
        to_email: str,
        subject: str,
        delivery_id: str,
        sent_on: date,
    ) -> str:
        now = _utc_now()
        nudge_id = secrets.token_urlsafe(12)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO nudges
                    (nudge_id, account_id, invoice_id, stage, to_email, subject,
                     delivery_id, sent_at, sent_on)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    nudge_id,
                    account_id,
                    invoice_id,
                    stage,
                    to_email,
                    subject,
                    delivery_id,
                    _iso_precise(now),
                    sent_on.isoformat(),
                ),
            )
        return nudge_id

    def nudges_for(self, account_id: int, invoice_id: str | None = None) -> list[dict]:
        query = (
            "SELECT invoice_id, stage, to_email, subject, delivery_id, sent_at, sent_on "
            "FROM nudges WHERE account_id = ?"
        )
        params: list = [account_id]
        if invoice_id is not None:
            query += " AND invoice_id = ?"
            params.append(invoice_id)
        query += " ORDER BY sent_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Waitlist
    # ------------------------------------------------------------------ #

    def add_waitlist(self, email: str, source: str) -> tuple[bool, int]:
        """Insert a signup. Returns ``(already_subscribed, count)``."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO waitlist (email, source, created_at) "
                "VALUES (?, ?, ?)",
                (email, source, _iso(_utc_now())),
            )
            already = cur.rowcount == 0
            count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM waitlist"
            ).fetchone()["n"]
        return already, int(count)

    def waitlist_count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) AS n FROM waitlist").fetchone()["n"]
            )
