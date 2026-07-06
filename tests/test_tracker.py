"""Tracker orchestration tests: fake adapter + in-memory DB, zero network (§13)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.adapters.base import RawListing
from src.storage import repository as repo
from src.storage.db import init_db
from src.tracker import EventType, ingest_raws, run_once


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class FakeAdapter:
    """In-memory stand-in for a SiteAdapter. Tests mutate its responses
    between run_once() calls to simulate price changes, misses, etc."""

    def __init__(self, source: str = "cian") -> None:
        self.source = source
        self.listing: dict[str, RawListing | None] = {}
        self.search: dict[str, list[RawListing]] = {}
        self.raise_on: set[str] = set()

    def fetch_listing(self, url: str) -> RawListing | None:
        if url in self.raise_on:
            raise RuntimeError("simulated fetch blow-up")
        return self.listing.get(url)

    def fetch_search(self, url: str) -> list[RawListing]:
        if url in self.raise_on:
            raise RuntimeError("simulated fetch blow-up")
        return list(self.search.get(url, []))


class Clock:
    """Distinct, strictly-increasing ISO-8601 UTC timestamp per run."""

    def __init__(self) -> None:
        self._n = 0

    def __call__(self) -> str:
        self._n += 1
        return (datetime(2026, 7, 2, tzinfo=timezone.utc)
                + timedelta(minutes=self._n)).isoformat()


def raw(ext: str, price: int, url: str | None = None, **extra) -> RawListing:
    return RawListing(
        source="cian", external_id=ext,
        url=url or f"https://cian.ru/sale/flat/{ext}/",
        price=price, extra=extra or None,
    )


def settings(n: int = 3) -> SimpleNamespace:
    return SimpleNamespace(delist_after_misses=n)


def lid(conn, ext: str) -> int | None:
    row = conn.execute("SELECT id FROM listings WHERE external_id = ?", (ext,)).fetchone()
    return row["id"] if row else None


def price_rows(conn, listing_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM price_history WHERE listing_id = ?", (listing_id,)
    ).fetchone()["c"]


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# first-seen / unchanged / changed
# --------------------------------------------------------------------------- #
def test_first_seen_emits_now_tracking_and_one_price_row(conn) -> None:
    url = "https://cian.ru/sale/flat/1/"
    repo.add_tracked_source(conn, "cian", url, "listing")
    fake = FakeAdapter()
    fake.listing[url] = raw("1", 10_000_000, url)

    events = run_once({"cian": fake}, conn, settings(), now_fn=Clock())

    assert len(events) == 1
    assert events[0].type == EventType.NOW_TRACKING
    assert events[0].price == 10_000_000
    assert price_rows(conn, lid(conn, "1")) == 1


def test_unchanged_price_writes_nothing_and_emits_nothing(conn) -> None:
    # The single most important rule (§8.6).
    url = "https://cian.ru/sale/flat/1/"
    repo.add_tracked_source(conn, "cian", url, "listing")
    fake = FakeAdapter()
    fake.listing[url] = raw("1", 10_000_000, url)
    clock = Clock()

    run_once({"cian": fake}, conn, settings(), now_fn=clock)          # first-seen
    events = run_once({"cian": fake}, conn, settings(), now_fn=clock)  # same price

    assert events == []
    assert price_rows(conn, lid(conn, "1")) == 1  # no new row


def test_price_change_records_row_and_emits_delta_percent(conn) -> None:
    url = "https://cian.ru/sale/flat/1/"
    repo.add_tracked_source(conn, "cian", url, "listing")
    fake = FakeAdapter()
    clock = Clock()
    fake.listing[url] = raw("1", 10_000_000, url)
    run_once({"cian": fake}, conn, settings(), now_fn=clock)

    fake.listing[url] = raw("1", 12_000_000, url)  # +20%
    events = run_once({"cian": fake}, conn, settings(), now_fn=clock)

    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.PRICE_CHANGED
    assert ev.old_price == 10_000_000
    assert ev.new_price == 12_000_000
    assert ev.delta == 2_000_000
    assert ev.percent == pytest.approx(20.0)
    assert price_rows(conn, lid(conn, "1")) == 2


# --------------------------------------------------------------------------- #
# per-source isolation
# --------------------------------------------------------------------------- #
def test_one_source_failing_does_not_abort_the_run(conn) -> None:
    url_ok = "https://cian.ru/sale/flat/1/"
    url_bad = "https://cian.ru/sale/flat/2/"
    repo.add_tracked_source(conn, "cian", url_bad, "listing")   # fails first
    repo.add_tracked_source(conn, "cian", url_ok, "listing")
    fake = FakeAdapter()
    fake.raise_on.add(url_bad)
    fake.listing[url_ok] = raw("1", 5_000_000, url_ok)

    events = run_once({"cian": fake}, conn, settings(), now_fn=Clock())

    # the good source still produced its event despite the bad one raising
    assert [e.type for e in events] == [EventType.NOW_TRACKING]
    assert events[0].external_id == "1"


# --------------------------------------------------------------------------- #
# delisting (§8.7)
# --------------------------------------------------------------------------- #
def test_single_miss_does_not_delist(conn) -> None:
    url = "https://cian.ru/sale/flat/1/"
    repo.add_tracked_source(conn, "cian", url, "listing")
    fake = FakeAdapter()
    clock = Clock()
    fake.listing[url] = raw("1", 5_000_000, url)
    run_once({"cian": fake}, conn, settings(3), now_fn=clock)  # first-seen

    fake.listing[url] = None  # a block/miss
    events = run_once({"cian": fake}, conn, settings(3), now_fn=clock)

    assert events == []  # not delisted after a single miss
    assert repo.get_listing(conn, lid(conn, "1"))["is_active"] == 1


def test_n_consecutive_misses_delist_exactly_once(conn) -> None:
    url = "https://cian.ru/sale/flat/1/"
    repo.add_tracked_source(conn, "cian", url, "listing")
    fake = FakeAdapter()
    clock = Clock()
    fake.listing[url] = raw("1", 5_000_000, url)
    run_once({"cian": fake}, conn, settings(2), now_fn=clock)  # first-seen

    fake.listing[url] = None
    delisted = []
    for _ in range(4):  # miss repeatedly, well past the threshold
        evs = run_once({"cian": fake}, conn, settings(2), now_fn=clock)
        delisted += [e for e in evs if e.type == EventType.DELISTED]

    assert len(delisted) == 1  # emitted once, on the transition only
    assert repo.get_listing(conn, lid(conn, "1"))["is_active"] == 0


def test_reappearance_reactivates_after_delist(conn) -> None:
    url = "https://cian.ru/sale/flat/1/"
    repo.add_tracked_source(conn, "cian", url, "listing")
    fake = FakeAdapter()
    clock = Clock()
    fake.listing[url] = raw("1", 5_000_000, url)
    run_once({"cian": fake}, conn, settings(1), now_fn=clock)  # first-seen
    fake.listing[url] = None
    run_once({"cian": fake}, conn, settings(1), now_fn=clock)  # 1 miss -> delisted
    assert repo.get_listing(conn, lid(conn, "1"))["is_active"] == 0

    fake.listing[url] = raw("1", 5_000_000, url)  # comes back, same price
    run_once({"cian": fake}, conn, settings(1), now_fn=clock)
    assert repo.get_listing(conn, lid(conn, "1"))["is_active"] == 1


# --------------------------------------------------------------------------- #
# search source
# --------------------------------------------------------------------------- #
def test_ingest_raws_tracks_and_detects_without_delisting(conn) -> None:
    # ingest (hand-saved page): records first price + NOW_TRACKING, then a change.
    src = repo.add_tracked_source(conn, "cian", "https://cian.ru/sale/flat/1/", "listing")
    src_row = next(r for r in repo.get_tracked(conn) if r["id"] == src)
    clock = Clock()

    e1 = ingest_raws(conn, src_row, [raw("1", 5_000_000)], now_fn=clock)
    assert [e.type for e in e1] == [EventType.NOW_TRACKING]

    e2 = ingest_raws(conn, src_row, [raw("1", 5_000_000)], now_fn=clock)  # same price
    assert e2 == []  # §8.6

    e3 = ingest_raws(conn, src_row, [raw("1", 5_500_000)], now_fn=clock)  # changed
    assert [e.type for e in e3] == [EventType.PRICE_CHANGED]
    assert e3[0].delta == 500_000
    assert price_rows(conn, lid(conn, "1")) == 2  # append-only; no delisting side effects


def test_search_source_tracks_each_listing(conn) -> None:
    url = "https://cian.ru/cat.php?deal_type=sale"
    repo.add_tracked_source(conn, "cian", url, "search")
    fake = FakeAdapter()
    fake.search[url] = [raw("1", 100), raw("2", 200), raw("3", 300)]

    events = run_once({"cian": fake}, conn, settings(), now_fn=Clock())

    assert len(events) == 3
    assert all(e.type == EventType.NOW_TRACKING for e in events)
    assert len(repo.list_listings(conn)) == 3
    for ext in ("1", "2", "3"):
        assert price_rows(conn, lid(conn, ext)) == 1


def test_search_dropout_does_not_delist(conn) -> None:
    # Policy A: a 'search' source's page-1 membership rotates as the site
    # re-ranks, so a listing dropping out is NOT a delisting (discovery-only).
    url = "https://cian.ru/cat.php?deal_type=sale"
    repo.add_tracked_source(conn, "cian", url, "search")
    fake = FakeAdapter()
    clock = Clock()
    fake.search[url] = [raw("1", 100), raw("2", 200), raw("3", 300)]
    run_once({"cian": fake}, conn, settings(1), now_fn=clock)

    fake.search[url] = [raw("1", 100), raw("2", 200)]  # #3 rotated off page 1
    # even with delist_after=1, and repeatedly missing, #3 is never delisted
    events = run_once({"cian": fake}, conn, settings(1), now_fn=clock)
    events += run_once({"cian": fake}, conn, settings(1), now_fn=clock)

    assert [e for e in events if e.type == EventType.DELISTED] == []
    assert repo.get_listing(conn, lid(conn, "3"))["is_active"] == 1  # stays active


def test_listing_source_still_delists_after_n_misses(conn) -> None:
    # Delisting remains in force for a pinned 'listing' source.
    url = "https://cian.ru/sale/flat/9/"
    repo.add_tracked_source(conn, "cian", url, "listing")
    fake = FakeAdapter()
    clock = Clock()
    fake.listing[url] = raw("9", 100, url)
    run_once({"cian": fake}, conn, settings(2), now_fn=clock)  # first-seen

    fake.listing[url] = None
    evs = run_once({"cian": fake}, conn, settings(2), now_fn=clock)   # miss 1
    evs += run_once({"cian": fake}, conn, settings(2), now_fn=clock)  # miss 2 -> delist
    delisted = [e for e in evs if e.type == EventType.DELISTED]
    assert len(delisted) == 1 and delisted[0].external_id == "9"
    assert repo.get_listing(conn, lid(conn, "9"))["is_active"] == 0
