"""SQLite connection and schema bootstrap (CLAUDE.md §4, §13).

No ORM — stdlib ``sqlite3`` with ``schema.sql`` as the single DDL source.
Supports an in-memory database (``":memory:"``) for fast, isolated tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str = ":memory:") -> sqlite3.Connection:
    """Open a connection with row access by name and FK enforcement on.

    For on-disk databases the parent directory is created if missing.
    Pass ``":memory:"`` (the default) for an ephemeral test database.
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def bootstrap(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes from ``schema.sql`` (idempotent)."""
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(ddl)
    conn.commit()


def init_db(db_path: str = ":memory:") -> sqlite3.Connection:
    """Open a connection and ensure the schema exists. Returns the connection."""
    conn = connect(db_path)
    bootstrap(conn)
    return conn
