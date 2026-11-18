"""SQLite access: connections and schema migrations."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def _migrations() -> list[tuple[int, str]]:
    """Numbered SQL files in ``deskscope/migrations``, in order."""
    folder = resources.files("deskscope").joinpath("migrations")
    found = []
    for entry in folder.iterdir():
        match = _MIGRATION_RE.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.read_text("utf-8")))
    found.sort()
    numbers = [n for n, _ in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(f"migrations must be numbered 1..n without gaps, found {numbers}")
    return found


SCHEMA_VERSION = len(_migrations())


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database with foreign keys on and rows readable by column name."""
    # FastAPI may run a request's dependency and handler in different worker
    # threads; each connection still belongs to a single request.
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply the missing migrations, each in its own transaction. Returns the version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{version} is newer than this DeskScope (v{SCHEMA_VERSION})"
        )
    pending = [(n, sql) for n, sql in _migrations() if n > version]
    if not pending:
        return version
    # Some migrations rebuild tables; SQLite requires foreign keys off (outside
    # a transaction) for that, and a foreign_key_check before turning them on.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for number, sql in pending:
            # executescript() commits first, so wrap the script and the version bump
            # in one explicit transaction: a failed migration leaves nothing half-done.
            try:
                conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.rollback()
                raise
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"migration {number} broke foreign keys: {problems[:5]}")
            version = number
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return version


def init_db(path: str | Path) -> None:
    """Create the database file (and its folder) if needed and migrate it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
    finally:
        conn.close()


def utcnow() -> str:
    """Current UTC time as an ISO 8601 string with seconds precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
