"""Command-line entry point for the Dunwell engine."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .agent import run
from .connectors import CONNECTOR_TYPES, ConnectorError, build_connector
from .core import Company
from .io import render_markdown, write_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dunwell",
        description="AI receivables follow-up agent (deterministic engine).",
    )
    parser.add_argument(
        "--source",
        default="csv",
        choices=sorted(CONNECTOR_TYPES),
        help="Ledger source to read (default: csv)",
    )
    parser.add_argument(
        "--ledger-dir",
        default="demo",
        help="Directory with customers.csv and invoices.csv (--source csv)",
    )
    parser.add_argument(
        "--fixtures",
        default=None,
        help="Read JSON fixtures instead of a live API (offline mode)",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO date to evaluate against (default: today)",
    )
    parser.add_argument(
        "--out", default="out", help="Output directory (default: out)"
    )
    parser.add_argument("--company", default="Northwind Studio")
    parser.add_argument("--reply-to", default="billing@northwind.example")
    parser.add_argument(
        "--payment-link", default="https://pay.northwind.example/inv"
    )
    args = parser.parse_args(argv)

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()

    connector = build_connector(
        args.source,
        ledger_dir=args.ledger_dir,
        fixtures=args.fixtures,
    )
    try:
        snapshot = connector.fetch()
    except ConnectorError as exc:
        print(f"dunwell: could not read source {connector.name!r}: {exc}", file=sys.stderr)
        return 2

    print(f"Source: {snapshot.source}")
    if snapshot.warnings:
        print(f"Warnings ({len(snapshot.warnings)}):")
        for warning in snapshot.warnings:
            print(f"  - {warning}")
    print()

    company = Company(
        name=args.company,
        reply_to=args.reply_to,
        signature=f"{args.company} - Billing",
        payment_link=args.payment_link,
    )

    report = run(snapshot.invoices, snapshot.customers, as_of, company)
    md_path = write_report(report, args.out)

    print(render_markdown(report))
    print(f"\nWrote {md_path}")
    print(f"Wrote {Path(args.out) / 'decisions.csv'}")
    return 0
