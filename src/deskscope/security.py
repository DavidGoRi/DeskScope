"""Password hashing (PBKDF2-SHA256, standard library only) and small helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# OWASP 2023 recommendation for PBKDF2-HMAC-SHA256. The value is stored in each
# hash, so it can be raised later without breaking existing passwords.
ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 10

_ALGORITHM = "pbkdf2_sha256"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str, iterations: int | None = None) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt>$<hash>``."""
    rounds = iterations or ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"{_ALGORITHM}${rounds}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash in constant time."""
    try:
        algorithm, rounds, salt, digest = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.b64decode(salt), int(rounds)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_problem(password: str) -> str | None:
    """Return an i18n key describing why a password is rejected, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return "error.password_short"
    if password.strip() != password or not password.strip():
        return "error.password_spaces"
    return None


def new_token() -> str:
    """Random URL-safe token for CSRF protection and secret keys."""
    return secrets.token_urlsafe(32)


def tokens_match(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))
