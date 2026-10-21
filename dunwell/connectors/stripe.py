"""Stripe ledger connector.

Read side is implemented against the real API shape
(``GET /v1/customers``, ``GET /v1/invoices``). Write-back (recording that a
follow-up was sent) is declared unsupported until we own a store for it.

Live usage::

    StripeConnector(api_key=os.environ["STRIPE_API_KEY"]).fetch()

Offline/tests use a :class:`~dunwell.connectors.base.FixtureTransport`.
"""

from __future__ import annotations

from typing import Any

from ..core import Customer, Invoice, InvoiceStatus
from .base import (
    CUSTOMERS,
    INVOICES,
    READ,
    WRITE_BACK,
    ConnectorError,
    LedgerSnapshot,
    Transport,
    UnsupportedOperation,
    UrllibTransport,
    minor_to_cents,
    unix_to_date,
)

STRIPE_API = "https://api.stripe.com/v1"

# Stripe invoice status -> our status.
_STATUS_MAP: dict[str, InvoiceStatus] = {
    "open": InvoiceStatus.OPEN,
    "paid": InvoiceStatus.PAID,
    "void": InvoiceStatus.VOID,
    # Written off: pull out of automation and let a human decide.
    "uncollectible": InvoiceStatus.DISPUTED,
}

# Stripe statuses we never chase (nothing has been billed yet).
_SKIP_STATUSES = {"draft"}


def _as_bool(value: Any) -> bool:
    if value is True:
        return True
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _map_customer(raw: dict[str, Any]) -> Customer:
    metadata = raw.get("metadata") or {}
    return Customer(
        id=str(raw.get("id")),
        name=str(raw.get("name") or raw.get("email") or "Unknown customer"),
        email=(raw.get("email") or None),
        tier=str(metadata.get("tier") or "standard"),
        opted_out=_as_bool(metadata.get("opted_out")),
        relationship_note=str(metadata.get("relationship_note") or ""),
    )


def _map_invoice(
    raw: dict[str, Any],
    customers: dict[str, Customer],
    warnings: list[str],
) -> Invoice | None:
    status_key = str(raw.get("status") or "")
    if status_key in _SKIP_STATUSES:
        return None

    invoice_id = str(raw.get("id"))
    number = str(raw.get("number") or invoice_id)

    # Stripe bills in minor units already.
    amount_cents = minor_to_cents(raw.get("amount_due")) or minor_to_cents(
        raw.get("total")
    )

    issued_on = unix_to_date(raw.get("created"))
    due_on = unix_to_date(raw.get("due_date"))
    if due_on is None:
        due_on = issued_on
        warnings.append(
            f"stripe invoice {number}: no due_date, falling back to created date "
            f"{issued_on.isoformat() if issued_on else 'unknown'}"
        )
    if issued_on is None or due_on is None:
        warnings.append(f"stripe invoice {number}: missing dates, skipped")
        return None

    status = _STATUS_MAP.get(status_key, InvoiceStatus.OPEN)
    transitions = raw.get("status_transitions") or {}
    paid_on = unix_to_date(transitions.get("paid_at"))

    dispute_note = ""
    if status_key == "uncollectible":
        dispute_note = "Stripe marked this invoice uncollectible"

    customer_id = raw.get("customer") or raw.get("customer_email") or f"stripe:{invoice_id}"
    customer_id = str(customer_id)
    if customer_id not in customers:
        customers[customer_id] = Customer(
            id=customer_id,
            name=str(raw.get("customer_name") or raw.get("customer_email") or "Unknown customer"),
            email=(raw.get("customer_email") or None),
        )

    return Invoice(
        id=invoice_id,
        number=number,
        customer_id=customer_id,
        amount_cents=amount_cents,
        currency=str(raw.get("currency") or "usd").upper(),
        issued_on=issued_on,
        due_on=due_on,
        status=status,
        paid_on=paid_on,
        dispute_note=dispute_note,
        # Prior nudges are ours to store, not Stripe's.
        contacts=(),
    )


class StripeConnector:
    """Read Stripe invoices and customers into the domain model."""

    name = "stripe"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        transport: Transport | None = None,
        limit: int = 100,
        offline: bool = False,
    ) -> None:
        self.api_key = api_key
        self.transport: Transport = transport or UrllibTransport()
        self.limit = limit
        self.offline = offline

    def capabilities(self) -> frozenset[str]:
        return frozenset({READ, INVOICES, CUSTOMERS, WRITE_BACK})

    def _headers(self) -> dict[str, str]:
        if self.offline:
            return {}
        if not self.api_key:
            raise ConnectorError(
                "STRIPE_API_KEY is required for live Stripe access "
                "(pass api_key= or supply a FixtureTransport)"
            )
        return {"Authorization": f"Bearer {self.api_key}"}

    def fetch(self) -> LedgerSnapshot:
        warnings: list[str] = []
        headers = self._headers()

        customers: dict[str, Customer] = {}
        payload = self.transport.get_json(
            f"{STRIPE_API}/customers",
            headers=headers,
            params={"limit": self.limit},
        )
        for raw in payload.get("data", []):
            customer = _map_customer(raw)
            customers[customer.id] = customer

        invoices: list[Invoice] = []
        # One call returns every status; we partition locally.
        payload = self.transport.get_json(
            f"{STRIPE_API}/invoices",
            headers=headers,
            params={"limit": self.limit},
        )
        for raw in payload.get("data", []):
            invoice = _map_invoice(raw, customers, warnings)
            if invoice is not None:
                invoices.append(invoice)

        return LedgerSnapshot(
            invoices=invoices,
            customers=customers,
            source=self.name,
            warnings=tuple(warnings),
        )

    def record_contact(self, invoice_id: str, sent_on: Any) -> None:
        """Not implemented: Dunwell stores nudge history itself."""
        raise UnsupportedOperation(
            "Stripe write-back is not wired yet; nudge history lives in Dunwell's "
            "own store, not in Stripe."
        )
