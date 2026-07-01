"""Offline CIAN parse tests against saved fixtures. No network anywhere (§13).

Fixtures live in tests/fixtures/cian_*.html and were derived from real captures
(allow-list sanitized — no personal data). Field-variant permutations are built
in-process by re-embedding an offer dict in CIAN's real state format via
``wrap_offer`` — still fully offline and deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.adapters.cian import (
    classify_page,
    external_id_from_url,
    parse_listing,
    parse_search,
)

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def wrap_offer(offer: dict) -> str:
    """Embed one offer dict in CIAN's real offer-card state format."""
    state = {"offerData": {"offer": offer}}
    blob = json.dumps([{"key": "defaultState", "value": state}], ensure_ascii=False)
    return (
        "<html><head><title>t - база ЦИАН, объявление 1</title></head><body><script>"
        "window._cianConfig = window._cianConfig || {};"
        "window._cianConfig['frontend-offer-card'] = "
        "(window._cianConfig['frontend-offer-card'] || []).concat(" + blob + ");"
        "</script></body></html>"
    )


# --------------------------------------------------------------------------- #
# Full-field mapping against the real sanitized fixture
# --------------------------------------------------------------------------- #
def test_full_listing_maps_all_fields() -> None:
    raw = parse_listing(load("cian_listing_full.html"), "https://spb.cian.ru/sale/flat/321997972/")
    assert raw is not None
    assert raw.source == "cian"
    assert raw.external_id == "321997972"
    assert raw.price == 13537955          # integer rubles
    assert raw.rooms == 2
    assert raw.area_total == pytest.approx(46.76)
    assert raw.floor == 3
    assert raw.floors_total == 4
    assert raw.address  # joined from geo.address parts
    # living/kitchen land in extra (normalize reads them from there)
    assert raw.extra["area_living"] == pytest.approx(21.2)
    assert raw.extra["area_kitchen"] == pytest.approx(11.8)
    assert "raw" in raw.extra  # trimmed debug snapshot (#14)


def test_price_is_integer_rubles() -> None:
    raw = parse_listing(load("cian_listing_full.html"))
    assert isinstance(raw.price, int)


# --------------------------------------------------------------------------- #
# Missing optionals -> None
# --------------------------------------------------------------------------- #
def test_missing_optionals_become_none() -> None:
    raw = parse_listing(load("cian_listing_min.html"))
    assert raw is not None
    assert raw.floor is None
    assert raw.floors_total is None
    assert raw.address is None
    assert "area_living" not in raw.extra
    assert "area_kitchen" not in raw.extra


# --------------------------------------------------------------------------- #
# Studio / rooms variants
# --------------------------------------------------------------------------- #
def test_studio_maps_to_zero_rooms() -> None:
    raw = parse_listing(load("cian_studio.html"))
    assert raw is not None
    assert raw.rooms == 0


def test_free_layout_rooms_is_none_with_note() -> None:
    html = wrap_offer(
        {"cianId": 5, "flatType": "openPlan", "totalArea": "40",
         "bargainTerms": {"price": 5_000_000, "currency": "rur"}}
    )
    raw = parse_listing(html)
    assert raw.rooms is None
    assert raw.extra.get("layout") == "free"


def test_apartments_parse_rooms_normally() -> None:
    html = wrap_offer(
        {"cianId": 6, "flatType": "rooms", "roomsCount": 3, "isApartments": True,
         "totalArea": "80", "bargainTerms": {"price": 9_000_000, "currency": "rur"}}
    )
    raw = parse_listing(html)
    assert raw.rooms == 3
    assert raw.extra.get("is_apartments") is True


# --------------------------------------------------------------------------- #
# Living / kitchen area land in extra
# --------------------------------------------------------------------------- #
def test_living_and_kitchen_go_to_extra() -> None:
    html = wrap_offer(
        {"cianId": 7, "flatType": "rooms", "roomsCount": 1, "totalArea": "35.5",
         "livingArea": "18,4", "kitchenArea": "7.1",
         "bargainTerms": {"price": 6_000_000, "currency": "rur"}}
    )
    raw = parse_listing(html)
    assert raw.area_total == pytest.approx(35.5)
    assert raw.extra["area_living"] == pytest.approx(18.4)   # comma decimal parsed
    assert raw.extra["area_kitchen"] == pytest.approx(7.1)


# --------------------------------------------------------------------------- #
# Price-format variants
# --------------------------------------------------------------------------- #
def test_price_prefers_price_total_rur() -> None:
    html = wrap_offer(
        {"cianId": 8, "priceTotalRur": 12_000_000,
         "bargainTerms": {"price": 180_000, "currency": "usd"}}
    )
    assert parse_listing(html).price == 12_000_000


