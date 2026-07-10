"""Data-access helpers over the SQLite schema (CLAUDE.md §5, §15).

Pure persistence: these functions take an open connection and read/write rows.
They contain no scraping, no notification, and no derived-value storage —
ratios like ₽/m² and price deltas are computed elsewhere, never stored.

Hard rules honored here:
- ``price_history`` is append-only: :func:`record_price` only INSERTs.
- Money is integer rubles.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def add_tracked_source(
    conn: sqlite3.Connection,
    source: str,
    url: str,
    kind: str = "listing",
    note: str | None = None,
    active: bool = True,
) -> int:
    """Insert a row into ``tracked_sources``; return its id."""
    cur = conn.execute(
        """
        INSERT INTO tracked_sources (source, url, kind, note, active)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source, url, kind, note, 1 if active else 0),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_tracked(
    conn: sqlite3.Connection, active_only: bool = True
) -> list[sqlite3.Row]:
    """Return rows from ``tracked_sources`` (active ones by default)."""
    if active_only:
        rows = conn.execute(
            "SELECT * FROM tracked_sources WHERE active = 1 ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tracked_sources ORDER BY id"
        ).fetchall()
    return list(rows)


def deactivate_tracked_source(conn: sqlite3.Connection, tracked_id: int) -> None:
    """Soft-remove a tracked source (set ``active = 0``); history is kept."""
    conn.execute(
        "UPDATE tracked_sources SET active = 0 WHERE id = ?", (tracked_id,)
    )
    conn.commit()


