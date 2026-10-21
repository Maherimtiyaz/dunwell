"""Dunwell core domain: models, aging, policy and grounded drafting.

Pure standard library. Every model is immutable so a run is fully reproducible
from a snapshot of the ledger.

The central invariant mirrors a "cited-or-refused" contract: an outreach draft
is only ever produced from facts that exist on the invoice/customer record.
If a required fact is missing, the agent refuses to write and raises a hold
instead of inventing anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class InvoiceStatus(str, Enum):
    OPEN = "open"
    PAID = "paid"
    VOID = "void"
    DISPUTED = "disputed"


class Stage(str, Enum):
    NONE = "none"            # nothing to do
    COURTEOUS = "courteous"  # friendly first reminder
    FIRM = "firm"            # second reminder
    FINAL = "final"          # last automated reminder
    ESCALATE = "escalate"    # hand to a human, never auto-send


class Reason(str, Enum):
    READY = "ready"
    NOT_DUE = "not_due"
    ALREADY_PAID = "already_paid"
    VOID = "void"
    DISPUTED = "disputed"
    OPTED_OUT = "opted_out"
    NO_CONTACT = "no_contact"
    COOLDOWN = "cooldown"
    TOO_SMALL = "too_small"
    MAX_NUDGES = "max_nudges"
    OVER_ESCALATION_AGE = "over_escalation_age"


REASON_LABELS = {
    Reason.READY: "Ready to draft",
    Reason.NOT_DUE: "Not yet due",
    Reason.ALREADY_PAID: "Already paid",
    Reason.VOID: "Voided invoice",
    Reason.DISPUTED: "Disputed - human review",
    Reason.OPTED_OUT: "Customer opted out",
    Reason.NO_CONTACT: "No contact - human review",
    Reason.COOLDOWN: "In cooldown window",
    Reason.TOO_SMALL: "Below minimum amount",
    Reason.MAX_NUDGES: "Max nudges reached - human review",
    Reason.OVER_ESCALATION_AGE: "Too old for automation - human review",
}


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    email: str | None = None
    tier: str = "standard"          # "standard" | "strategic"
    opted_out: bool = False
    relationship_note: str = ""

    @property
    def first_name(self) -> str:
        return (self.name or "").strip().split(" ")[0] or "there"


@dataclass(frozen=True)
class Invoice:
    id: str
    number: str
    customer_id: str
    amount_cents: int
    currency: str
    issued_on: date
    due_on: date
    status: InvoiceStatus = InvoiceStatus.OPEN
    paid_on: date | None = None
    dispute_note: str = ""
    contacts: tuple[date, ...] = ()

    @property
    def amount(self) -> float:
        return self.amount_cents / 100.0

    @property
    def last_contact(self) -> date | None:
        return max(self.contacts) if self.contacts else None

    def days_overdue(self, as_of: date) -> int:
        """Positive when past due, zero on the due date, negative before."""
        return (as_of - self.due_on).days


@dataclass(frozen=True)
class Company:
    name: str
    reply_to: str
    signature: str
    payment_link: str = ""


# --------------------------------------------------------------------------- #
# Aging
# --------------------------------------------------------------------------- #


BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def aging_bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


def days_sales_outstanding(invoices: list[Invoice], as_of: date) -> float | None:
    """Rough DSO: average days outstanding across invoices still open."""
    open_inv = [i for i in invoices if i.status == InvoiceStatus.OPEN]
    if not open_inv:
        return None
    total = sum(max(0, i.days_overdue(as_of)) for i in open_inv)
    return round(total / len(open_inv), 1)


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Policy:
    min_amount_cents: int = 2000      # don't chase tiny amounts automatically
    cooldown_days: int = 7            # min gap between nudges
    final_after_days: int = 45        # force final tone past this age
    escalate_after_days: int = 75     # hand to a human past this age
    max_nudges: int = 3               # automated nudges before human handoff


@dataclass(frozen=True)
class Decision:
    invoice_id: str
    action: str                       # "draft" | "hold"
    stage: Stage
    reason: Reason
    notes: tuple[str, ...] = ()


def decide(
    invoice: Invoice,
    customer: Customer,
    as_of: date,
    policy: Policy | None = None,
) -> Decision:
    """Deterministic decision for one invoice. Ordered, fail-closed."""
    policy = policy or Policy()

    if invoice.status == InvoiceStatus.VOID:
        return Decision(invoice.id, "hold", Stage.NONE, Reason.VOID)
    if invoice.status == InvoiceStatus.PAID:
        return Decision(invoice.id, "hold", Stage.NONE, Reason.ALREADY_PAID)
    if invoice.status == InvoiceStatus.DISPUTED:
        notes = (invoice.dispute_note,) if invoice.dispute_note else ()
        return Decision(invoice.id, "hold", Stage.ESCALATE, Reason.DISPUTED, notes)
    if customer.opted_out:
        return Decision(invoice.id, "hold", Stage.NONE, Reason.OPTED_OUT)
    if not customer.email:
        return Decision(invoice.id, "hold", Stage.ESCALATE, Reason.NO_CONTACT)

    overdue = invoice.days_overdue(as_of)
    if overdue < 1:
        return Decision(invoice.id, "hold", Stage.NONE, Reason.NOT_DUE)
    if invoice.amount_cents < policy.min_amount_cents:
        return Decision(invoice.id, "hold", Stage.NONE, Reason.TOO_SMALL)
    if len(invoice.contacts) >= policy.max_nudges:
        return Decision(invoice.id, "hold", Stage.ESCALATE, Reason.MAX_NUDGES)
    if overdue >= policy.escalate_after_days:
        return Decision(
            invoice.id, "hold", Stage.ESCALATE, Reason.OVER_ESCALATION_AGE
        )

    last = invoice.last_contact
    if last is not None and (as_of - last).days < policy.cooldown_days:
        return Decision(invoice.id, "hold", Stage.NONE, Reason.COOLDOWN)

    nudge_index = len(invoice.contacts)
    if overdue >= policy.final_after_days:
        stage = Stage.FINAL
    elif nudge_index == 0:
        stage = Stage.COURTEOUS
    elif nudge_index == 1:
        stage = Stage.FIRM
    else:
        stage = Stage.FINAL

    notes: tuple[str, ...] = ()
    if customer.tier == "strategic":
        notes = ("strategic account: keep tone warm",)
    return Decision(invoice.id, "draft", stage, Reason.READY, notes)


# --------------------------------------------------------------------------- #
# Grounded drafting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Draft:
    invoice_id: str
    to: str
    subject: str
    body: str
    facts: tuple[str, ...]
    stage: Stage


def _money(cents: int, currency: str) -> str:
    return f"{currency} {cents / 100:,.2f}"


_STAGE_INTRO = {
    Stage.COURTEOUS: "a quick, friendly reminder",
    Stage.FIRM: "a second reminder",
    Stage.FINAL: "a final reminder",
}


def draft(
    decision: Decision,
    invoice: Invoice,
    customer: Customer,
    company: Company,
    as_of: date,
) -> Draft | None:
    """Render an outreach email strictly from facts on the record.

    Returns None when there is nothing to draft (hold) or when a required
    fact is absent (refuse rather than invent).
    """
    if decision.action != "draft":
        return None
    if not customer.email:
        return None

    amount = _money(invoice.amount_cents, invoice.currency)
    due = invoice.due_on.isoformat()
    days = invoice.days_overdue(as_of)

    facts = (
        f"invoice number {invoice.number}",
        f"amount {amount}",
        f"due date {due}",
        f"{days} day(s) overdue as of {as_of.isoformat()}",
    )

    subject = f"Reminder: invoice {invoice.number} ({amount}) due {due}"

    body_lines = [
        f"Hi {customer.first_name},",
        "",
        f"Hope you're well. This is {_STAGE_INTRO[decision.stage]} that invoice "
        f"{invoice.number} for {amount} was due on {due} ({days} day(s) ago).",
        "",
    ]

    if decision.stage == Stage.COURTEOUS:
        body_lines.append(
            "If payment is already scheduled, please ignore this note."
        )
    elif decision.stage == Stage.FIRM:
        body_lines.append(
            "Could you let us know a date when we can expect this to be settled?"
        )
    else:  # FINAL
        body_lines.append(
            "We may need to pause work on the account until this balance clears. "
            "Please reply with a payment date, or let us know if something is wrong."
        )

    if company.payment_link:
        body_lines += ["", f"Pay here: {company.payment_link}"]

    body_lines += ["", "Thanks,", company.signature]

    return Draft(
        invoice_id=invoice.id,
        to=customer.email,
        subject=subject,
        body="\n".join(body_lines),
        facts=facts,
        stage=decision.stage,
    )
