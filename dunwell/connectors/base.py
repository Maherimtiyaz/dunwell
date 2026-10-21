"""Connector interfaces and transports.

A *connector* reads a ledger from somewhere (CSV, Stripe, QuickBooks, Xero)
and normalizes it into the domain models in :mod:`dunwell.core`. Everything
below is deliberately small and dependency-free so the same code runs against
a live API or against local JSON fixtures in tests.

Design rules
------------
* Read mapping is real; write-back is declared via ``capabilities()`` so callers
  can hold a connector to what it actually supports.
* Nothing invents data. When a source omits a field the engine needs (usually
  the due date), the connector falls back to a *documented* value and records a
  human-readable warning on the snapshot.
* HTTP goes through a ``Transport`` so tests never touch the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..core import Customer, Invoice


class ConnectorError(RuntimeError):
    """A source could not be read (bad credentials, missing fixture, HTTP error)."""


class UnsupportedOperation(ConnectorError):
    """The connector does not implement this operation (see ``capabilities``)."""


# Capability flags. Callers should branch on these rather than on connector name.
READ = "read"
INVOICES = "invoices"
CUSTOMERS = "customers"
WRITE_BACK = "write_back"


@dataclass
class LedgerSnapshot:
    """A normalized read of one ledger, ready to hand to the engine."""

    invoices: list[Invoice]
    customers: dict[str, Customer]
    source: str = ""
    warnings: tuple[str, ...] = ()
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@runtime_checkable
class Transport(Protocol):
    """Minimal JSON-over-GET client. Swapped for a fixture in tests."""

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any: ...


@runtime_checkable
class LedgerConnector(Protocol):
    """Read side of a ledger source."""

    name: str

    def capabilities(self) -> frozenset[str]: ...

    def fetch(self) -> LedgerSnapshot: ...


class UrllibTransport:
    """Standard-library HTTP client (no ``requests`` dependency)."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.calls: list[str] = []

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        full = f"{url}?{urlencode(params)}" if params else url
        self.calls.append(full)
        request = Request(full, headers=headers or {}, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed error
            raise ConnectorError(f"GET {full} failed: {exc}") from exc


class FixtureTransport:
    """Offline transport backed by a directory of JSON files.

    Resolution order for a request:

    1. an explicit ``routes`` substring match against the URL *and* the params
       (so QuickBooks' SQL queries can pick different files);
    2. a file named after the trailing path segments, joined by ``_``;
    3. a file named after the last path segment;
    4. a file named after the first path segment.
    """

    def __init__(
        self,
        root: str | Path,
        routes: dict[str, str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.routes = routes or {}
        self.calls: list[str] = []

    def _haystack(self, url: str, params: dict[str, Any] | None) -> str:
        return f"{url} {json.dumps(params or {}, sort_keys=True)}"

    def _resolve(self, haystack: str, path: str) -> Path | None:
        for key, filename in self.routes.items():
            if key in haystack:
                return self.root / filename
        parts = [p for p in path.split("/") if p]
        names: list[str] = []
        if len(parts) >= 2:
            names.append("_".join(parts[-2:]))
        if parts:
            names.append(parts[-1])
            names.append(parts[0])
        for name in names:
            candidate = self.root / f"{name}.json"
            if candidate.exists():
                return candidate
        return None

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        display = f"{url}?{urlencode(params)}" if params else url
        self.calls.append(display)
        path = url.split("?", 1)[0]
        target = self._resolve(self._haystack(url, params), path)
        if target is None or not target.exists():
            raise ConnectorError(
                f"no fixture for {display} under {self.root}"
            )
        return json.loads(target.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Shared mapping helpers
# --------------------------------------------------------------------------- #


def unix_to_date(value: Any) -> date | None:
    """Convert a Unix timestamp (seconds) to a date, tolerating None/0."""
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


def iso_to_date(value: Any) -> date | None:
    """Parse an ISO date or datetime string to a date."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def to_cents(value: Any) -> int:
    """Convert a decimal amount (float/str/int) to integer cents."""
    if value in (None, ""):
        return 0
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def minor_to_cents(value: Any) -> int:
    """Normalize a value that is *already* in minor units (cents) to int."""
    if value in (None, ""):
        return 0
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0
