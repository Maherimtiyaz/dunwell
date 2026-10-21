from datetime import date

import pytest

from dunwell.agent import run
from dunwell.core import (
    Company,
    Customer,
    Invoice,
    InvoiceStatus,
    Reason,
    Stage,
    aging_bucket,
    decide,
    draft,
)

AS_OF = date(2026, 10, 16)
COMPANY = Company(
    name="Northwind Studio",
    reply_to="billing@northwind.example",
    signature="Northwind Studio - Billing",
    payment_link="https://pay.northwind.example/inv",
)


def inv(**kw) -> Invoice:
    base = dict(
        id="i1",
        number="INV-1",
        customer_id="c1",
        amount_cents=120000,
        currency="USD",
        issued_on=date(2026, 9, 1),
        due_on=date(2026, 9, 30),
    )
    base.update(kw)
    return Invoice(**base)


def cust(**kw) -> Customer:
    base = dict(id="c1", name="Ava Chen", email="ava@lumenlabs.io")
    base.update(kw)
    return Customer(**base)


def test_paid_is_held() -> None:
    d = decide(inv(status=InvoiceStatus.PAID, paid_on=date(2026, 10, 1)), cust(), AS_OF)
    assert d.action == "hold"
    assert d.reason == Reason.ALREADY_PAID


def test_void_is_held() -> None:
    d = decide(inv(status=InvoiceStatus.VOID), cust(), AS_OF)
    assert d.reason == Reason.VOID


def test_not_due() -> None:
    d = decide(inv(due_on=date(2026, 11, 1)), cust(), AS_OF)
    assert d.reason == Reason.NOT_DUE


def test_disputed_escalates() -> None:
    d = decide(inv(status=InvoiceStatus.DISPUTED, dispute_note="scope"), cust(), AS_OF)
    assert d.stage == Stage.ESCALATE
    assert d.reason == Reason.DISPUTED
    assert "scope" in d.notes


def test_opted_out() -> None:
    d = decide(inv(), cust(opted_out=True), AS_OF)
    assert d.reason == Reason.OPTED_OUT


def test_no_contact_escalates() -> None:
    d = decide(inv(), cust(email=None), AS_OF)
    assert d.reason == Reason.NO_CONTACT
    assert d.stage == Stage.ESCALATE


def test_too_small() -> None:
    d = decide(inv(amount_cents=1500), cust(), AS_OF)
    assert d.reason == Reason.TOO_SMALL


def test_cooldown() -> None:
    d = decide(inv(contacts=(date(2026, 10, 14),)), cust(), AS_OF)
    assert d.reason == Reason.COOLDOWN


def test_max_nudges() -> None:
    d = decide(
        inv(contacts=(date(2026, 9, 1), date(2026, 9, 10), date(2026, 9, 20))),
        cust(),
        AS_OF,
    )
    assert d.reason == Reason.MAX_NUDGES
    assert d.stage == Stage.ESCALATE


def test_escalation_age() -> None:
    d = decide(
        inv(due_on=date(2026, 6, 30), contacts=(date(2026, 7, 10), date(2026, 7, 20))),
        cust(),
        AS_OF,
    )
    assert d.reason == Reason.OVER_ESCALATION_AGE


def test_courteous_first_nudge() -> None:
    d = decide(inv(), cust(), AS_OF)
    assert d.action == "draft"
    assert d.stage == Stage.COURTEOUS


def test_firm_second_nudge() -> None:
    d = decide(inv(due_on=date(2026, 9, 19), contacts=(date(2026, 9, 25),)), cust(), AS_OF)
    assert d.stage == Stage.FIRM


def test_final_when_old() -> None:
    d = decide(inv(due_on=date(2026, 8, 31), contacts=(date(2026, 9, 10),)), cust(), AS_OF)
    assert d.stage == Stage.FINAL


def test_strategic_note() -> None:
    d = decide(inv(), cust(tier="strategic"), AS_OF)
    assert any("strategic" in n for n in d.notes)


def test_draft_is_grounded() -> None:
    invoice = inv()
    customer = cust()
    d = decide(invoice, customer, AS_OF)
    dr = draft(d, invoice, customer, COMPANY, AS_OF)
    assert dr is not None
    assert "INV-1" in dr.subject
    assert "1,200.00" in dr.body
    assert "2026-09-30" in dr.body
    assert dr.to == "ava@lumenlabs.io"
    assert any("INV-1" in f for f in dr.facts)


def test_draft_refused_on_hold() -> None:
    invoice = inv(status=InvoiceStatus.PAID)
    customer = cust()
    d = decide(invoice, customer, AS_OF)
    assert draft(d, invoice, customer, COMPANY, AS_OF) is None


def test_aging_bucket_edges() -> None:
    assert aging_bucket(0) == "current"
    assert aging_bucket(-5) == "current"
    assert aging_bucket(30) == "1-30"
    assert aging_bucket(31) == "31-60"
    assert aging_bucket(60) == "31-60"
    assert aging_bucket(61) == "61-90"
    assert aging_bucket(91) == "90+"


def test_run_end_to_end() -> None:
    customers = {
        "c1": cust(id="c1"),
        "c2": cust(id="c2", name="Marcus Reed", email="marcus@brightpath.co"),
    }
    invoices = [
        inv(id="a", number="INV-A", customer_id="c1"),
        inv(id="b", number="INV-B", customer_id="c2", status=InvoiceStatus.PAID),
    ]
    report = run(invoices, customers, AS_OF, COMPANY)
    assert len(report.actions) == 2
    assert len(report.drafts) == 1
    assert report.metrics()["drafts"] == 1
    assert report.recoverable_cents() == 120000
