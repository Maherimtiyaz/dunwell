"""Ledger connectors.

Every source normalizes into :class:`~dunwell.connectors.base.LedgerSnapshot`,
so the engine never knows or cares where the invoices came from.

    from dunwell.connectors import build_connector

    snapshot = build_connector("stripe", fixtures="data/stripe").fetch()
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from .base import (
    CUSTOMERS,
    INVOICES,
    READ,
    WRITE_BACK,
    ConnectorError,
    FixtureTransport,
    LedgerConnector,
    LedgerSnapshot,
    Transport,
    UnsupportedOperation,
    UrllibTransport,
)
from .csv_source import CsvConnector
from .quickbooks import QuickBooksConnector
from .stripe import StripeConnector
from .xero import XeroConnector

CONNECTOR_TYPES: dict[str, type] = {
    "csv": CsvConnector,
    "stripe": StripeConnector,
    "quickbooks": QuickBooksConnector,
    "xero": XeroConnector,
}

# Which fixture file answers which call, per source. Only needed where several
# calls share one URL path (QuickBooks' /query) or where a plain filename is
# ambiguous.
FIXTURE_ROUTES: dict[str, dict[str, str]] = {
    "stripe": {"customers": "customers.json", "invoices": "invoices.json"},
    "quickbooks": {"Customer": "customers.json", "Invoice": "invoices.json"},
    "xero": {"Invoices": "invoices.json"},
}


def build_connector(
    source: str = "csv",
    *,
    ledger_dir: str | Path = "demo",
    fixtures: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    transport: Transport | None = None,
) -> LedgerConnector:
    """Construct a connector.

    ``fixtures`` forces offline mode (no network, no credentials) which is what
    tests and demos use. Otherwise credentials are read from ``env``.
    """
    name = (source or "csv").strip().lower()
    if name not in CONNECTOR_TYPES:
        known = ", ".join(sorted(CONNECTOR_TYPES))
        raise ConnectorError(f"unknown ledger source {source!r} (known: {known})")

    if name == "csv":
        return CsvConnector(ledger_dir)

    offline = fixtures is not None
    if transport is None and offline:
        transport = FixtureTransport(fixtures, routes=FIXTURE_ROUTES.get(name, {}))
    transport = transport or UrllibTransport()

    environ: Mapping[str, str] = os.environ if env is None else env

    if name == "stripe":
        return StripeConnector(
            environ.get("STRIPE_API_KEY") or None,
            transport=transport,
            offline=offline,
        )

    if name == "quickbooks":
        return QuickBooksConnector(
            environ.get("QBO_ACCESS_TOKEN") or None,
            environ.get("QBO_REALM_ID") or "offline",
            transport=transport,
            sandbox=str(environ.get("QBO_SANDBOX", "")).lower() in {"1", "true", "yes"},
            offline=offline,
        )

    return XeroConnector(
        environ.get("XERO_ACCESS_TOKEN") or None,
        environ.get("XERO_TENANT_ID") or "offline",
        transport=transport,
        offline=offline,
    )


__all__ = [
    "CUSTOMERS",
    "INVOICES",
    "READ",
    "WRITE_BACK",
    "CONNECTOR_TYPES",
    "FIXTURE_ROUTES",
    "ConnectorError",
    "CsvConnector",
    "FixtureTransport",
    "LedgerConnector",
    "LedgerSnapshot",
    "QuickBooksConnector",
    "StripeConnector",
    "Transport",
    "UnsupportedOperation",
    "UrllibTransport",
    "XeroConnector",
    "build_connector",
]
