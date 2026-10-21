"""Run loop and aggregated recovery reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .core import (
    Company,
    Customer,
    Decision,
    Draft,
    Invoice,
    InvoiceStatus,
    Policy,
    Reason,
    Stage,
    aging_bucket,
    days_sales_outstanding,
    decide,
    draft,
)


@dataclass
class Action:
    invoice: Invoice
    customer: Customer
    decision: Decision
    draft: Draft | None


@dataclass
class RunReport:
    as_of: date
    company: Company
    actions: list[Action]

    @property
    def drafts(self) -> list[Action]:
        return [a for a in self.actions if a.draft is not None]

    @property
    def holds(self) -> list[Action]:
        return [a for a in self.actions if a.draft is None]

    def by_reason(self) -> dict[Reason, list[Action]]:
        out: dict[Reason, list[Action]] = {}
        for a in self.actions:
            out.setdefault(a.decision.reason, []).append(a)
        return out

    def at_risk_cents(self) -> int:
        live = (InvoiceStatus.OPEN, InvoiceStatus.DISPUTED)
        return sum(a.invoice.amount_cents for a in self.actions
                   if a.invoice.status in live)

    def recoverable_cents(self) -> int:
        """Cash we can act on right now (drafts ready to send)."""
        return sum(a.invoice.amount_cents for a in self.drafts)

    def escalate_cents(self) -> int:
        return sum(a.invoice.amount_cents for a in self.actions
                   if a.decision.stage == Stage.ESCALATE)

    def aging(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            if a.invoice.status != InvoiceStatus.OPEN:
                continue
            bucket = aging_bucket(a.invoice.days_overdue(self.as_of))
            out[bucket] = out.get(bucket, 0) + a.invoice.amount_cents
        return dict(sorted(out.items()))

    def metrics(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "invoices": len(self.actions),
            "drafts": len(self.drafts),
            "holds": len(self.holds),
            "at_risk_cents": self.at_risk_cents(),
            "recoverable_cents": self.recoverable_cents(),
            "escalate_cents": self.escalate_cents(),
            "dso": days_sales_outstanding(
                [a.invoice for a in self.actions], self.as_of
            ),
        }


def run(
    invoices: list[Invoice],
    customers: dict[str, Customer],
    as_of: date,
    company: Company,
    policy: Policy | None = None,
) -> RunReport:
    """Evaluate every invoice, decide, and draft only where grounded."""
    policy = policy or Policy()
    actions: list[Action] = []
    for inv in invoices:
        customer = customers[inv.customer_id]
        decision = decide(inv, customer, as_of, policy)
        dr = draft(decision, inv, customer, company, as_of)
        actions.append(Action(inv, customer, decision, dr))
    return RunReport(as_of=as_of, company=company, actions=actions)
