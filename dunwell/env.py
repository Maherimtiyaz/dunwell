"""Read configuration from a local ``.env`` file.

The service has always read its settings from the process environment, which
is correct for a deployment but surprising on a laptop: putting
``DUNWELL_MAIL_MODE=api`` in ``.env`` did nothing until it was exported in the
shell. That made a correctly written config look ignored.

This loader is deliberately small and stdlib-only (the project has no
dependencies), and it follows the rules most ``.env`` files rely on:

- ``KEY=value`` and ``export KEY=value``
- ``#`` comments, blank lines, and an unquoted `` #`` inline comment
- single quotes for a literal value, double quotes for escaped characters
- real environment variables win, so a host's settings are never overridden
  by a file that happened to be copied into the checkout

It is called from every entry point (``serve``, ``wsgi``, ``admin``,
``mailtest``, ``cli``) rather than at package import, so importing ``dunwell``
inside a test or another program does not silently mutate ``os.environ``.

Set ``DUNWELL_ENV_FILE`` to point at a specific file. Otherwise the working
directory and the repository root are searched, in that order.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# ``.env`` is the convention. ``dunwell.env`` is accepted because on Windows a
# file named ``.env`` is awkward to create in Explorer.
_FILENAMES = (".env", "dunwell.env")

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ``D:\project\dunwell\dunwell\env.py`` -> ``D:\project\dunwell``
_REPO_ROOT = Path(__file__).resolve().parent.parent

_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\\": "\\",
    '"': '"',
    "'": "'",
}


def _unescape_double(value: str) -> str:
    """Resolve the backslash escapes that make sense in a one-line value."""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                index += 2
                continue
            # An unknown escape is kept verbatim; silently eating the
            # backslash would corrupt Windows paths and API keys.
            out.append(char)
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _parse_value(raw: str) -> str:
    """Turn the text after ``=`` into the value a shell would have exported."""
    value = raw.strip()
    if value[:1] in {'"', "'"}:
        quote = value[0]
        # Only treat it as quoted when the closing quote is the last
        # character. A value like "abc" # note is common and should still
        # work, so check the trimmed tail before giving up.
        if len(value) >= 2 and value[-1] == quote:
            inner = value[1:-1]
            return _unescape_double(inner) if quote == '"' else inner
        closing = value.find(quote, 1)
        if closing != -1:
            tail = value[closing + 1 :].strip()
            if not tail or tail.startswith("#"):
                inner = value[1:closing]
                return _unescape_double(inner) if quote == '"' else inner
        # Unterminated quote: fall through and keep the text as written.
        return value

    # An unquoted inline comment starts at whitespace followed by '#'. A URL
    # fragment such as https://site/#pricing has no whitespace, so it stays.
    comment = re.search(r"\s+#", value)
    if comment:
        value = value[: comment.start()]
    return value.strip()


def _parse_line(line: str) -> tuple[str, str] | None | bool:
    """Parse one line.

    Returns ``(key, value)`` for an assignment, ``None`` for a blank line or
    comment, and ``False`` for a line that looks like an assignment but has an
    unusable key (so the caller can warn instead of silently ignoring it).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()

    key, separator, raw_value = stripped.partition("=")
    if not separator:
        return None
    key = key.strip()
    if not _KEY.match(key):
        return False
    return key, _parse_value(raw_value)


def _candidates(explicit: str | Path | None) -> list[Path]:
    """Files to try, best first."""
    if explicit is not None:
        return [Path(explicit)]

    override = (os.environ.get("DUNWELL_ENV_FILE") or "").strip()
    if override:
        return [Path(override)]

    found: list[Path] = []
    for root in (Path.cwd(), _REPO_ROOT):
        for name in _FILENAMES:
            candidate = root / name
            if candidate not in found:
                found.append(candidate)
    return found


def load_dotenv(
    path: str | Path | None = None,
    *,
    override: bool = False,
) -> Path | None:
    """Load the first existing ``.env`` file into ``os.environ``.

    Returns the file that was read, or ``None`` when there was nothing to
    load. ``override=False`` keeps real environment variables authoritative,
    which is what a deployment wants: a stray ``.env`` in the app directory
    must not replace the host's settings.
    """
    explicit = path is not None
    candidates = _candidates(path)

    for candidate in candidates:
        try:
            exists = candidate.is_file()
        except OSError:
            exists = False
        if not exists:
            continue

        try:
            text = candidate.read_text(encoding="utf-8-sig")
        except OSError as exc:
            print(
                f"dunwell: could not read {candidate}: {exc}",
                file=sys.stderr,
            )
            return None

        for number, line in enumerate(text.splitlines(), start=1):
            parsed = _parse_line(line)
            if parsed is None:
                continue
            if parsed is False:
                print(
                    f"dunwell: {candidate}:{number}: ignoring a line whose "
                    "name is not a valid environment variable",
                    file=sys.stderr,
                )
                continue
            key, value = parsed
            if override or key not in os.environ:
                os.environ[key] = value
        return candidate

    if explicit or os.environ.get("DUNWELL_ENV_FILE"):
        print(
            f"dunwell: configuration file {candidates[0]} does not exist",
            file=sys.stderr,
        )
    return None
