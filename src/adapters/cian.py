"""CIAN adapter (CLAUDE.md §6, §7). CIAN is the first/easier source.

Architecture (non-negotiable, for testability §13): FETCH is separated from
PARSE.
- The ``parse_*`` / ``classify_page`` functions are PURE: they take an already
  obtained page (HTML string) and return ``RawListing`` / ``list[RawListing]``.
  Zero I/O, deterministic, exercised offline against saved fixtures.
- ``CianAdapter.fetch_*`` obtain the HTML via Playwright and delegate to the
  pure functions. These are NOT unit-tested; they are smoke-tested by hand.

CIAN serves the listing as an embedded JSON state blob (verified against a real
page): ``window._cianConfig['frontend-offer-card']`` → the ``defaultState``
entry → ``value.offerData.offer``. Search pages use
``window._cianConfig['frontend-serp']`` → ``initialState`` →
``value.results.offers[]`` (same offer schema). We parse that blob, not the DOM.

This module never writes to the DB and never sends notifications (§6).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Literal

from src.adapters.base import RawListing

logger = logging.getLogger(__name__)

PageStatus = Literal["ok", "captcha", "removed", "not_found"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Statuses that mean a listing is live; anything else is treated as inactive.
_ACTIVE_STATUSES = {"published"}

_CAPTCHA_URL_MARKERS = ("showcaptcha", "/tmgrdfrend/")
_CAPTCHA_BODY_MARKERS = (
    "smartcaptcha",
    "checkbox-captcha",
    "вы не робот",
    "подтвердите, что запросы",
)
# A removed/archived CIAN listing serves the site's 404 page (verified).
_NOTFOUND_MARKERS = ("frontend-status-pages/404", "ошибка 404")

# Listing URL -> id. Tolerant of subdomains (www/m2/spb/...), deal/object types,
# query strings and trailing slashes.
_URL_ID_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)?cian\.ru/"
    r"(?:sale|rent)/[a-z-]+/(\d+)",
    re.IGNORECASE,
)
_URL_ID_FALLBACK_RE = re.compile(r"/(\d{5,})/?(?:[?#]|$)")


# --------------------------------------------------------------------------- #
# Embedded-state extraction
# --------------------------------------------------------------------------- #
def _extract_config_array(html: str, name: str) -> list[Any] | None:
    """Return the JSON array assigned to ``window._cianConfig['<name>']``.

    Handles both the real ``= (… || []).concat([…])`` wrapper and a bare
    ``= […]`` assignment. Returns ``None`` if absent or unparseable.
    """
    m = re.search(
        r"_cianConfig\['" + re.escape(name) + r"'\]\s*=\s*"
        r"(?:\([^)]*\)\.concat\()?\s*",
        html,
    )
    if not m:
        return None
    start = html.find("[", m.end())
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(html)):
        c = html[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _state_entry(arr: list[Any] | None, *keys: str) -> dict[str, Any] | None:
    """Pick the ``{key, value}`` entry whose key is one of ``keys``."""
    if not arr:
        return None
    for entry in arr:
        if isinstance(entry, dict) and entry.get("key") in keys:
            value = entry.get("value")
            return value if isinstance(value, dict) else None
    return None


def _is_removed(offer: dict[str, Any]) -> bool:
    """A delisted listing keeps its offer page but goes non-published.

    Verified against a real removed listing: ``status`` becomes e.g. ``'draft'``
    and an ``archivedDate`` is set (the live listing has neither).
    """
    status = offer.get("status")
    if status is not None and status not in _ACTIVE_STATUSES:
        return True
    return bool(offer.get("archivedDate"))


def classify_page(html: str, final_url: str = "") -> PageStatus:
    """Distinguish a normal page from a captcha wall vs a removed listing.

    A live listing carries the offer state blob with an active ``status``. A
    *delisted* listing keeps the blob but goes non-published (``status``/
    ``archivedDate``). A captcha wall and a bad-id 404 carry no blob at all.
    This ordering means we never mistake a page that merely *mentions* captcha
    config for a block, and never treat a delisted listing as live.
    """
    offer_state = _state_entry(
        _extract_config_array(html, "frontend-offer-card"),
        "defaultState",
        "initialState",
    )
    if offer_state is not None:
        offer = (offer_state.get("offerData") or {}).get("offer")
        if isinstance(offer, dict) and _is_removed(offer):
            return "removed"
        return "ok"
    if _extract_config_array(html, "frontend-serp") is not None:
        return "ok"  # a search results page
    low = html.lower()
    url_low = (final_url or "").lower()
    if any(m in url_low for m in _CAPTCHA_URL_MARKERS) or any(
        m in low for m in _CAPTCHA_BODY_MARKERS
    ):
        return "captcha"
    if any(m in low for m in _NOTFOUND_MARKERS):
        return "removed"
    return "not_found"


# --------------------------------------------------------------------------- #
# Field helpers (defensive: never raise on a missing/odd optional)
# --------------------------------------------------------------------------- #
def _to_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", ".").strip())
        except ValueError:
            return None
    return None


def _to_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        digits = re.sub(r"[^\d]", "", v)
        return int(digits) if digits else None
    return None


def external_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _URL_ID_RE.search(url)
    if m:
        return m.group(1)
    m = _URL_ID_FALLBACK_RE.search(url)
    return m.group(1) if m else None


def classify_url(url: str | None) -> str:
    """Guess whether a CIAN URL is a single ``'listing'`` or a ``'search'``.

    A URL that carries a numeric offer id is a listing; anything else (a catalog
    / saved-search page) is treated as a search. Site-specific, so it lives in
    the adapter (§15).
    """
    return "listing" if external_id_from_url(url) else "search"


def _extract_external_id(offer: dict[str, Any], url: str | None) -> str | None:
    pid = offer.get("cianId") or offer.get("id")
    payload_id = str(pid) if pid is not None else None
    url_id = external_id_from_url(url)
    if payload_id and url_id and payload_id != url_id:
        logger.warning(
            "external_id mismatch (payload=%s url=%s) — trusting payload",
            payload_id,
            url_id,
        )
    return payload_id or url_id


def _extract_price(offer: dict[str, Any]) -> int | None:
    """Integer rubles, or ``None`` for unsupported (range/``от N``/non-RUB).

    Data comes from structured JSON, so spaces/nbsp/₽/``торг`` are non-issues:
    ``price`` is already numeric. A range/newbuilding ``от`` price has no single
    value (``price`` absent or non-scalar) → unsupported.
    """
    bt = offer.get("bargainTerms") or {}
    price = bt.get("price")
    currency = str(bt.get("currency") or "").lower()

    ptr = offer.get("priceTotalRur")
    if isinstance(ptr, (int, float)) and not isinstance(ptr, bool) and ptr > 0:
        return int(ptr)
    if (
        currency in ("rur", "rub")
        and isinstance(price, (int, float))
        and not isinstance(price, bool)
        and price > 0
    ):
        return int(price)
    return None


def _extract_rooms(offer: dict[str, Any], extra: dict[str, Any]) -> int | None:
    """studio → 0; free layout → None (+note); else the room count."""
    flat = str(offer.get("flatType") or "").lower()
    if flat == "studio":
        return 0
    if flat in ("openplan", "freeplan"):
        extra["layout"] = "free"
        return None
    return _to_int(offer.get("roomsCount"))


def _extract_address(offer: dict[str, Any]) -> str | None:
    geo = offer.get("geo") or {}
    addr = geo.get("address")
    if isinstance(addr, list):
        parts = [p.get("name") for p in addr if isinstance(p, dict) and p.get("name")]
        if parts:
            return ", ".join(parts)
    ui = geo.get("userInput")
    return ui if isinstance(ui, str) else None


def _extract_title(html: str | None) -> str | None:
    if not html:
        return None
    m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = re.split(r"\s*[-–]\s*база ЦИАН", m.group(1), maxsplit=1)[0].strip()
    return title or None


def _offer_url(offer: dict[str, Any]) -> str | None:
    url = offer.get("fullUrl")
    if isinstance(url, str) and url:
        return url
    pid = offer.get("cianId") or offer.get("id")
    deal = offer.get("dealType") or "sale"
    otype = offer.get("offerType") or "flat"
    return f"https://www.cian.ru/{deal}/{otype}/{pid}/" if pid else None


def _offer_to_raw(
    offer: dict[str, Any], url: str | None, html: str | None = None
) -> RawListing | None:
    """Map one CIAN ``offer`` dict to a :class:`RawListing`, or ``None``.

    Hard failures for this item (skip, per §6): no ``external_id``, no usable
    ``price``. Everything else degrades to ``None``.
    """
    external_id = _extract_external_id(offer, url)
    if not external_id:
        logger.warning("skipping CIAN offer with no external_id (url=%s)", url)
        return None
    price = _extract_price(offer)
    if price is None:
        logger.info(
            "skipping CIAN offer %s: no single/RUB price (range/newbuilding?)",
            external_id,
        )
        return None

    extra: dict[str, Any] = {}
    rooms = _extract_rooms(offer, extra)

    living = _to_float(offer.get("livingArea"))
    kitchen = _to_float(offer.get("kitchenArea"))
    if living is not None:
        extra["area_living"] = living
    if kitchen is not None:
        extra["area_kitchen"] = kitchen

    bt = offer.get("bargainTerms") or {}
    if offer.get("dealType") == "rent" and bt.get("paymentPeriod"):
        extra["rent_period"] = bt.get("paymentPeriod")
    if offer.get("isApartments"):
        extra["is_apartments"] = True

    status = offer.get("status")
    if status is not None and status not in _ACTIVE_STATUSES:
        extra["status"] = status  # informational; 404 is the real removal signal

    # Trimmed debug snapshot so normalize can fill listings.raw_json (#14).
    extra["raw"] = {
        "cianId": offer.get("cianId"),
        "status": status,
        "dealType": offer.get("dealType"),
        "flatType": offer.get("flatType"),
        "totalArea": offer.get("totalArea"),
        "price": price,
    }

    return RawListing(
        source="cian",
        external_id=external_id,
        url=url or _offer_url(offer) or "",
        price=price,
        title=_extract_title(html),
        address=_extract_address(offer),
        rooms=rooms,
        area_total=_to_float(offer.get("totalArea")),
        floor=_to_int(offer.get("floorNumber")),
        floors_total=_to_int((offer.get("building") or {}).get("floorsCount")),
        extra=extra,
    )


# --------------------------------------------------------------------------- #
# Pure parse entry points (fixture-tested)
# --------------------------------------------------------------------------- #
def parse_listing(html: str, url: str | None = None) -> RawListing | None:
    """Parse a single CIAN listing page. Soft failures return ``None``."""
    status = classify_page(html)
    if status == "captcha":
        logger.warning("CIAN blocked (captcha/anti-bot) for %s", url)
        return None
    if status == "removed":
        logger.info("CIAN listing removed/404: %s", url)
        return None
    if status != "ok":
        logger.warning("CIAN page not recognized (no state blob): %s", url)
        return None

    state = _state_entry(
        _extract_config_array(html, "frontend-offer-card"),
        "defaultState",
        "initialState",
    )
    offer = ((state or {}).get("offerData") or {}).get("offer")
    if not isinstance(offer, dict):
        logger.info("no single offer (unsupported url type, e.g. ЖК): %s", url)
        return None
    return _offer_to_raw(offer, url, html)


def parse_search(html: str) -> list[RawListing]:
    """Parse a CIAN search page's embedded offers directly (no click-through)."""
    if classify_page(html) != "ok":
        logger.warning("CIAN search page soft-failed (blocked/removed)")
        return []
    state = _state_entry(
        _extract_config_array(html, "frontend-serp"), "initialState", "defaultState"
    )
    offers = ((state or {}).get("results") or {}).get("offers") or []
    out: list[RawListing] = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        raw = _offer_to_raw(offer, _offer_url(offer), html=None)
        if raw is not None:
            out.append(raw)
    return out


