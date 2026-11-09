"""The .env loader: parsing, precedence, and the paths it searches."""

from __future__ import annotations

import os

import pytest

from dunwell.env import load_dotenv


@pytest.fixture()
def isolated_env(monkeypatch):
    """Replace os.environ with a plain dict that is restored afterwards.

    The loader writes straight into ``os.environ``, so ``monkeypatch.setenv``
    would not undo it at teardown. Swapping the mapping keeps every test
    hermetic.
    """
    environ = dict(os.environ)
    monkeypatch.setattr(os, "environ", environ)
    environ.pop("DUNWELL_ENV_FILE", None)
    environ.pop("DUNWELL_SKIP_DOTENV", None)
    return environ


@pytest.fixture()
def env_dir(tmp_path, monkeypatch, isolated_env):
    import dunwell.env as env_module

    monkeypatch.chdir(tmp_path)
    # Keep a real .env in the developer's checkout out of the test, so the
    # "no file" case is actually testing no file.
    monkeypatch.setattr(env_module, "_REPO_ROOT", tmp_path / "repo-root")
    return tmp_path


def write(path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_assignments_quotes_and_comments(env_dir, isolated_env) -> None:
    path = write(
        env_dir / ".env",
        """
# a comment
DUNWELL_MAIL_MODE=api

export DUNWELL_MAIL_API_KEY=re_abc123
DUNWELL_BRAND_NAME="Dunwell & Co"   # trailing note
DUNWELL_FOUNDER_NAME='Mahek'
DUNWELL_SITE_URL=https://dunwell.test/#pricing
DUNWELL_MAILING_ADDRESS="1 Road\\nCity"
""",
    )

    assert load_dotenv() == path
    assert isolated_env["DUNWELL_MAIL_MODE"] == "api"
    assert isolated_env["DUNWELL_MAIL_API_KEY"] == "re_abc123"
    assert isolated_env["DUNWELL_BRAND_NAME"] == "Dunwell & Co"
    assert isolated_env["DUNWELL_FOUNDER_NAME"] == "Mahek"
    # A URL fragment is not a comment: inline comments need whitespace first.
    assert isolated_env["DUNWELL_SITE_URL"] == "https://dunwell.test/#pricing"
    assert isolated_env["DUNWELL_MAILING_ADDRESS"] == "1 Road\nCity"


def test_real_environment_wins_unless_overridden(env_dir, isolated_env) -> None:
    isolated_env["DUNWELL_MAIL_MODE"] = "smtp"
    write(env_dir / ".env", "DUNWELL_MAIL_MODE=api\n")

    load_dotenv()
    assert isolated_env["DUNWELL_MAIL_MODE"] == "smtp"

    load_dotenv(override=True)
    assert isolated_env["DUNWELL_MAIL_MODE"] == "api"


def test_missing_file_is_not_an_error_when_implicit(env_dir, isolated_env) -> None:
    assert load_dotenv() is None


def test_explicit_env_file_wins_over_the_search(env_dir, isolated_env) -> None:
    write(env_dir / ".env", "DUNWELL_MAIL_MODE=smtp\n")
    selected = write(env_dir / "production.env", "DUNWELL_MAIL_MODE=api\n")
    isolated_env["DUNWELL_ENV_FILE"] = str(selected)

    assert load_dotenv() == selected
    assert isolated_env["DUNWELL_MAIL_MODE"] == "api"


def test_missing_explicit_file_is_reported(env_dir, isolated_env, capsys) -> None:
    isolated_env["DUNWELL_ENV_FILE"] = str(env_dir / "nope.env")

    assert load_dotenv() is None
    assert "does not exist" in capsys.readouterr().err


def test_a_line_with_an_invalid_name_is_skipped_and_reported(
    env_dir, isolated_env, capsys
) -> None:
    write(
        env_dir / ".env",
        "DUNWELL_MAIL_MODE=api\nnot a variable=oops\nDUNWELL_SITE_URL=https://x.test\n",
    )

    load_dotenv()

    assert isolated_env["DUNWELL_MAIL_MODE"] == "api"
    assert isolated_env["DUNWELL_SITE_URL"] == "https://x.test"
    assert "not a valid environment variable" in capsys.readouterr().err


def test_reads_a_utf16_file_saved_by_windows_powershell(
    env_dir, isolated_env
) -> None:
    """PowerShell 5's ``> .env`` writes UTF-16LE with a BOM."""
    (env_dir / ".env").write_bytes(
        "DUNWELL_MAIL_MODE=api\nDUNWELL_MAIL_API_KEY=re_utf16\n".encode("utf-16")
    )

    assert load_dotenv() is not None
    assert isolated_env["DUNWELL_MAIL_MODE"] == "api"
    assert isolated_env["DUNWELL_MAIL_API_KEY"] == "re_utf16"


def test_skip_flag_disables_the_automatic_search(env_dir, isolated_env) -> None:
    write(env_dir / ".env", "DUNWELL_MAIL_MODE=api\n")
    isolated_env["DUNWELL_SKIP_DOTENV"] = "1"

    assert load_dotenv() is None
    assert "DUNWELL_MAIL_MODE" not in isolated_env

    # An explicit path is an explicit request, so it still loads.
    assert load_dotenv(env_dir / ".env") == env_dir / ".env"
    assert isolated_env["DUNWELL_MAIL_MODE"] == "api"


def test_mailtest_reads_the_dot_env(env_dir, isolated_env) -> None:
    """The point of the loader: a config written to .env is actually used."""
    from dunwell import mailtest

    write(
        env_dir / ".env",
        "DUNWELL_FOUNDER_NAME=Asha\nDUNWELL_BRAND_NAME=Dunwell\n",
    )

    assert mailtest.main(["--preview", "out/welcome.html"]) == 0

    html = (env_dir / "out" / "welcome.html").read_text(encoding="utf-8")
    assert "Asha" in html
    assert isolated_env["DUNWELL_FOUNDER_NAME"] == "Asha"
