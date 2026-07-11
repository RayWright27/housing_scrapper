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
    # check_same_thread=False: the web server runs sync endpoints in a threadpool,
    # so a connection may be used from a worker thread. This is a single-user
    # loopback tool; connections are not shared concurrently across threads.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Small pre-bootstrap migrations for shape changes ``CREATE IF NOT EXISTS``
    cannot make on an existing table.

    ``pending_notifications`` gained a ``chat_id`` column (one row per recipient).
    A pre-existing table without it holds only transient, undelivered notices, so
    we drop it and let :func:`bootstrap` recreate it in the current shape — no
    durable history is lost (the outbox is not history)."""
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'pending_notifications'"
    ).fetchone()
    if row is None:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_notifications)")}
    if "chat_id" not in cols:
        conn.execute("DROP TABLE pending_notifications")


def bootstrap(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes from ``schema.sql`` (idempotent)."""
    _migrate(conn)
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(ddl)
    conn.commit()


def init_db(db_path: str = ":memory:") -> sqlite3.Connection:
    """Open a connection and ensure the schema exists. Returns the connection."""
    conn = connect(db_path)
    bootstrap(conn)
    return conn