# --------------------------------------------------------------------------- #
# Thin Playwright fetch layer (NOT unit-tested; smoke-tested by hand)
# --------------------------------------------------------------------------- #
class CianAdapter:
    """SiteAdapter for CIAN. One lazy persistent browser context per instance."""

    source = "cian"

    def __init__(self, settings: Any = None) -> None:
        if settings is None:
            from config import settings as default_settings

            settings = default_settings
        self.settings = settings
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None

    # -- lifecycle --------------------------------------------------------- #
    def _ensure_context(self) -> None:
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": True}
        if self.settings.proxy_url:
            launch_kwargs["proxy"] = {"server": self.settings.proxy_url}
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context(
            locale="ru-RU",
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
        )
        if self.settings.block_media:
            self._context.route("**/*", self._maybe_block)

    @staticmethod
    def _maybe_block(route: Any) -> None:
        if route.request.resource_type in {"image", "media", "font"}:
            route.abort()
        else:
            route.continue_()

    def close(self) -> None:
        for obj, stop in ((self._context, "close"), (self._browser, "close")):
            try:
                if obj is not None:
                    getattr(obj, stop)()
            except Exception:  # best-effort teardown
                logger.debug("error closing browser object", exc_info=True)
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                logger.debug("error stopping playwright", exc_info=True)
        self._context = self._browser = self._pw = None

    def __enter__(self) -> "CianAdapter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- fetching ---------------------------------------------------------- #
    def _sleep(self) -> None:
        lo, hi = self.settings.scrape_delay_min_sec, self.settings.scrape_delay_max_sec
        time.sleep(random.uniform(lo, hi))

    def fetch_raw(self, url: str) -> tuple[str | None, str]:
        """Navigate and return ``(html, final_url)``; ``(None, url)`` on timeout."""
        from playwright.sync_api import TimeoutError as PWTimeout

        self._ensure_context()
        self._sleep()
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                pass
            return page.content(), page.url
        except PWTimeout:
            logger.warning("navigation timeout for %s", url)
            return None, url
        finally:
            page.close()

    def fetch_listing(self, url: str) -> RawListing | None:
        html, final_url = self.fetch_raw(url)
        if html is None:
            return None
        if classify_page(html, final_url) == "captcha":
            logger.warning("CIAN blocked (captcha) while fetching %s", url)
            return None
        return parse_listing(html, url)

    def fetch_search(self, url: str) -> list[RawListing]:
        results: list[RawListing] = []
        pages = max(1, int(self.settings.max_search_pages))
        for page_no in range(1, pages + 1):
            page_url = url if page_no == 1 else _with_page(url, page_no)
            html, final_url = self.fetch_raw(page_url)
            if html is None:
                break
            if classify_page(html, final_url) == "captcha":
                logger.warning("CIAN blocked (captcha) while fetching search %s", page_url)
                break
            batch = parse_search(html)
            if not batch:
                break
            results.extend(batch)
        return results


