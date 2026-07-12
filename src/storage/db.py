"""SQLite connection and schema bootstrap (CLAUDE.md §4, §13).

No ORM — stdlib ``sqlite3`` with ``schema.sql`` as the single DDL source.
Supports an in-memory database (``":memory:"``) for fast, isolated tests.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("realty.storage")

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
    if db_path != ":memory:":
        # A long refresh writes while the dashboard reads concurrently. WAL lets a
        # reader and a writer coexist, and busy_timeout makes the loser wait
        # briefly instead of raising "database is locked" outright.
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
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


def backup_db(db_path: str, keep: int = 10) -> Path | None:
    """Write a timestamped snapshot of ``db_path`` into ``<dir>/backups`` and
    prune to the most recent ``keep``. Returns the snapshot path (or ``None``).

    The price history is the system's irreplaceable asset (§2); this guards
    against accidental deletion / corruption. Uses SQLite's online backup API so
    the copy is consistent even with WAL and a live writer. Best-effort: a backup
    failure is logged, never fatal."""
    if db_path == ":memory:" or not Path(db_path).exists():
        return None
    src_path = Path(db_path)
    backups = src_path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    # Microseconds keep the name unique even for backups made in the same second.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    dest = backups / f"{src_path.stem}-{ts}{src_path.suffix}"
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
    except sqlite3.Error:
        logger.warning("db backup failed", exc_info=True)
        return None
    # Prune oldest, keeping the newest `keep` snapshots (name sorts chronologically).
    snaps = sorted(backups.glob(f"{src_path.stem}-*{src_path.suffix}"))
    for old in snaps[: max(0, len(snaps) - max(1, keep))]:
        try:
            old.unlink()
        except OSError:
            logger.debug("could not prune old backup %s", old, exc_info=True)
    logger.info("db backup written: %s (keeping %d)", dest.name, keep)
    return dest
