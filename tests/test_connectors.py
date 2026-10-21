"""Connector tests: mapping, warnings, parity, and offline behaviour."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dunwell.agent import run
from dunwell.connectors import (
    CONNECTOR_TYPES,
    ConnectorError,
    CsvConnector,
    FixtureTransport,
    QuickBooksConnector,
    StripeConnector,
    UnsupportedOperation,
    XeroConnector,
    build_connector,
)
from dunwell.core import Company, InvoiceStatus, Reason, Stage

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "demo" / "fixtures"
DEMO = ROOT / "demo"

AS_OF = date(2026, 10, 16)
COMPANY = Company(
    name="Northwind Studio",
    reply_to="billing@northwind.example",
    signature="Northwind Studio - Billing",
    payment_link="https://pay.northwind.example/inv",
)


# --------------------------------------------------------------------------- #
# Stripe
# --------------------------------------------------------------------------- #


def stripe_snapshot():
    return build_connector("stripe", fixtures=FIXTURES / "stripe").fetch()


def test_stripe_maps_and_skips_drafts() -> None:
    snapshot = stripe_snapshot()
    assert snapshot.source == "stripe"
    # 8 invoices in the fixture, one of them a draft that is never chased.
    assert len(snapshot.invoices) == 8 - 1
    assert all(i.status is not InvoiceStatus.OPEN or i.number != "STR-2005" for i in snapshot.invoices)


def test_stripe_status_mapping() -> None:
    by_number = {i.number: i for i in stripe_snapshot().invoices}
    assert by_number["STR-2001"].status is InvoiceStatus.OPEN
    assert by_number["STR-2003"].status is InvoiceStatus.PAID
    assert by_number["STR-2003"].paid_on == date(2026, 10, 10)
    assert by_number["STR-2004"].status is InvoiceStatus.DISPUTED
    assert "uncollectible" in by_number["STR-2004"].dispute_note.lower()
    assert by_number["STR-2007"].status is InvoiceStatus.VOID


def test_stripe_amount_uses_minor_units() -> None:
    by_number = {i.number: i for i in stripe_snapshot().invoices}
    assert by_number["STR-2001"].amount_cents == 120000
    assert by_number["STR-2001"].currency == "USD"


def test_stripe_missing_due_date_warns_and_falls_back() -> None:
    snapshot = stripe_snapshot()
    by_number = {i.number: i for i in snapshot.invoices}
    invoice = by_number["STR-2006"]
    assert invoice.due_on == date(2026, 9, 10)  # falls back to created
    assert any("STR-2006" in w and "no due_date" in w for w in snapshot.warnings)


def test_stripe_customer_metadata_and_fallback() -> None:
    snapshot = stripe_snapshot()
    assert snapshot.customers["cus_ava"].tier == "strategic"
    assert snapshot.customers["cus_dana"].opted_out is True
    assert snapshot.customers["cus_noemail"].email is None
    # STR-2004 has no customer id, so one is synthesized from the invoice email.
    uncollectible = next(i for i in snapshot.invoices if i.number == "STR-2004")
    assert snapshot.customers[uncollectible.customer_id].email == "priya@northgate.example"


def test_stripe_end_to_end_decisions() -> None:
    snapshot = stripe_snapshot()
    report = run(snapshot.invoices, snapshot.customers, AS_OF, COMPANY)
    reasons = {a.invoice.number: a.decision.reason for a in report.actions}
    assert reasons["STR-2001"] is Reason.READY
    assert reasons["STR-2003"] is Reason.ALREADY_PAID
    assert reasons["STR-2004"] is Reason.DISPUTED
    assert reasons["STR-2006"] is Reason.NO_CONTACT
    assert reasons["STR-2007"] is Reason.VOID
    assert reasons["STR-2008"] is Reason.OPTED_OUT

    staged = {a.invoice.number: a.decision.stage for a in report.drafts}
    assert staged["STR-2001"] is Stage.COURTEOUS
    assert staged["STR-2002"] is Stage.FINAL


# --------------------------------------------------------------------------- #
# QuickBooks
# --------------------------------------------------------------------------- #


def qbo_snapshot():
    return build_connector("quickbooks", fixtures=FIXTURES / "quickbooks").fetch()


def test_quickbooks_maps_invoices_and_customers() -> None:
    snapshot = qbo_snapshot()
    assert snapshot.source == "quickbooks"
    assert len(snapshot.invoices) == 5
    # 3 customers in the fixture + 1 synthesized for the unknown CustomerRef 99.
    assert len(snapshot.customers) == 4


def test_quickbooks_balance_zero_is_paid() -> None:
    by_number = {i.number: i for i in qbo_snapshot().invoices}
    assert by_number["QBO-1002"].status is InvoiceStatus.PAID
    assert by_number["QBO-1002"].amount_cents == 0
    assert by_number["QBO-1001"].amount_cents == 120000


def test_quickbooks_dispute_marker() -> None:
    snapshot = qbo_snapshot()
    invoice = next(i for i in snapshot.invoices if i.number == "QBO-1003")
    assert invoice.status is InvoiceStatus.DISPUTED
    assert invoice.dispute_note == "scope disagreement on line items"

    report = run(snapshot.invoices, snapshot.customers, AS_OF, COMPANY)
    decision = next(a.decision for a in report.actions if a.invoice.number == "QBO-1003")
    assert decision.reason is Reason.DISPUTED
    assert decision.stage is Stage.ESCALATE


def test_quickbooks_defaults_currency_and_flags_no_contact() -> None:
    snapshot = qbo_snapshot()
    by_number = {i.number: i for i in snapshot.invoices}
    assert by_number["QBO-1004"].currency == "USD"  # no CurrencyRef on the row
    report = run(snapshot.invoices, snapshot.customers, AS_OF, COMPANY)
    decision = next(a.decision for a in report.actions if a.invoice.number == "QBO-1004")
    assert decision.reason is Reason.NO_CONTACT


def test_quickbooks_query_routing() -> None:
    """Both queries share /query, so the fixture routes must disambiguate."""
    transport = FixtureTransport(
        FIXTURES / "quickbooks", routes={"Customer": "customers.json", "Invoice": "invoices.json"}
    )
    connector = QuickBooksConnector(transport=transport, offline=True)
    connector.fetch()
    assert any("from+Customer" in call for call in transport.calls)
    assert any("from+Invoice" in call for call in transport.calls)


# --------------------------------------------------------------------------- #
# Xero
# --------------------------------------------------------------------------- #


def xero_snapshot():
    return build_connector("xero", fixtures=FIXTURES / "xero").fetch()


def test_xero_filters_payables_and_drafts() -> None:
    snapshot = xero_snapshot()
    numbers = {i.number for i in snapshot.invoices}
    assert numbers == {"X-1001", "X-1002", "X-1005", "X-1006"}  # ACCPAY + DRAFT dropped


def test_xero_amounts_and_paid_date() -> None:
    by_number = {i.number: i for i in xero_snapshot().invoices}
    assert by_number["X-1001"].amount_cents == 120000
    assert by_number["X-1002"].status is InvoiceStatus.PAID
    assert by_number["X-1002"].paid_on == date(2026, 10, 10)


def test_xero_dispute_marker_and_warning() -> None:
    snapshot = xero_snapshot()
    disputed = next(i for i in snapshot.invoices if i.number == "X-1006")
    assert disputed.status is InvoiceStatus.DISPUTED
    assert disputed.dispute_note == "scope disagreement on line items"
    assert any("X-1005" in w for w in snapshot.warnings)  # no due date

    report = run(snapshot.invoices, snapshot.customers, AS_OF, COMPANY)
    decision = next(a.decision for a in report.actions if a.invoice.number == "X-1006")
    assert decision.reason is Reason.DISPUTED


# --------------------------------------------------------------------------- #
# Shared behaviour
# --------------------------------------------------------------------------- #


def test_csv_connector_parity_with_demo_ledger() -> None:
    snapshot = CsvConnector(DEMO).fetch()
    assert snapshot.source == "csv"
    assert len(snapshot.invoices) == 13
    assert snapshot.warnings == ()
    assert "c1" in snapshot.customers


def test_capabilities_are_honest() -> None:
    assert {"read", "invoices", "customers", "write_back"} <= StripeConnector().capabilities()
    assert "write_back" not in CsvConnector().capabilities()
    assert "invoices" in QuickBooksConnector().capabilities()
    assert "invoices" in XeroConnector().capabilities()


def test_live_connectors_require_credentials_without_network() -> None:
    with pytest.raises(ConnectorError):
        StripeConnector().fetch()
    with pytest.raises(ConnectorError):
        QuickBooksConnector().fetch()
    with pytest.raises(ConnectorError):
        XeroConnector().fetch()


def test_write_back_is_declared_unsupported() -> None:
    with pytest.raises(UnsupportedOperation):
        StripeConnector().record_contact("in_1", AS_OF)
    with pytest.raises(UnsupportedOperation):
        QuickBooksConnector().record_contact("501", AS_OF)


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(ConnectorError):
        build_connector("oracle")
    assert set(CONNECTOR_TYPES) == {"csv", "stripe", "quickbooks", "xero"}


def test_missing_fixture_raises() -> None:
    connector = build_connector("stripe", fixtures=ROOT / "demo" / "fixtures" / "nope")
    with pytest.raises(ConnectorError):
        connector.fetch()


def test_all_sources_produce_a_report() -> None:
    """Every connector feeds the same engine unchanged."""
    for source, fixture in (
        ("csv", DEMO),
        ("stripe", FIXTURES / "stripe"),
        ("quickbooks", FIXTURES / "quickbooks"),
        ("xero", FIXTURES / "xero"),
    ):
        connector = (
            CsvConnector(DEMO) if source == "csv" else build_connector(source, fixtures=fixture)
        )
        snapshot = connector.fetch()
        report = run(snapshot.invoices, snapshot.customers, AS_OF, COMPANY)
        assert report.actions, f"{source} produced no actions"
        assert report.drafts, f"{source} produced no drafts"