# --------------------------------------------------------------------------- #
# PII redaction for hand-captured fixtures (site-specific: knows where CIAN
# stores contact data). Kept in the adapter so §15 stays intact.
# --------------------------------------------------------------------------- #
_PHONES_ARRAY_RE = re.compile(r'"phones"\s*:\s*\[[^\]]*\]')
_PHONE_NUMBER_RE = re.compile(
    r"(?:\+?7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
)
_CONTACT_NAME_RE = re.compile(
    r'"(agentName|userName|firstName|lastName|fullName|name)"\s*:\s*"[^"]*"'
)


def redact(html: str) -> str:
    """Mask seller/agent PII in a raw CIAN payload before it touches disk (§14).

    Conservative and public-data-preserving: property fields (price, area,
    address, rooms) are untouched; only contact PII is masked. Best-effort —
    always eyeball the output before committing a fixture.
    """
    html = _PHONES_ARRAY_RE.sub('"phones":[]', html)
    html = _PHONE_NUMBER_RE.sub("+7XXXXXXXXXX", html)
    html = _CONTACT_NAME_RE.sub(lambda m: f'"{m.group(1)}":"REDACTED"', html)
    return html


def _with_page(url: str, page_no: int) -> str:
    sep = "&" if "?" in url else "?"
    if re.search(r"[?&]p=\d+", url):
        return re.sub(r"([?&]p=)\d+", lambda m: f"{m.group(1)}{page_no}", url)
    return f"{url}{sep}p={page_no}"
