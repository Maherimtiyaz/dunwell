"""CSV ledger connector — the offline entry point and the shape every other
connector normalizes into.
"""

from __future__ import annotations

from pathlib import Path

from ..io import load_customers, load_invoices
from .base import CUSTOMERS, INVOICES, READ, LedgerSnapshot


class CsvConnector:
    """Read a ledger from ``customers.csv`` + ``invoices.csv`` in a directory."""

    name = "csv"

    def __init__(self, directory: str | Path = "demo") -> None:
        self.directory = Path(directory)

    def capabilities(self) -> frozenset[str]:
        return frozenset({READ, INVOICES, CUSTOMERS})

    def fetch(self) -> LedgerSnapshot:
        customers = load_customers(self.directory / "customers.csv")
        invoices = load_invoices(self.directory / "invoices.csv")
        return LedgerSnapshot(
            invoices=invoices,
            customers=customers,
            source=self.name,
        )
