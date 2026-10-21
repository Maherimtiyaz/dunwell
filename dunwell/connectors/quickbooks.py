"""QuickBooks Online connector.

Read side targets the QBO Accounting API (``/v3/company/{realmId}/query``)
which returns SQL-ish result envelopes. Auth is a bearer access token plus the
company ``realmId``; both come from an OAuth2 flow we do not own yet.

Live usage::

    QuickBooksConnector(access_token=..., realm_id=...).fetch()

Offline/tests use a :class:`~dunwell.connectors.base.FixtureTransport` with a
``routes`` map, because every call shares the same ``/query`` path:

    FixtureTransport("data/qbo", routes={"Customer": "customers.json",
                                         "Invoice": "invoices.json"})
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
    iso_to_date,
    to_cents,
)

QBO_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
QBO_PRODUCTION_BASE = "https://quickbooks.api.intuit.com"
QBO_MINOR_VERSION = "65"

# QuickBooks has no "disputed" status, and guessing from free text would be
# unsafe. Instead we honour an explicit, documented marker in PrivateNote:
# a note starting with ``DISPUTED:`` pulls the invoice out of automation.
DISPUTE_PREFIX = "DISPUTED:"


def _map_customer(raw: dict[str, Any]) -> Customer:
    email_block = raw.get("PrimaryEmailAddr") or {}
    return Customer(
        id=str(raw.get("Id")),
        name=str(raw.get("DisplayName") or raw.get("CompanyName") or "Unknown customer"),
        email=(email_block.get("Address") or None),
        tier="standard",
        opted_out=False,
        relationship_note=str(raw.get("Notes") or ""),
    )


def _map_invoice(
    raw: dict[str, Any],
    customers: dict[str, Customer],
    warnings: list[str],
) -> Invoice | None:
    invoice_id = str(raw.get("Id"))
    number = str(raw.get("DocNumber") or invoice_id)

    issued_on = iso_to_date(raw.get("TxnDate"))
    due_on = iso_to_date(raw.get("DueDate"))
    if due_on is None:
        due_on = issued_on
        warnings.append(
            f"quickbooks invoice {number}: no DueDate, falling back to TxnDate "
            f"{issued_on.isoformat() if issued_on else 'unknown'}"
        )
    if issued_on is None or due_on is None:
        warnings.append(f"quickbooks invoice {number}: missing dates, skipped")
        return None

    # Balance is the remaining amount owed; TotalAmt is the original.
    balance = raw.get("Balance")
    amount_cents = to_cents(balance if balance is not None else raw.get("TotalAmt"))

    status = InvoiceStatus.OPEN
    try:
        if float(balance if balance is not None else 1) <= 0:
            status = InvoiceStatus.PAID
    except (TypeError, ValueError):
        pass

    note = str(raw.get("PrivateNote") or "")
    dispute_note = ""
    if note.upper().startswith(DISPUTE_PREFIX):
        status = InvoiceStatus.DISPUTED
        dispute_note = note[len(DISPUTE_PREFIX) :].strip()

    currency_block = raw.get("CurrencyRef") or {}
    currency = str(currency_block.get("value") or "USD").upper()

    customer_ref = raw.get("CustomerRef") or {}
    customer_id = str(customer_ref.get("value") or "unknown")
    if customer_id not in customers:
        customers[customer_id] = Customer(
            id=customer_id,
            name=str(customer_ref.get("name") or "Unknown customer"),
            email=None,  # no email on the invoice; the Customer record carries it
        )

    return Invoice(
        id=invoice_id,
        number=number,
        customer_id=customer_id,
        amount_cents=amount_cents,
        currency=currency,
        issued_on=issued_on,
        due_on=due_on,
        status=status,
        paid_on=None,  # requires the linked Payment document
        dispute_note=dispute_note,
        contacts=(),
    )


class QuickBooksConnector:
    """Read QuickBooks Online invoices and customers into the domain model."""

    name = "quickbooks"

    def __init__(
        self,
        access_token: str | None = None,
        realm_id: str | None = None,
        *,
        transport: Transport | None = None,
        sandbox: bool = False,
        page_size: int = 1000,
        offline: bool = False,
    ) -> None:
        self.access_token = access_token
        self.realm_id = realm_id
        self.transport: Transport = transport or UrllibTransport()
        self.sandbox = sandbox
        self.page_size = page_size
        self.offline = offline

    def capabilities(self) -> frozenset[str]:
        return frozenset({READ, INVOICES, CUSTOMERS, WRITE_BACK})

    @property
    def base_url(self) -> str:
        return QBO_SANDBOX_BASE if self.sandbox else QBO_PRODUCTION_BASE

    def _headers(self) -> dict[str, str]:
        if self.offline:
            return {}
        if not self.access_token or not self.realm_id:
            raise ConnectorError(
                "QBO_ACCESS_TOKEN and QBO_REALM_ID are required for live "
                "QuickBooks access (or supply a FixtureTransport)"
            )
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    def _query(self, statement: str) -> dict[str, Any]:
        payload = self.transport.get_json(
            f"{self.base_url}/v3/company/{self.realm_id}/query",
            headers=self._headers(),
            params={"query": statement, "minorversion": QBO_MINOR_VERSION},
        )
        response = payload.get("QueryResponse")
        if response is None:
            raise ConnectorError(
                f"unexpected QuickBooks response (no QueryResponse) for: {statement}"
            )
        return response

    def fetch(self) -> LedgerSnapshot:
        warnings: list[str] = []
        headers = self._headers()  # validate credentials once, up front

        customers: dict[str, Customer] = {}
        response = self._query(
            f"select * from Customer maxresults {self.page_size}"
        )
        for raw in response.get("Customer", []):
            customer = _map_customer(raw)
            customers[customer.id] = customer

        invoices: list[Invoice] = []
        response = self._query(
            f"select * from Invoice maxresults {self.page_size}"
        )
        for raw in response.get("Invoice", []):
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
        """Intentionally unsupported until we own nudge-history storage."""
        raise UnsupportedOperation(
            "QuickBooks write-back is not wired yet; nudge history lives in "
            "Dunwell's own store."
        )
