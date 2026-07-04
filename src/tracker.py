"""Orchestration: fetch -> normalize -> upsert -> diff -> events (CLAUDE.md §8).

This module GLUES existing pieces; it re-implements nothing. Site-specific
knowledge stays in the adapters (§15); the tracker does the DB writes and emits
the notification events that adapters are forbidden to do (§6).

Adapters are dependency-injected as a ``{source: SiteAdapter}`` mapping so the
whole pipeline can be driven by a fake in-memory adapter in tests — no network
(§13/§14). The live ``CianAdapter`` is constructed only at the CLI entrypoint.

Key behaviors:
- §8.6 unchanged price -> nothing written, no event (no noise).
- §8.4 first sighting -> record the first price, emit NOW_TRACKING (not CHANGED).
- §8.7 a listing missing for N consecutive runs -> DELISTED once. A single soft
  failure (block/captcha) is NOT a delisting; the N-consecutive threshold is
  what distinguishes a transient block from a genuine removal. Delisting is
  applied to pinned 'listing' sources only: a 'search' source's page-1 results
  rotate as the site re-ranks, so a listing dropping out is not evidence of
  removal (search is discovery-only).
- Per-source isolation: one source failing (soft failure or exception) is logged
  and never aborts the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Mapping

from src.adapters.base import RawListing, SiteAdapter
from src.changes import detect_price_change
from src.normalize import NormalizationError, Listing, normalize
from src.storage import repository as repo

logger = logging.getLogger("realty.tracker")


class EventType(str, Enum):
    NOW_TRACKING = "now_tracking"
    PRICE_CHANGED = "price_changed"
    DELISTED = "delisted"


@dataclass(frozen=True)
class Event:
    """A notification-worthy outcome of a run. The seam the dashboard/Telegram
    consume later — intentionally free of any site-specific detail."""

    type: EventType
    source: str
    listing_id: int
    external_id: str
    url: str
    note: str | None = None
    observed_at: str | None = None
    price: int | None = None          # NOW_TRACKING: the first observed price
    old_price: int | None = None      # PRICE_CHANGED
    new_price: int | None = None      # PRICE_CHANGED
    delta: int | None = None          # PRICE_CHANGED: new - old
    percent: float | None = None      # PRICE_CHANGED: delta / old * 100


def _utcnow() -> str:
    """A single ISO-8601 UTC timestamp for the run (§12)."""
    return datetime.now(timezone.utc).isoformat()


def _fetch(adapter: SiteAdapter, kind: str, url: str) -> list[RawListing]:
    """Fetch one source, normalizing listing/search to a uniform list.

    A soft failure returns an empty list. Politeness/rate-limiting lives in the
    adapter (§7); the tracker adds no sleeps of its own.
    """
    if kind == "search":
        return list(adapter.fetch_search(url))
    one = adapter.fetch_listing(url)
    return [one] if one is not None else []


def _process_listing(
    conn,
    src_row,
    raw: RawListing,
    now: str,
    events: list[Event],
) -> int | None:
    """Normalize + upsert one RawListing, record price, emit any event.

    Returns the internal listing id (so the caller can mark it seen), or
    ``None`` if the item could not be normalized (bad/absent identity).
    """
    try:
        listing: Listing = normalize(raw)
    except NormalizationError as exc:
        logger.warning("skipping unnormalizable item from source %s: %s",
                       src_row["id"], exc)
        return None

    listing_id = repo.upsert_listing(
        conn,
        source=listing.source,
        external_id=listing.external_id,
        url=listing.url,
        now=now,
        title=listing.title,
        address=listing.address,
        rooms=listing.rooms,
        area_total=listing.area_total,
        area_living=listing.area_living,
        area_kitchen=listing.area_kitchen,
        floor=listing.floor,
        floors_total=listing.floors_total,
        raw_json=listing.raw_json,
    )
    repo.link_source_listing(conn, src_row["id"], listing_id)

    previous = repo.last_price(conn, listing_id)
    change = detect_price_change(previous, raw.price)

    if change.kind == "first_seen":
        repo.record_price(conn, listing_id, raw.price, now)
        events.append(Event(
            type=EventType.NOW_TRACKING, source=listing.source,
            listing_id=listing_id, external_id=listing.external_id,
            url=listing.url, note=src_row["note"], observed_at=now,
            price=raw.price,
        ))
        logger.info("NOW_TRACKING %s %s @ %s", listing.source,
                    listing.external_id, raw.price)
    elif change.kind == "changed":
        repo.record_price(conn, listing_id, raw.price, now)
        events.append(Event(
            type=EventType.PRICE_CHANGED, source=listing.source,
            listing_id=listing_id, external_id=listing.external_id,
            url=listing.url, note=src_row["note"], observed_at=now,
            old_price=change.old_price, new_price=change.new_price,
            delta=change.delta, percent=change.percent,
        ))
        logger.info("PRICE_CHANGED %s %s %s -> %s (%+d, %.1f%%)",
                    listing.source, listing.external_id, change.old_price,
                    change.new_price, change.delta, change.percent)
    # change.kind == "unchanged": nothing written, no event (§8.6)

    return listing_id


def _handle_misses(
    conn,
    src_row,
    seen_listing_ids: set[int],
    delist_after: int,
    now: str,
    events: list[Event],
) -> None:
    """Increment miss counters for this source's linked-but-unseen listings;
    emit DELISTED once when a link crosses the consecutive-miss threshold."""
    for link in repo.get_active_links_for_source(conn, src_row["id"]):
        listing_id = link["listing_id"]
        if listing_id in seen_listing_ids:
            continue
        misses = repo.increment_miss(conn, src_row["id"], listing_id)
        if misses >= delist_after:
            repo.delist_link(conn, src_row["id"], listing_id)
            row = repo.get_listing(conn, listing_id)
            events.append(Event(
                type=EventType.DELISTED, source=src_row["source"],
                listing_id=listing_id,
                external_id=row["external_id"] if row else "",
                url=row["url"] if row else "", note=src_row["note"],
                observed_at=now,
            ))
            logger.info("DELISTED %s listing_id=%s after %d misses",
                        src_row["source"], listing_id, misses)


def process_source(
    adapters: Mapping[str, SiteAdapter],
    conn,
    settings,
    src_row,
    *,
    now: str,
) -> list[Event]:
    """Run the full pipeline for ONE tracked source and return its events.

    This is the single-source seam shared by :func:`run_once` (the scheduled
    pass) and the dashboard's add-and-fetch-once flow — the pipeline lives here
    exactly once. Per-source isolation: a soft failure or exception is logged
    and yields no events; it never propagates.
    """
    events: list[Event] = []
    source = src_row["source"]
    adapter = adapters.get(source)
    if adapter is None:
        logger.warning("no adapter wired for source %r (tracked_source %s); "
                       "skipping", source, src_row["id"])
        return events

    try:
        raws = _fetch(adapter, src_row["kind"], src_row["url"])
    except Exception:  # noqa: BLE001 - deliberately broad at the seam
        logger.exception("fetch failed for tracked_source %s (%s); "
                         "treating as a miss for this run", src_row["id"], source)
        raws = []

    seen_listing_ids: set[int] = set()
    for raw in raws:
        listing_id = _process_listing(conn, src_row, raw, now, events)
        if listing_id is not None:
            seen_listing_ids.add(listing_id)

    # Delisting applies to pinned 'listing' sources only. A 'search' source's
    # page-1 membership legitimately rotates run-to-run (the site re-ranks), so a
    # listing dropping out is NOT evidence of removal — treating it as a miss
    # produced false delistings. Search is discovery-only; to get removal
    # detection for a specific object, pin it as a 'listing' source.
    if src_row["kind"] != "search":
        _handle_misses(conn, src_row, seen_listing_ids,
                       settings.delist_after_misses, now, events)
    return events


def run_once(
    adapters: Mapping[str, SiteAdapter],
    conn,
    settings,
    *,
    now_fn: Callable[[], str] = _utcnow,
) -> list[Event]:
    """Run a single tracking pass over all active tracked sources.

    Returns the list of emitted events (and logs each). Safe to run repeatedly:
    change detection relies on ``last_price`` ordering, not insertion luck (§8).
    """
    now = now_fn()
    events: list[Event] = []
    for src_row in repo.get_tracked(conn, active_only=True):
        events.extend(process_source(adapters, conn, settings, src_row, now=now))
    return events

    return events
