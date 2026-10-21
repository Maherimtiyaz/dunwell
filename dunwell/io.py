"""Ledger IO: load CSV ledgers and write run artifacts."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from .core import (
    REASON_LABELS,
    Customer,
    Invoice,
    InvoiceStatus,
)
from .agent import RunReport


def _date_or_none(value: str) -> date | None:
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def parse_customers_csv(text: str) -> dict[str, Customer]:
    """Parse a customers CSV body. Shared by the file loader and the API."""
    customers: dict[str, Customer] = {}
    for row in csv.DictReader(text.splitlines()):
        if not (row.get("id") or "").strip():
            continue
        c = Customer(
            id=row["id"].strip(),
            name=row["name"].strip(),
            email=(row.get("email") or "").strip() or None,
            tier=(row.get("tier") or "standard").strip() or "standard",
            opted_out=(row.get("opted_out") or "").strip().lower()
            in {"1", "true", "yes", "y"},
            relationship_note=(row.get("relationship_note") or "").strip(),
        )
        customers[c.id] = c
    return customers


def parse_invoices_csv(text: str) -> list[Invoice]:
    """Parse an invoices CSV body. Shared by the file loader and the API."""
    invoices: list[Invoice] = []
    for row in csv.DictReader(text.splitlines()):
        if not (row.get("id") or "").strip():
            continue
        contacts = tuple(
            date.fromisoformat(part.strip())
            for part in (row.get("contacts") or "").split(";")
            if part.strip()
        )
        invoices.append(
            Invoice(
                id=row["id"].strip(),
                number=row["number"].strip(),
                customer_id=row["customer_id"].strip(),
                amount_cents=int(row["amount_cents"]),
                currency=(row.get("currency") or "USD").strip() or "USD",
                issued_on=date.fromisoformat(row["issued_on"].strip()),
                due_on=date.fromisoformat(row["due_on"].strip()),
                status=InvoiceStatus((row.get("status") or "open").strip()),
                paid_on=_date_or_none(row.get("paid_on") or ""),
                dispute_note=(row.get("dispute_note") or "").strip(),
                contacts=contacts,
            )
        )
    return invoices


def load_customers(path: str | Path) -> dict[str, Customer]:
    return parse_customers_csv(Path(path).read_text(encoding="utf-8"))


def load_invoices(path: str | Path) -> list[Invoice]:
    return parse_invoices_csv(Path(path).read_text(encoding="utf-8"))


def render_markdown(report: RunReport) -> str:
    m = report.metrics()
    lines = [
        "# Dunwell recovery report",
        "",
        f"- As of: **{m['as_of']}**",
        f"- Invoices evaluated: **{m['invoices']}**",
        f"- Drafts ready to send: **{m['drafts']}**",
        f"- Held for human review: **{m['holds']}**",
        f"- Cash at risk (open + disputed): **{m['at_risk_cents'] / 100:,.2f}**",
        f"- Immediately recoverable (drafts): **{m['recoverable_cents'] / 100:,.2f}**",
        f"- Escalated to human: **{m['escalate_cents'] / 100:,.2f}**",
        f"- Days sales outstanding (open): **{m['dso']}**",
        "",
        "## Aging (open invoices)",
        "",
    ]
    for bucket, cents in report.aging().items():
        lines.append(f"- {bucket}: {cents / 100:,.2f}")
    if not report.aging():
        lines.append("- (none)")

    lines += ["", "## Decisions", ""]
    for a in report.actions:
        lines.append(
            f"- `{a.invoice.number}` {a.customer.name} "
            f"({a.invoice.amount_cents / 100:,.2f}) -> "
            f"**{a.decision.action}/{a.decision.stage.value}** "
            f"[{REASON_LABELS[a.decision.reason]}]"
        )
        for note in a.decision.notes:
            lines.append(f"  - {note}")

    lines += ["", "## Drafts", ""]
    for a in report.drafts:
        d = a.draft
        assert d is not None
        lines += [
            f"### {a.invoice.number} -> {d.to}",
            "",
            f"**Subject:** {d.subject}",
            "",
            "```",
            d.body,
            "```",
            "",
            "Grounded in: " + "; ".join(d.facts),
            "",
        ]
    if not report.drafts:
        lines.append("(none)")

    return "\n".join(lines)


def write_report(report: RunReport, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    md_path = out / "dunwell_report.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")

    csv_path = out / "decisions.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "invoice_id",
                "invoice_number",
                "customer",
                "amount_cents",
                "currency",
                "days_overdue",
                "action",
                "stage",
                "reason",
                "to",
            ]
        )
        for a in report.actions:
            writer.writerow(
                [
                    a.invoice.id,
                    a.invoice.number,
                    a.customer.name,
                    a.invoice.amount_cents,
                    a.invoice.currency,
                    a.invoice.days_overdue(report.as_of),
                    a.decision.action,
                    a.decision.stage.value,
                    a.decision.reason.value,
                    a.draft.to if a.draft else "",
                ]
            )
    return md_path
