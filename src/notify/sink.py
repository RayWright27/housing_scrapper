"""Composition glue between the DB-free notifier and the repository.

:mod:`src.notify.telegram` keeps no DB access (§2, §9b): the listing metadata a
message needs and the outbox that persists undeliverable messages are both
injected. This module builds those injected seams over an open connection, so
*both* entry points that notify — the CLI (:mod:`src.main`) and the web
dashboard's add (:mod:`src.web.app`) — share one implementation instead of
duplicating it. It is the only place outside the CLI/web composition roots that
wires the notifier to storage.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.notify.telegram import (
    ListingMeta,
    MetaLookup,
    Outbox,
    PendingMessage,
    TelegramNotifier,
)
from src.storage import repository as repo


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_meta_lookup(conn: sqlite3.Connection) -> MetaLookup:
    """A ``get_meta(listing_id)`` closure for the notifier over an open conn."""

    def get_meta(listing_id: int) -> ListingMeta | None:
        row = repo.get_listing(conn, listing_id)
        if row is None:
            return None
        return ListingMeta(rooms=row["rooms"], area_total=row["area_total"],
                           title=row["title"], address=row["address"],
                           target_price=repo.get_target(conn, listing_id))

    return get_meta


def make_outbox(conn: sqlite3.Connection) -> Outbox:
    """A DB-backed :class:`~src.notify.telegram.Outbox` over an open conn.

    Persists messages that fail to send and replays them once Telegram is
    reachable again (oldest first)."""

    class _SqliteOutbox:
        def pending(self) -> list[PendingMessage]:
            return [PendingMessage(id=r["id"], chat_id=r["chat_id"], text=r["text"])
                    for r in repo.pending_notifications(conn)]

        def remember(self, chat_id: str, text: str) -> None:
            repo.enqueue_notification(conn, chat_id, text, _now())

        def forget(self, message_id: int) -> None:
            repo.delete_notification(conn, message_id)

        def attempted(self, message_id: int, error: str) -> None:
            repo.mark_notification_attempt(conn, message_id, _now(), error)

    return _SqliteOutbox()


def notify_events(conn: sqlite3.Connection, events, settings) -> None:
    """Deliver ``events`` to Telegram over ``conn``.

    Uses the persistent outbox so a failed send (e.g. Telegram unreachable) is
    queued and replayed later, and flushes any existing backlog first. A no-op
    when Telegram is unconfigured. Never raises. Must run while ``conn`` is open."""
    TelegramNotifier.from_settings(settings, outbox=make_outbox(conn)).notify(
        events, make_meta_lookup(conn)
    )
