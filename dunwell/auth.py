"""Account and session handling for the Dunwell service.

Standard library only:

- passwords hashed with ``hashlib.scrypt`` (memory-hard, no dependency)
- session tokens are ``secrets.token_urlsafe``; only the SHA-256 digest is
  stored, so a database leak does not hand out live sessions

Verification is constant-time via ``hmac.compare_digest``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from .store import Store, StoreError

# scrypt parameters. n=2**14 keeps a single hash well under 100ms on a laptop
# while staying far above plain PBKDF2-at-default cost for interactive login.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32
_ENCODING_PREFIX = "scrypt"

SESSION_TTL_SECONDS = 60 * 60 * 24 * 14  # two weeks


class AuthError(RuntimeError):
    """Credential or session failure. Never leak which side was wrong."""


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return ``scrypt$n$r$p$salt$hash`` for a password."""
    if not isinstance(password, str) or len(password) < 8:
        raise AuthError("password must be at least 8 characters")
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=64 * 1024 * 1024,
        dklen=_KEY_BYTES,
    )
    return (
        f"{_ENCODING_PREFIX}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${salt.hex()}${derived.hex()}"
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time password check against a stored encoding."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = encoded.split("$")
    except (AttributeError, ValueError):
        return False
    if scheme != _ENCODING_PREFIX:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        n_val, r_val, p_val = int(n), int(r), int(p)
    except ValueError:
        return False

    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n_val,
            r=r_val,
            p=p_val,
            maxmem=64 * 1024 * 1024,
            dklen=len(expected) or _KEY_BYTES,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def validate_email(raw: str) -> str:
    """Minimal RFC-shaped check. Returns the normalized email or raises."""
    email = normalize_email(raw)
    if not email or len(email) > 254 or email.count("@") != 1:
        raise AuthError("invalid_email")
    local, _, domain = email.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise AuthError("invalid_email")
    if any(ch.isspace() for ch in email):
        raise AuthError("invalid_email")
    return email


@dataclass(frozen=True)
class Session:
    token: str
    account_id: int
    email: str


class AuthService:
    """Sign-up, sign-in, and session resolution over a :class:`Store`."""

    def __init__(self, store: Store, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self.store = store
        self.ttl_seconds = ttl_seconds

    def signup(self, email: str, password: str, **profile: str) -> Session:
        email = validate_email(email)
        password_hash = hash_password(password)
        account_id = self.store.create_account(email, password_hash, **profile)
        token = secrets.token_urlsafe(32)
        self.store.create_session(account_id, token, self.ttl_seconds)
        return Session(token=token, account_id=account_id, email=email)

    def login(self, email: str, password: str) -> Session:
        email = normalize_email(email)
        row = self.store.account_by_email(email)
        # Always run a hash so timing does not reveal whether the account
        # exists, and never say which side failed.
        if row is None:
            verify_password(password, hash_password("placeholder-password"))
            raise AuthError("invalid_credentials")
        if not verify_password(password, row["password_hash"]):
            raise AuthError("invalid_credentials")
        token = secrets.token_urlsafe(32)
        self.store.create_session(int(row["id"]), token, self.ttl_seconds)
        return Session(token=token, account_id=int(row["id"]), email=email)

    def logout(self, token: str | None) -> None:
        if token:
            self.store.delete_session(token)

    def resolve(self, token: str | None) -> Session | None:
        """Resolve a session token to a live session, or None."""
        if not token:
            return None
        account_id = self.store.session_account(token)
        if account_id is None:
            return None
        row = self.store.account_by_id(account_id)
        if row is None:
            return None
        return Session(token=token, account_id=account_id, email=row["email"])

    def profile(self, account_id: int) -> dict:
        row = self.store.account_by_id(account_id)
        if row is None:
            raise StoreError("account not found")
        return {
            "id": int(row["id"]),
            "email": row["email"],
            "company_name": row["company_name"],
            "reply_to": row["reply_to"],
            "signature": row["signature"],
            "payment_link": row["payment_link"],
            "created_at": row["created_at"],
        }