def test_price_falls_back_to_rur_bargain_terms() -> None:
    html = wrap_offer(
        {"cianId": 9, "bargainTerms": {"price": 8_500_000, "currency": "rur"}}
    )
    assert parse_listing(html).price == 8_500_000


def test_rent_records_period_in_extra() -> None:
    html = wrap_offer(
        {"cianId": 10, "dealType": "rent", "roomsCount": 1, "totalArea": "40",
         "bargainTerms": {"price": 45_000, "currency": "rur", "paymentPeriod": "monthly"}}
    )
    raw = parse_listing(html)
    assert raw.price == 45_000
    assert raw.extra.get("rent_period") == "monthly"


def test_non_rur_without_normalized_price_is_unsupported() -> None:
    html = wrap_offer(
        {"cianId": 11, "bargainTerms": {"price": 200_000, "currency": "usd"}}
    )
    assert parse_listing(html) is None  # no FX -> hard skip for this item


def test_missing_price_is_hard_skip() -> None:
    html = wrap_offer({"cianId": 12, "flatType": "rooms", "roomsCount": 2})
    assert parse_listing(html) is None


def test_missing_external_id_is_hard_skip() -> None:
    html = wrap_offer({"bargainTerms": {"price": 5_000_000, "currency": "rur"}})
    assert parse_listing(html) is None


# --------------------------------------------------------------------------- #
# external_id extraction from URLs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.cian.ru/sale/flat/328700780/", "328700780"),
        ("https://spb.cian.ru/sale/flat/328700780/", "328700780"),
        ("https://www.cian.ru/rent/flat/12345678", "12345678"),
        ("https://m2.cian.ru/sale/room/99887766/?foo=bar", "99887766"),
        ("https://spb.cian.ru/sale/house/555444/#gallery", "555444"),
        ("https://www.cian.ru/cat.php?deal_type=sale", None),
    ],
)
def test_external_id_from_url(url: str, expected: str | None) -> None:
    assert external_id_from_url(url) == expected


def test_payload_id_wins_over_url_id() -> None:
    # URL says 111, payload says 222 -> trust payload (mismatch is logged).
    html = wrap_offer(
        {"cianId": 222, "bargainTerms": {"price": 5_000_000, "currency": "rur"}}
    )
    raw = parse_listing(html, "https://www.cian.ru/sale/flat/111/")
    assert raw.external_id == "222"


# --------------------------------------------------------------------------- #
# Block / removed detection -> soft failure
# --------------------------------------------------------------------------- #
def test_captcha_page_is_soft_failure() -> None:
    html = load("cian_captcha.html")
    assert classify_page(html) == "captcha"
    assert parse_listing(html, "https://www.cian.ru/sale/flat/1/") is None


def test_removed_404_page_is_soft_failure() -> None:
    html = load("cian_404.html")
    assert classify_page(html) == "removed"
    assert parse_listing(html, "https://www.cian.ru/sale/flat/1/") is None


def test_delisted_listing_is_removed_despite_valid_offer() -> None:
    # A real delisted listing keeps its offer blob (status='draft' + archivedDate);
    # it must NOT be parsed as live even though price/id are present (#10).
    html = load("cian_removed.html")
    assert classify_page(html) == "removed"
    assert parse_listing(html, "https://www.cian.ru/sale/flat/1/") is None


def test_draft_status_flag_triggers_removed() -> None:
    html = wrap_offer(
        {"cianId": 20, "status": "draft",
         "bargainTerms": {"price": 5_000_000, "currency": "rur"}}
    )
    assert classify_page(html) == "removed"
    assert parse_listing(html) is None


def test_archived_date_triggers_removed() -> None:
    html = wrap_offer(
        {"cianId": 21, "status": "published", "archivedDate": "2025-12-26T19:32:16+03:00",
         "bargainTerms": {"price": 5_000_000, "currency": "rur"}}
    )
    assert classify_page(html) == "removed"


def test_captcha_and_removed_classify_distinctly() -> None:
    # distinct statuses so logs can tell a block from a delisting (#9/#10)
    assert classify_page(load("cian_captcha.html")) == "captcha"
    assert classify_page(load("cian_404.html")) == "removed"
    assert classify_page(load("cian_removed.html")) == "removed"
    assert classify_page(load("cian_listing_full.html")) == "ok"


# --------------------------------------------------------------------------- #
# Search parsing
# --------------------------------------------------------------------------- #
def test_parse_search_returns_listings() -> None:
    items = parse_search(load("cian_search.html"))
    assert len(items) >= 1
    for raw in items:
        assert raw.source == "cian"
        assert raw.external_id
        assert isinstance(raw.price, int) and raw.price > 0


def test_parse_search_on_captcha_returns_empty() -> None:
    assert parse_search(load("cian_captcha.html")) == []
