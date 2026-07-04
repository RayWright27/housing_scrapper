"""Offline Avito parse tests against sanitized real fixtures. No network."""

from __future__ import annotations

from pathlib import Path

from src.adapters.avito import (
    classify_page,
    classify_url,
    external_id_from_url,
    parse_listing,
    parse_search,
)

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def test_external_id_from_url() -> None:
    assert external_id_from_url(
        "https://www.avito.ru/sankt-peterburg_pushkin/kvartiry/"
        "2-k._kvartira_437_m_59_et._8223980209?context=abc") == "8223980209"
    assert external_id_from_url(
        "https://www.avito.ru/spb/kvartiry/2-k._kvartira_484_m_39_et._8234179450") == "8234179450"
    # a search URL has no trailing numeric id
    assert external_id_from_url(
        "https://www.avito.ru/sankt-peterburg/kvartiry/prodam/studii-ASgBAgIC~WA?context=x") is None


def test_classify_url() -> None:
    assert classify_url("https://www.avito.ru/spb/kvartiry/2-k._..._8223980209") == "listing"
    assert classify_url("https://www.avito.ru/sankt-peterburg/kvartiry/prodam") == "search"


# --------------------------------------------------------------------------- #
# listing parsing
# --------------------------------------------------------------------------- #
def test_full_listing_maps_all_fields() -> None:
    url = "https://www.avito.ru/spb/kvartiry/2-k._kvartira_437_m_59_et._8223980209"
    raw = parse_listing(load("avito_listing_full.html"), url)
    assert raw is not None
    assert raw.source == "avito" and raw.external_id == "8223980209"
    assert raw.price == 7_395_000            # integer rubles
    assert raw.rooms == 2
    assert raw.area_total == 43.7
    assert raw.floor == 5 and raw.floors_total == 9
    assert raw.extra["area_living"] == 31.7  # from structured params -> extra
    assert raw.extra["area_kitchen"] == 5.7
    assert "Пушкин" in (raw.address or "")
    assert raw.extra["raw"]["id"] == 8223980209


def test_missing_optionals_become_none() -> None:
    raw = parse_listing(load("avito_listing_min.html"),
                        "https://www.avito.ru/spb/kvartiry/x_8223980209")
    assert raw is not None
    assert raw.price == 7_395_000
    assert raw.rooms is None and raw.area_total is None
    assert raw.floor is None and raw.floors_total is None
    assert "area_living" not in raw.extra


def test_studio_maps_to_zero_rooms() -> None:
    raw = parse_listing(load("avito_studio.html"),
                        "https://www.avito.ru/spb/kvartiry/studiya_7534690077")
    assert raw is not None
    assert raw.rooms == 0
    assert raw.area_total == 20.4
    assert raw.floor == 19 and raw.floors_total == 25


def test_removed_listing_is_soft_failure() -> None:
    html = load("avito_removed.html")
    assert classify_page(html) == "removed"
    assert parse_listing(html, "https://www.avito.ru/spb/kvartiry/x_8234179450") is None


def test_firewall_page_is_soft_failure() -> None:
    html = load("avito_captcha.html")
    assert classify_page(html) == "captcha"
    assert parse_listing(html, "https://www.avito.ru/spb/kvartiry/x_1") is None
    assert parse_search(html) == []


def test_captcha_and_removed_classify_distinctly() -> None:
    assert classify_page(load("avito_captcha.html")) == "captcha"
    assert classify_page(load("avito_removed.html")) == "removed"
    assert classify_page(load("avito_listing_full.html")) == "ok"
    assert classify_page(load("avito_search.html")) == "ok"


# --------------------------------------------------------------------------- #
# search parsing
# --------------------------------------------------------------------------- #
def test_search_parses_items_with_price_and_url() -> None:
    items = parse_search(load("avito_search.html"))
    assert len(items) == 3
    first = items[0]
    assert first.source == "avito" and first.external_id == "7534690077"
    assert first.price == 4_028_184                    # priceDetailed.value (int)
    assert first.rooms == 0                             # studio in title
    assert first.area_total == 20.4
    assert first.url.startswith("https://www.avito.ru/")  # built from urlPath
    # every parsed item has a positive integer price and an id
    assert all(i.price > 0 and i.external_id for i in items)


def test_missing_price_or_id_items_are_skipped() -> None:
    # a search blob containing an ad slot (no id/price) yields nothing for it
    from src.adapters.avito import _load_serp_catalog  # noqa: F401 (ensure importable)
    html = ('<html><body><script type="mime/invalid" data-mfe-state="true">'
            '{"loaderData":{"data":{"catalog":{"items":['
            '{"id":111222333,"title":"1-к. квартира, 30 м², 2/5 эт.",'
            ' "urlPath":"/spb/kvartiry/x_111222333","priceDetailed":{"value":5000000,"hasValue":true}},'
            '{"title":"Реклама"},'
            '{"id":444555666,"priceDetailed":{"hasValue":false}}'
            ']}}}}</script></body></html>')
    items = parse_search(html)
    assert len(items) == 1 and items[0].external_id == "111222333"
