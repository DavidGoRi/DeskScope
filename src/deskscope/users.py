"""User accounts."""

from __future__ import annotations

import re
import sqlite3

from . import security
from .db import utcnow
from .models import ROLES, NotFound, PermissionDenied, User, ValidationError

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Hash checked when the username does not exist, so a failed login takes the
# same time whether or not the account exists (no username enumeration).
_DUMMY_HASH = security.hash_password("not-a-real-password", iterations=1000)


def _to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        full_name=row["full_name"],
        email=row["email"],
        role=row["role"],
        active=bool(row["active"]),
        is_system=bool(row["is_system"]),
    )


def create_user(
    conn: sqlite3.Connection,
    username: str,
    full_name: str,
    email: str,
    role: str,
    password: str,
) -> User:
    username, full_name, email = username.strip(), full_name.strip(), email.strip()
    if not USERNAME_RE.match(username):
        raise ValidationError("error.username_invalid")
    if not full_name or len(full_name) > 100:
        raise ValidationError("error.name_invalid")
    if not EMAIL_RE.match(email):
        raise ValidationError("error.email_invalid")
    if role not in ROLES:
        raise ValidationError("error.role_invalid")
    problem = security.password_problem(password)
    if problem:
        raise ValidationError(problem)
    if get_by_username(conn, username):
        raise ValidationError("error.username_taken")
    cur = conn.execute(
        "INSERT INTO users (username, full_name, email, role, password_hash, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (username, full_name, email, role, security.hash_password(password), utcnow()),
    )
    conn.commit()
    return get_user(conn, cur.lastrowid)


def get_user(conn: sqlite3.Connection, user_id: int) -> User:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise NotFound("error.user_not_found")
    return _to_user(row)


def get_by_username(conn: sqlite3.Connection, username: str) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    return _to_user(row) if row else None


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> User | None:
    """Return the active user for these credentials, or None."""
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if row is None:
        security.verify_password(password, _DUMMY_HASH)
        return None
    if row["is_system"]:
        security.verify_password(password, _DUMMY_HASH)
        return None
    if not security.verify_password(password, row["password_hash"]) or not row["active"]:
        return None
    return _to_user(row)


def list_users(conn: sqlite3.Connection, role: str | None = None) -> list[User]:
    """People with an account (system accounts are left out)."""
    if role:
        rows = conn.execute(
            "SELECT * FROM users WHERE role = ? AND is_system = 0 ORDER BY full_name", (role,)
        )
    else:
        rows = conn.execute("SELECT * FROM users WHERE is_system = 0 ORDER BY full_name")
    return [_to_user(r) for r in rows]


def system_user(conn: sqlite3.Connection) -> User:
    """The account integrations act as (created by migration 3)."""
    row = conn.execute("SELECT * FROM users WHERE username = 'integrations'").fetchone()
    if row is None or not row["is_system"]:
        raise NotFound("error.user_not_found")
    return _to_user(row)


def list_staff(conn: sqlite3.Connection) -> list[User]:
    """Active agents and admins: the people a ticket can be assigned to."""
    rows = conn.execute(
        "SELECT * FROM users WHERE role IN ('agent', 'admin') AND active = 1 AND is_system = 0"
        " ORDER BY full_name"
    )
    return [_to_user(r) for r in rows]


def set_active(conn: sqlite3.Connection, actor: User, user_id: int, active: bool) -> User:
    if not actor.is_admin:
        raise PermissionDenied("error.forbidden")
    target = get_user(conn, user_id)
    if target.is_system:
        raise PermissionDenied("error.forbidden")
    if target.id == actor.id and not active:
        raise ValidationError("error.cannot_disable_self")
    conn.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), target.id))
    conn.commit()
    return get_user(conn, target.id)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    problem = security.password_problem(password)
    if problem:
        raise ValidationError(problem)
    get_user(conn, user_id)
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (security.hash_password(password), user_id),
    )
    conn.commit()
