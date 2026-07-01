"""Normalization tests — pure logic on fabricated RawListing inputs, plus one
light integration test against an in-memory DB. No network, no DB file."""

from __future__ import annotations

import json

import pytest

from src.adapters.base import RawListing
from src.normalize import Listing, NormalizationError, normalize
from src.storage import repository as repo
from src.storage.db import init_db

NOW = "2026-07-01T12:00:00Z"


def test_full_field_mapping() -> None:
    raw = RawListing(
        source="cian",
        external_id="123",
        url="https://cian.ru/sale/flat/123",
        price=10_000_000,
        title="2-room near park",
        address="Moscow, Park St 1",
        rooms=2,
        area_total=54.0,
        floor=5,
        floors_total=12,
        extra={"area_living": 30.5, "area_kitchen": 9.0},
    )
    listing = normalize(raw)

    assert listing.source == "cian"
    assert listing.external_id == "123"
    assert listing.url == "https://cian.ru/sale/flat/123"
    assert listing.title == "2-room near park"
    assert listing.address == "Moscow, Park St 1"
    assert listing.rooms == 2
    assert listing.area_total == 54.0
    assert listing.area_living == 30.5
    assert listing.area_kitchen == 9.0
    assert listing.floor == 5
    assert listing.floors_total == 12


def test_price_is_not_carried_onto_listing() -> None:
    # Price lives in price_history, never on Listing (CLAUDE.md §5).
    listing = normalize(
        RawListing(source="cian", external_id="1", url="u", price=9_000_000)
    )
    assert not hasattr(listing, "price")


def test_all_optionals_missing_become_none() -> None:
    # Only the mandatory fields are provided.
    raw = RawListing(source="avito", external_id="x1", url="https://avito.ru/x1", price=500)
    listing = normalize(raw)

    assert listing.title is None
    assert listing.address is None
    assert listing.rooms is None
    assert listing.area_total is None
    assert listing.area_living is None      # not on RawListing, absent from extra
    assert listing.area_kitchen is None
    assert listing.floor is None
    assert listing.floors_total is None


def test_area_living_and_kitchen_pulled_from_extra() -> None:
    raw = RawListing(
        source="cian",
        external_id="7",
        url="u",
        price=1,
        extra={"area_living": 40.0, "area_kitchen": 12.5},
    )
    listing = normalize(raw)
    assert listing.area_living == 40.0
    assert listing.area_kitchen == 12.5


def test_studio_passed_through_as_zero_rooms() -> None:
    raw = RawListing(source="cian", external_id="s1", url="u", price=1, rooms=0)
    assert normalize(raw).rooms == 0


def test_studio_flag_in_extra_maps_to_zero_rooms() -> None:
    raw = RawListing(
        source="cian", external_id="s2", url="u", price=1, extra={"is_studio": True}
    )
    assert normalize(raw).rooms == 0


def test_missing_external_id_raises() -> None:
    raw = RawListing(source="cian", external_id="", url="u", price=1)
    with pytest.raises(NormalizationError):
        normalize(raw)


def test_raw_json_is_serializable_snapshot() -> None:
    raw = RawListing(
        source="cian", external_id="9", url="u", price=1, extra={"foo": "bar"}
    )
    listing = normalize(raw)
    assert isinstance(listing.raw_json, dict)
    # Must be JSON-serializable and carry the raw payload for debugging.
    dumped = json.loads(json.dumps(listing.raw_json))
    assert dumped["external_id"] == "9"
    assert dumped["price"] == 1
    assert dumped["extra"] == {"foo": "bar"}


def test_normalize_output_persists_and_reads_back_unchanged() -> None:
    # Integration: normalize() -> upsert_listing() -> read back, no drift.
    conn = init_db(":memory:")
    try:
        raw = RawListing(
            source="cian",
            external_id="42",
            url="https://cian.ru/42",
            price=8_000_000,
            title="flat",
            address="somewhere",
            rooms=3,
            area_total=70.0,
            floor=4,
            floors_total=9,
            extra={"area_living": 45.0, "area_kitchen": 11.0},
        )
        listing: Listing = normalize(raw)

        lid = repo.upsert_listing(
            conn,
            source=listing.source,
            external_id=listing.external_id,
            url=listing.url,
            now=NOW,
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

        row = conn.execute("SELECT * FROM listings WHERE id = ?", (lid,)).fetchone()
        assert row["source"] == listing.source
        assert row["external_id"] == listing.external_id
        assert row["url"] == listing.url
        assert row["title"] == listing.title
        assert row["address"] == listing.address
        assert row["rooms"] == listing.rooms
        assert row["area_total"] == listing.area_total
        assert row["area_living"] == listing.area_living
        assert row["area_kitchen"] == listing.area_kitchen
        assert row["floor"] == listing.floor
        assert row["floors_total"] == listing.floors_total
        assert json.loads(row["raw_json"]) == listing.raw_json
    finally:
        conn.close()