def upsert_listing(
    conn: sqlite3.Connection,
    *,
    source: str,
    external_id: str,
    url: str,
    now: str,
    title: str | None = None,
    address: str | None = None,
    rooms: int | None = None,
    area_total: float | None = None,
    area_living: float | None = None,
    area_kitchen: float | None = None,
    floor: int | None = None,
    floors_total: int | None = None,
    raw_json: dict[str, Any] | None = None,
) -> int:
    """Insert a listing or update the existing one on ``(source, external_id)``.

    ``now`` is an ISO-8601 UTC timestamp used for ``first_seen_at`` (on insert)
    and ``last_seen_at`` (always). An upserted listing is marked active.
    Returns the listing's internal id.
    """
    raw_text = json.dumps(raw_json, ensure_ascii=False) if raw_json is not None else None
    conn.execute(
        """
        INSERT INTO listings (
            source, external_id, url, title, address, rooms,
            area_total, area_living, area_kitchen, floor, floors_total,
            first_seen_at, last_seen_at, is_active, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT (source, external_id) DO UPDATE SET
            url          = excluded.url,
            title        = excluded.title,
            address      = excluded.address,
            rooms        = excluded.rooms,
            area_total   = excluded.area_total,
            area_living  = excluded.area_living,
            area_kitchen = excluded.area_kitchen,
            floor        = excluded.floor,
            floors_total = excluded.floors_total,
            last_seen_at = excluded.last_seen_at,
            is_active    = 1,
            raw_json     = excluded.raw_json
        """,
        (
            source, external_id, url, title, address, rooms,
            area_total, area_living, area_kitchen, floor, floors_total,
            now, now, raw_text,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM listings WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    return int(row["id"])


def list_listings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all rows from ``listings``."""
    return list(conn.execute("SELECT * FROM listings ORDER BY id").fetchall())


def get_listing(conn: sqlite3.Connection, listing_id: int) -> sqlite3.Row | None:
    """Return a single listing row by internal id, or ``None``."""
    return conn.execute(
        "SELECT * FROM listings WHERE id = ?", (listing_id,)
    ).fetchone()


# --------------------------------------------------------------------------- #
# source <-> listing links + consecutive-miss counter (drives delisting, §8.7)
# --------------------------------------------------------------------------- #
def link_source_listing(
    conn: sqlite3.Connection, tracked_source_id: int, listing_id: int
) -> None:
    """Record that a source produced a listing this run; reset its miss counter.

    Idempotent: creates the (source, listing) link if absent, and on every
    sighting resets ``consecutive_misses`` to 0 and re-activates the link.
    """
    conn.execute(
        """
        INSERT INTO source_listings (tracked_source_id, listing_id,
                                     consecutive_misses, is_linked)
        VALUES (?, ?, 0, 1)
        ON CONFLICT (tracked_source_id, listing_id) DO UPDATE SET
            consecutive_misses = 0,
            is_linked = 1
        """,
        (tracked_source_id, listing_id),
    )
    conn.commit()


def get_active_links_for_source(
    conn: sqlite3.Connection, tracked_source_id: int
) -> list[sqlite3.Row]:
    """Return the still-linked (is_linked=1) rows for a tracked source."""
    return list(
        conn.execute(
            """
            SELECT tracked_source_id, listing_id, consecutive_misses, is_linked
            FROM source_listings
            WHERE tracked_source_id = ? AND is_linked = 1
            ORDER BY listing_id
            """,
            (tracked_source_id,),
        ).fetchall()
    )


def increment_miss(
    conn: sqlite3.Connection, tracked_source_id: int, listing_id: int
) -> int:
    """Increment and return the consecutive-miss counter for a link."""
    conn.execute(
        """
        UPDATE source_listings SET consecutive_misses = consecutive_misses + 1
        WHERE tracked_source_id = ? AND listing_id = ?
        """,
        (tracked_source_id, listing_id),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT consecutive_misses FROM source_listings
        WHERE tracked_source_id = ? AND listing_id = ?
        """,
        (tracked_source_id, listing_id),
    ).fetchone()
    return int(row["consecutive_misses"])


def delist_link(
    conn: sqlite3.Connection, tracked_source_id: int, listing_id: int
) -> None:
    """Delist a listing from a source: unlink it and mark the listing inactive.

    A later reappearance re-activates the listing via :func:`upsert_listing`
    and re-links it via :func:`link_source_listing`.
    """
    conn.execute(
        """
        UPDATE source_listings SET is_linked = 0
        WHERE tracked_source_id = ? AND listing_id = ?
        """,
        (tracked_source_id, listing_id),
    )
    conn.execute("UPDATE listings SET is_active = 0 WHERE id = ?", (listing_id,))
    conn.commit()


def record_price(
    conn: sqlite3.Connection,
    listing_id: int,
    price: int,
    observed_at: str,
    currency: str = "RUB",
) -> int:
    """Append a price observation. INSERT only — never UPDATE (§15).

    ``price`` is integer rubles; ``observed_at`` is ISO-8601 UTC.
    """
    cur = conn.execute(
        """
        INSERT INTO price_history (listing_id, price, currency, observed_at)
        VALUES (?, ?, ?, ?)
        """,
        (listing_id, int(price), currency, observed_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def last_price(conn: sqlite3.Connection, listing_id: int) -> int | None:
    """Return the most recent observed price for a listing, or ``None``.

    Newest is determined by ``observed_at``, then by insertion order (id) as a
    tiebreaker for observations sharing a timestamp.
    """
    row = conn.execute(
        """
        SELECT price FROM price_history
        WHERE listing_id = ?
        ORDER BY observed_at DESC, id DESC
        LIMIT 1
        """,
        (listing_id,),
    ).fetchone()
    return int(row["price"]) if row is not None else None


def count_active_links(conn: sqlite3.Connection, tracked_source_id: int) -> int:
    """How many listings this tracked source is currently linked to (is_linked=1)."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM source_listings "
        "WHERE tracked_source_id = ? AND is_linked = 1",
        (tracked_source_id,),
    ).fetchone()
    return int(row["c"])


# --------------------------------------------------------------------------- #
# outbox: Telegram messages that failed to send, replayed once delivery works
# --------------------------------------------------------------------------- #
def enqueue_notification(
    conn: sqlite3.Connection,
    text: str,
    created_at: str,
    *,
    last_error: str | None = None,
) -> int:
    """Append a rendered message to the outbox; return its id.

    ``created_at`` is ISO-8601 UTC. ``attempts`` starts at 1 because a message is
    only ever enqueued *after* a delivery attempt has already failed.
    """
    cur = conn.execute(
        """
        INSERT INTO pending_notifications (text, created_at, attempts,
                                           last_attempt_at, last_error)
        VALUES (?, ?, 1, ?, ?)
        """,
        (text, created_at, created_at, last_error),
    )
    conn.commit()
    return int(cur.lastrowid)


def pending_notifications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return queued messages, oldest first (the order they must be resent in)."""
    return list(
        conn.execute(
            "SELECT * FROM pending_notifications ORDER BY created_at, id"
        ).fetchall()
    )


def delete_notification(conn: sqlite3.Connection, notification_id: int) -> None:
    """Drop a message from the outbox once it has been delivered."""
    conn.execute(
        "DELETE FROM pending_notifications WHERE id = ?", (notification_id,)
    )
    conn.commit()


def mark_notification_attempt(
    conn: sqlite3.Connection,
    notification_id: int,
    attempted_at: str,
    error: str | None,
) -> None:
    """Record a failed replay attempt (bump counter, note time + error type)."""
    conn.execute(
        """
        UPDATE pending_notifications
        SET attempts = attempts + 1, last_attempt_at = ?, last_error = ?
        WHERE id = ?
        """,
        (attempted_at, error, notification_id),
    )
    conn.commit()


def delete_tracked_source(conn: sqlite3.Connection, tracked_id: int) -> None:
    """Hard-delete a tracked source and its source-listing links.

    Listings and their price history are shared, durable data and are kept —
    only the watchlist entry and its bookkeeping links are removed.
    """
    conn.execute(
        "DELETE FROM source_listings WHERE tracked_source_id = ?", (tracked_id,)
    )
    conn.execute("DELETE FROM tracked_sources WHERE id = ?", (tracked_id,))
    conn.commit()
