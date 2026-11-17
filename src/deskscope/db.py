"""SQLite access: connections and schema migrations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database with foreign keys on and rows readable by column name."""
    # FastAPI may run a request's dependency and handler in different worker
    # threads; each connection still belongs to a single request.
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the schema up to date. Returns the version the database ended at."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{version} is newer than this DeskScope (v{SCHEMA_VERSION})"
        )
    if version < 1:
        sql = resources.files("deskscope").joinpath("schema.sql").read_text("utf-8")
        conn.executescript(sql)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        version = 1
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
