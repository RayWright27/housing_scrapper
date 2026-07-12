"""Storage-layer tests on an in-memory SQLite DB — no network, no DB file."""

from __future__ import annotations

import sqlite3

import pytest

from src.storage import repository as repo
from src.storage.db import backup_db, init_db

NOW = "2026-06-30T12:00:00Z"
LATER = "2026-06-30T18:00:00Z"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = init_db(":memory:")
    yield c
    c.close()


def test_backup_db_snapshots_and_prunes(tmp_path) -> None:
    db_file = tmp_path / "tracker.db"
    c = init_db(str(db_file))
    repo.add_tracked_source(c, "cian", "u", "listing")
    c.close()

    # Make several backups keeping only the last 2; older ones are pruned away.
    made = [backup_db(str(db_file), keep=2) for _ in range(3)]
    assert all(p is not None for p in made)     # each snapshot was created
    kept = sorted((tmp_path / "backups").glob("tracker-*.db"))
    assert len(kept) == 2                       # pruned to `keep`
    assert not made[0].exists() and made[-1].exists()  # oldest gone, newest kept
    copy = sqlite3.connect(kept[-1])
    assert copy.execute("SELECT COUNT(*) FROM tracked_sources").fetchone()[0] == 1
    copy.close()


def test_backup_db_noop_for_memory_or_missing(tmp_path) -> None:
    assert backup_db(":memory:") is None
    assert backup_db(str(tmp_path / "nope.db")) is None


def test_bootstrap_creates_tables_and_index(conn: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"tracked_sources", "listings", "price_history"} <= tables

    indexes = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_price_history_listing_observed" in indexes


def test_add_and_get_tracked(conn: sqlite3.Connection) -> None:
    repo.add_tracked_source(conn, "cian", "https://cian.ru/1", note="park flat")
    inactive = repo.add_tracked_source(
        conn, "avito", "https://avito.ru/2", active=False
    )

    active = repo.get_tracked(conn)
    assert len(active) == 1
    assert active[0]["source"] == "cian"

    all_rows = repo.get_tracked(conn, active_only=False)
    assert len(all_rows) == 2

    repo.deactivate_tracked_source(conn, active[0]["id"])
    assert repo.get_tracked(conn) == []
    # History preserved: row still exists, just inactive.
    assert len(repo.get_tracked(conn, active_only=False)) == 2
    assert inactive  # id returned


def test_delete_tracked_source_removes_row_and_links_but_keeps_history(
    conn: sqlite3.Connection,
) -> None:
    src = repo.add_tracked_source(conn, "cian", "https://cian.ru/1", "listing")
    lid = repo.upsert_listing(conn, source="cian", external_id="1", url="u", now=NOW)
    repo.link_source_listing(conn, src, lid)
    repo.record_price(conn, lid, 5_000_000, NOW)
    assert repo.count_active_links(conn, src) == 1

    repo.delete_tracked_source(conn, src)

    assert repo.get_tracked(conn, active_only=False) == []       # watchlist row gone
    assert repo.count_active_links(conn, src) == 0               # links gone
    assert repo.get_listing(conn, lid) is not None               # listing kept
    assert repo.last_price(conn, lid) == 5_000_000               # history kept


def test_count_active_links_ignores_delisted_links(conn: sqlite3.Connection) -> None:
    src = repo.add_tracked_source(conn, "cian", "https://cian.ru/s", "search")
    a = repo.upsert_listing(conn, source="cian", external_id="a", url="u", now=NOW)
    b = repo.upsert_listing(conn, source="cian", external_id="b", url="u", now=NOW)
    repo.link_source_listing(conn, src, a)
    repo.link_source_listing(conn, src, b)
    assert repo.count_active_links(conn, src) == 2

    repo.delist_link(conn, src, b)  # b unlinked from this source
    assert repo.count_active_links(conn, src) == 1


def test_upsert_is_idempotent_on_identity(conn: sqlite3.Connection) -> None:
    first = repo.upsert_listing(
        conn,
        source="cian",
        external_id="abc",
        url="https://cian.ru/abc",
        now=NOW,
        title="old title",
        rooms=2,
    )
    second = repo.upsert_listing(
        conn,
        source="cian",
        external_id="abc",  # same identity
        url="https://cian.ru/abc-renamed",
        now=LATER,
        title="new title",
        rooms=3,
    )

    assert first == second  # same row, not a duplicate
    rows = repo.list_listings(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "new title"
    assert row["rooms"] == 3
    assert row["first_seen_at"] == NOW       # preserved on update
    assert row["last_seen_at"] == LATER      # advanced
    assert row["is_active"] == 1


def test_upsert_reactivates_listing(conn: sqlite3.Connection) -> None:
    lid = repo.upsert_listing(
        conn, source="cian", external_id="x", url="u", now=NOW
    )
    conn.execute("UPDATE listings SET is_active = 0 WHERE id = ?", (lid,))
    conn.commit()

    repo.upsert_listing(conn, source="cian", external_id="x", url="u", now=LATER)
    row = conn.execute("SELECT is_active FROM listings WHERE id = ?", (lid,)).fetchone()
    assert row["is_active"] == 1


def test_optional_fields_default_to_null(conn: sqlite3.Connection) -> None:
    lid = repo.upsert_listing(
        conn, source="cian", external_id="y", url="u", now=NOW
    )
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (lid,)).fetchone()
    assert row["area_total"] is None
    assert row["floor"] is None
    assert row["raw_json"] is None


def test_record_price_appends_and_never_overwrites(conn: sqlite3.Connection) -> None:
    lid = repo.upsert_listing(
        conn, source="cian", external_id="abc", url="u", now=NOW
    )
    repo.record_price(conn, lid, 10_000_000, NOW)
    repo.record_price(conn, lid, 9_500_000, LATER)

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM price_history WHERE listing_id = ?", (lid,)
    ).fetchone()["n"]
    assert count == 2  # both observations retained


def test_last_price_returns_newest(conn: sqlite3.Connection) -> None:
    lid = repo.upsert_listing(
        conn, source="cian", external_id="abc", url="u", now=NOW
    )
    assert repo.last_price(conn, lid) is None  # no observations yet

    repo.record_price(conn, lid, 10_000_000, NOW)
    repo.record_price(conn, lid, 9_500_000, LATER)
    assert repo.last_price(conn, lid) == 9_500_000


def test_record_price_defaults_to_rub(conn: sqlite3.Connection) -> None:
    lid = repo.upsert_listing(
        conn, source="cian", external_id="abc", url="u", now=NOW
    )
    repo.record_price(conn, lid, 1_000_000, NOW)
    row = conn.execute(
        "SELECT currency FROM price_history WHERE listing_id = ?", (lid,)
    ).fetchone()
    assert row["currency"] == "RUB"


def test_price_history_foreign_key_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        repo.record_price(conn, listing_id=999, price=1, observed_at=NOW)
