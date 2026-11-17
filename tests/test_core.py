"""Security helpers, database migrations and user accounts."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import PASSWORD

from deskscope import security, users
from deskscope.db import SCHEMA_VERSION, connect, init_db, migrate
from deskscope.models import NotFound, PermissionDenied, ValidationError

# ---- security ---------------------------------------------------------------


def test_hash_roundtrip_and_format():
    stored = security.hash_password("s3cret-password")
    algorithm, rounds, _salt, _digest = stored.split("$")
    assert algorithm == "pbkdf2_sha256"
    assert int(rounds) == security.ITERATIONS
    assert security.verify_password("s3cret-password", stored)
    assert not security.verify_password("s3cret-passwor", stored)


def test_same_password_gets_different_salts():
    assert security.hash_password("same-password!") != security.hash_password("same-password!")


def test_hash_keeps_its_own_iteration_count(monkeypatch):
    stored = security.hash_password("old-password-1", iterations=1500)
    monkeypatch.setattr(security, "ITERATIONS", 2000)
    assert security.verify_password("old-password-1", stored)


@pytest.mark.parametrize("stored", ["", "garbage", "md5$1$a$b", "pbkdf2_sha256$x$y$z", "a$b$c"])
def test_malformed_hashes_never_verify(stored):
    assert not security.verify_password("anything", stored)


@pytest.mark.parametrize(
    ("password", "problem"),
    [("short", "error.password_short"), (" leading-space", "error.password_spaces"),
     ("long-enough-pw", None)],
)
def test_password_policy(password, problem):
    assert security.password_problem(password) == problem


def test_token_comparison():
    token = security.new_token()
    assert len(token) >= 40
    assert security.tokens_match(token, token)
    assert not security.tokens_match(token, token + "x")
    assert not security.tokens_match(None, None)
    assert not security.tokens_match("", "")


# ---- database ---------------------------------------------------------------


def test_migrate_is_idempotent(db_path):
    conn = connect(db_path)
    try:
        assert migrate(conn) == SCHEMA_VERSION
        assert migrate(conn) == SCHEMA_VERSION
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"users", "tickets", "comments", "ticket_events"} <= tables
    finally:
        conn.close()


def test_refuses_a_newer_schema(db_path):
    conn = connect(db_path)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        with pytest.raises(RuntimeError, match="newer"):
            migrate(conn)
    finally:
        conn.close()


def test_init_db_creates_missing_folders(tmp_path):
    path = tmp_path / "a" / "b" / "desk.db"
    init_db(path)
    assert path.exists()


def test_foreign_keys_and_checks_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tickets (title, description, category, priority, status, requester_id,"
            " created_at, updated_at) VALUES ('t', 'd', 'other', 'low', 'new', 999, 'x', 'x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO users (username, full_name, email, role, password_hash, created_at)"
            " VALUES ('x', 'X', 'x@example.com', 'superuser', 'h', 'x')"
        )


# ---- users ------------------------------------------------------------------


def test_create_and_read_user(conn):
    user = users.create_user(conn, " ana.garcia ", "Ana García", "ana@example.com", "agent", PASSWORD)
    assert user.username == "ana.garcia"
    assert user.is_staff and not user.is_admin and user.active
    assert users.get_by_username(conn, "ANA.GARCIA").id == user.id
    assert users.get_user(conn, user.id) == user


def test_password_is_not_stored_in_clear(conn):
    users.create_user(conn, "ana", "Ana", "ana@example.com", "requester", PASSWORD)
    stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    assert PASSWORD not in stored and stored.startswith("pbkdf2_sha256$")


@pytest.mark.parametrize(
    ("username", "name", "email", "role", "password", "key"),
    [
        ("a", "Ana", "a@example.com", "requester", PASSWORD, "error.username_invalid"),
        ("ana garcia", "Ana", "a@example.com", "requester", PASSWORD, "error.username_invalid"),
        ("ana", "", "a@example.com", "requester", PASSWORD, "error.name_invalid"),
        ("ana", "Ana", "not-an-email", "requester", PASSWORD, "error.email_invalid"),
        ("ana", "Ana", "a@example.com", "root", PASSWORD, "error.role_invalid"),
        ("ana", "Ana", "a@example.com", "requester", "short", "error.password_short"),
    ],
)
def test_user_validation(conn, username, name, email, role, password, key):
    with pytest.raises(ValidationError) as exc:
        users.create_user(conn, username, name, email, role, password)
    assert exc.value.key == key


def test_usernames_are_unique_ignoring_case(conn):
    users.create_user(conn, "ana", "Ana", "a@example.com", "requester", PASSWORD)
    with pytest.raises(ValidationError) as exc:
        users.create_user(conn, "ANA", "Ana 2", "a2@example.com", "requester", PASSWORD)
    assert exc.value.key == "error.username_taken"


def test_authenticate(conn, people):
    assert users.authenticate(conn, "ana", PASSWORD).id == people["req"].id
    assert users.authenticate(conn, " Ana ", PASSWORD).id == people["req"].id
    assert users.authenticate(conn, "ana", "wrong-password") is None
    assert users.authenticate(conn, "nobody", PASSWORD) is None


def test_disabled_users_cannot_sign_in(conn, people):
    users.set_active(conn, people["admin"], people["req"].id, False)
    assert users.authenticate(conn, "ana", PASSWORD) is None
    users.set_active(conn, people["admin"], people["req"].id, True)
    assert users.authenticate(conn, "ana", PASSWORD) is not None


def test_only_admins_disable_accounts_and_never_their_own(conn, people):
    with pytest.raises(PermissionDenied):
        users.set_active(conn, people["agent"], people["req"].id, False)
    with pytest.raises(ValidationError) as exc:
        users.set_active(conn, people["admin"], people["admin"].id, False)
    assert exc.value.key == "error.cannot_disable_self"
    with pytest.raises(NotFound):
        users.set_active(conn, people["admin"], 999, False)


def test_staff_list_has_only_active_technicians(conn, people):
    users.set_active(conn, people["admin"], people["agent2"].id, False)
    names = [u.username for u in users.list_staff(conn)]
    assert names == ["admin", "tech1"]


def test_set_password(conn, people):
    users.set_password(conn, people["req"].id, "brand-new-password")
    assert users.authenticate(conn, "ana", "brand-new-password")
    assert users.authenticate(conn, "ana", PASSWORD) is None
    with pytest.raises(ValidationError):
        users.set_password(conn, people["req"].id, "short")
