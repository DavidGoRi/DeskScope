"""API tokens: random secrets shown once, stored only as SHA-256."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3

from . import users
from .db import utcnow
from .models import NotFound, PermissionDenied, User, ValidationError

PREFIX = "dsk_"
NAME_MAX = 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(conn: sqlite3.Connection, actor: User, owner_id: int, name: str) -> str:
    """Create a token for ``owner_id`` and return it. It cannot be read again later."""
    owner = users.get_user(conn, owner_id)
    if owner.id != actor.id and not actor.is_admin:
        raise PermissionDenied("error.forbidden")
    name = " ".join((name or "").split())
    if not name or len(name) > NAME_MAX:
        raise ValidationError("error.token_name_invalid", max=NAME_MAX)
    token = PREFIX + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO api_tokens (user_id, name, token_hash, prefix, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (owner.id, name, _hash(token), token[:12], utcnow()),
    )
    conn.commit()
    return token


def list_tokens(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, prefix, created_at, last_used_at, revoked_at FROM api_tokens"
        " WHERE user_id = ? ORDER BY revoked_at IS NOT NULL, created_at DESC",
        (user_id,),
    )
    return [dict(r) for r in rows]


def revoke_token(conn: sqlite3.Connection, actor: User, token_id: int) -> None:
    row = conn.execute("SELECT user_id FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
    if row is None or (row["user_id"] != actor.id and not actor.is_admin):
        raise NotFound("error.not_found")
    conn.execute(
        "UPDATE api_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
        (utcnow(), token_id),
    )
    conn.commit()


def authenticate_token(conn: sqlite3.Connection, token: str | None) -> User | None:
    """The active user behind a token, or None. Records when it was last used."""
    if not token or not token.startswith(PREFIX):
        return None
    row = conn.execute(
        "SELECT id, user_id FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL",
        (_hash(token),),
    ).fetchone()
    if row is None:
        return None
    user = users.get_user(conn, row["user_id"])
    if not user.active:
        return None
    conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (utcnow(), row["id"]))
    conn.commit()
    return user
