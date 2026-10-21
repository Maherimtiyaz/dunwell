"""CLI tests: source selection, offline fixtures, and clean failure paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from dunwell.cli import main

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "demo" / "fixtures"
DEMO = ROOT / "demo"


def test_default_source_is_the_csv_ledger(tmp_path, capsys) -> None:
    code = main(
        [
            "--ledger-dir",
            str(DEMO),
            "--as-of",
            "2026-10-16",
            "--out",
            str(tmp_path),
        ]
    )
    assert code == 0
    assert "Source: csv" in capsys.readouterr().out


def test_runs_a_fixture_source_and_surfaces_warnings(tmp_path, capsys) -> None:
    code = main(
        [
            "--source",
            "stripe",
            "--fixtures",
            str(FIXTURES / "stripe"),
            "--as-of",
            "2026-10-16",
            "--out",
            str(tmp_path),
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "Source: stripe" in printed
    assert "STR-2006" in printed  # the missing-due-date warning is shown
    assert (tmp_path / "dunwell_report.md").is_file()
    assert (tmp_path / "decisions.csv").is_file()


def test_all_sources_run_through_the_cli(tmp_path) -> None:
    for source, fixtures in (
        ("csv", None),
        ("stripe", FIXTURES / "stripe"),
        ("quickbooks", FIXTURES / "quickbooks"),
        ("xero", FIXTURES / "xero"),
    ):
        args = [
            "--source",
            source,
            "--as-of",
            "2026-10-16",
            "--out",
            str(tmp_path / source),
        ]
        if fixtures is not None:
            args += ["--fixtures", str(fixtures)]
        else:
            args += ["--ledger-dir", str(DEMO)]
        assert main(args) == 0, source


def test_reports_connector_errors_cleanly(capsys) -> None:
    """Missing credentials exit 2 with a message, not a traceback."""
    assert main(["--source", "stripe", "--as-of", "2026-10-16"]) == 2
    captured = capsys.readouterr()
    assert "STRIPE_API_KEY" in captured.err
    assert "Traceback" not in captured.err


def test_unknown_source_is_rejected(capsys) -> None:
    """argparse rejects an unknown --source with exit 2 before any I/O."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--source", "oracle"])
    assert excinfo.value.code == 2
