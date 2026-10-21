"""Xero connector (accounts-receivable invoices).

Read side targets ``GET https://api.xero.com/api.xro/2.0/Invoices`` with a
bearer token and an ``xero-tenant-id`` header. Only ``ACCREC`` (accounts
receivable / sales) invoices are mapped — bills we owe are out of scope.

Live usage::

    XeroConnector(access_token=..., tenant_id=...).fetch()
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..core import Customer, Invoice, InvoiceStatus
from .base import (
    INVOICES,
    READ,
    WRITE_BACK,
    ConnectorError,
    LedgerSnapshot,
    Transport,
    UnsupportedOperation,
    UrllibTransport,
    iso_to_date,
    to_cents,
)

XERO_API = "https://api.xero.com/api.xro/2.0"

_STATUS_MAP: dict[str, InvoiceStatus] = {
    "authorised": InvoiceStatus.OPEN,
    "paid": InvoiceStatus.PAID,
    "voided": InvoiceStatus.VOID,
    "deleted": InvoiceStatus.VOID,
}
_SKIP_STATUSES = {"draft", "submitted"}

# Xero has no "disputed" status; honour an explicit marker in Reference, the
# same convention the QuickBooks connector uses, so behaviour is consistent.
DISPUTE_PREFIX = "DISPUTED:"

_DATE_MS = re.compile(r"/Date\((\d+)")


def _xero_date(value: Any):
    """Xero returns either ISO strings or ``/Date(ms)/`` depending on version."""
    if value in (None, ""):
        return None
    text = str(value)
    match = _DATE_MS.search(text)
    if match:
        return datetime.fromtimestamp(
            int(match.group(1)) / 1000, tz=timezone.utc
        ).date()
    return iso_to_date(text)


def _map_customer(raw_contact: dict[str, Any]) -> Customer:
    return Customer(
        id=str(raw_contact.get("ContactID")),
        name=str(raw_contact.get("Name") or "Unknown customer"),
        email=(raw_contact.get("EmailAddress") or None),
    )


def _map_invoice(
    raw: dict[str, Any],
    customers: dict[str, Customer],
    warnings: list[str],
) -> Invoice | None:
    if str(raw.get("Type") or "").upper() != "ACCREC":
        return None

    status_key = str(raw.get("Status") or "").lower()
    if status_key in _SKIP_STATUSES:
        return None

    invoice_id = str(raw.get("InvoiceID"))
    number = str(raw.get("InvoiceNumber") or invoice_id)

    issued_on = _xero_date(raw.get("DateString") or raw.get("Date"))
    due_on = _xero_date(raw.get("DueDateString") or raw.get("DueDate"))
    if due_on is None:
        due_on = issued_on
        warnings.append(
            f"xero invoice {number}: no due date, falling back to issue date "
            f"{issued_on.isoformat() if issued_on else 'unknown'}"
        )
    if issued_on is None or due_on is None:
        warnings.append(f"xero invoice {number}: missing dates, skipped")
        return None

    contact = raw.get("Contact") or {}
    customer_id = str(contact.get("ContactID") or "unknown")
    if customer_id not in customers:
        customers[customer_id] = _map_customer(contact)

    amount_cents = to_cents(
        raw.get("AmountDue") if raw.get("AmountDue") is not None else raw.get("Total")
    )

    status = _STATUS_MAP.get(status_key, InvoiceStatus.OPEN)
    reference = str(raw.get("Reference") or "")
    dispute_note = ""
    if reference.upper().startswith(DISPUTE_PREFIX):
        status = InvoiceStatus.DISPUTED
        dispute_note = reference[len(DISPUTE_PREFIX) :].strip()

    return Invoice(
        id=invoice_id,
        number=number,
        customer_id=customer_id,
        amount_cents=amount_cents,
        currency=str(raw.get("CurrencyCode") or "USD").upper(),
        issued_on=issued_on,
        due_on=due_on,
        status=status,
        paid_on=_xero_date(raw.get("FullyPaidOnDate")),
        dispute_note=dispute_note,
        contacts=(),
    )


class XeroConnector:
    """Read Xero accounts-receivable invoices into the domain model."""

    name = "xero"

    def __init__(
        self,
        access_token: str | None = None,
        tenant_id: str | None = None,
        *,
        transport: Transport | None = None,
        page_size: int = 100,
        offline: bool = False,
    ) -> None:
        self.access_token = access_token
        self.tenant_id = tenant_id
        self.transport: Transport = transport or UrllibTransport()
        self.page_size = page_size
        self.offline = offline

    def capabilities(self) -> frozenset[str]:
        return frozenset({READ, INVOICES, WRITE_BACK})

    def _headers(self) -> dict[str, str]:
        if self.offline:
            return {}
        if not self.access_token or not self.tenant_id:
            raise ConnectorError(
                "XERO_ACCESS_TOKEN and XERO_TENANT_ID are required for live "
                "Xero access (or supply a FixtureTransport)"
            )
        return {
            "Authorization": f"Bearer {self.access_token}",
            "xero-tenant-id": self.tenant_id,
            "Accept": "application/json",
        }

    def fetch(self) -> LedgerSnapshot:
        warnings: list[str] = []
        customers: dict[str, Customer] = {}
        invoices: list[Invoice] = []

        payload = self.transport.get_json(
            f"{XERO_API}/Invoices",
            headers=self._headers(),
            params={"page": 1, "pageSize": self.page_size},
        )
        for raw in payload.get("Invoices", []):
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
            "Xero write-back is not wired yet; nudge history lives in Dunwell's "
            "own store."
        )
