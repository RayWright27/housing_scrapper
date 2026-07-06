"""Avito adapter (CLAUDE.md §6, §7). Avito is the LAST/hardest source.

Architecture mirrors ``cian.py`` exactly: FETCH is separated from PARSE for
testability (§13). The ``parse_*`` / ``classify_page`` functions are PURE — they
take an already-obtained page and return ``RawListing`` / ``list[RawListing]``,
exercised offline against fixtures. ``AvitoAdapter.fetch_*`` obtain the HTML via
**sync** Playwright and delegate; they are smoke-tested by hand.

Avito embeds state in two places (verified against real pages):
- a single **listing** in ``window.__staticRouterHydrationData = JSON.parse(…)``
  → ``loaderData[<route>]`` → ``buyerItem.item`` (+ ``paramsBlock.items``);
- **search** results in ``<script type="mime/invalid" data-mfe-state="true">``
  → ``loaderData.data.catalog.items[]``.
A removed listing's loader is ``{type,message,isGone}`` with no ``buyerItem``.

This module never writes to the DB and never sends notifications (§6). Per §7 it
does not solve captchas or defeat the firewall: a block soft-fails (None/[]).
"""

from __future__ import annotations

import html as htmlmod
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

# Avito's firewall/anti-bot wall (verified: tiny page, GeeTest, no state blob).
_FIREWALL_MARKERS = (
    "доступ ограничен",
    "firewall",
    "geetest",
    "подтвердите, что вы не робот",
    "ip-адрес",
)
_FIREWALL_URL_MARKERS = ("/blocked", "geetest")

# Loader "gone" signals for a delisted listing (no buyerItem present).
_GONE_TYPES = {"notfound", "gone", "closed"}

# Param labels on a listing page (structured, preferred over the title).
_P_ROOMS = "Количество комнат"
_P_AREA = "Общая площадь"
_P_LIVING = "Жилая площадь"
_P_KITCHEN = "Площадь кухни"
_P_FLOOR = "Этаж"


# --------------------------------------------------------------------------- #
# Embedded-state extraction
# --------------------------------------------------------------------------- #
def _load_hydration_loader(html: str) -> dict[str, Any] | None:
    """Return the item/route loader from ``window.__staticRouterHydrationData``.

    That value is ``JSON.parse("<escaped JSON>")``; we decode the JS string then
    the inner JSON, and pick the loaderData entry that carries an item or a
    gone-marker (falling back to the first dict entry, e.g. a search shell).
    """
    m = re.search(r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(", html)
    if not m:
        return None
    try:
        inner, _ = json.JSONDecoder().raw_decode(html, m.end())
        data = json.loads(inner)
    except (ValueError, json.JSONDecodeError):
        return None
    loader_data = data.get("loaderData") if isinstance(data, dict) else None
    if not isinstance(loader_data, dict):
        return None
    for value in loader_data.values():
        if isinstance(value, dict) and (
            "buyerItem" in value or "isGone" in value or "message" in value
        ):
            return value
    for value in loader_data.values():
        if isinstance(value, dict):
            return value
    return None


def _load_serp_catalog(html: str) -> dict[str, Any] | None:
    """Return ``loaderData.data.catalog`` from the search MFE-state script."""
    for m in re.finditer(r'data-mfe-state="true"\s*>', html):
        try:
            obj, _ = json.JSONDecoder().raw_decode(html, m.end())
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        data = (obj.get("loaderData") or {}).get("data")
        catalog = (data or {}).get("catalog") if isinstance(data, dict) else None
        if isinstance(catalog, dict) and isinstance(catalog.get("items"), list):
            return catalog
    return None


def _is_removed_item(item: dict[str, Any]) -> bool:
    return item.get("isClosed") is True or item.get("isActive") is False


def _is_gone_loader(loader: dict[str, Any]) -> bool:
    if "buyerItem" in loader:
        return False
    if loader.get("isGone") or loader.get("message"):
        return True
    return str(loader.get("type") or "").lower() in _GONE_TYPES


def classify_page(html: str, final_url: str = "") -> PageStatus:
    """Normal page vs firewall/captcha vs removed listing (distinct logging)."""
    loader = _load_hydration_loader(html)
    if loader is not None:
        item = (loader.get("buyerItem") or {}).get("item")
        if isinstance(item, dict):
            return "removed" if _is_removed_item(item) else "ok"
        if _is_gone_loader(loader):
            return "removed"
        # else: a search shell whose items live in the MFE-state script below
    if _load_serp_catalog(html) is not None:
        return "ok"
    low = html.lower()
    url_low = (final_url or "").lower()
    if any(m in low for m in _FIREWALL_MARKERS) or any(
        m in url_low for m in _FIREWALL_URL_MARKERS
    ):
        return "captcha"
    return "not_found"


# --------------------------------------------------------------------------- #
# Field helpers (defensive: never raise on a missing/odd optional)
# --------------------------------------------------------------------------- #
def _clean(value: Any) -> str | None:
    """HTML-unescape, collapse whitespace incl. nbsp, strip → text or None."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", htmlmod.unescape(value).replace("\xa0", " ")).strip()
    return text or None


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


def _area_from(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*м²", text)
    return float(m.group(1).replace(",", ".")) if m else None


def _floor_from(text: str | None) -> tuple[int | None, int | None]:
    if not text:
        return None, None
    m = re.search(r"(\d+)\s*(?:/|из)\s*(\d+)\s*эт", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def external_id_from_url(url: str | None) -> str | None:
    """The trailing numeric id of an Avito listing path (ignores the query)."""
    if not url:
        return None
    path = url.split("?", 1)[0].split("#", 1)[0]
    ids = re.findall(r"_(\d{6,})(?:/|$)", path)
    return ids[-1] if ids else None


def classify_url(url: str | None) -> str:
    """listing if the URL carries a numeric id, else search (mirrors CIAN)."""
    return "listing" if external_id_from_url(url) else "search"


def _parse_params(items: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in items or []:
        if isinstance(p, dict):
            label = _clean(p.get("title"))
            value = _clean(p.get("description"))
            if label and value:
                out[label] = value
    return out


def _resolve_rooms(params: dict[str, str], title: str | None,
                   extra: dict[str, Any]) -> int | None:
    """studio → 0; free layout → None(+note); apartments/N-к → count."""
    raw = params.get(_P_ROOMS, "")
    text = f"{raw} {title or ''}".lower()
    if "студи" in text:
        return 0
    if "своб" in text:  # свободная / своб. планировка
        extra["layout"] = "free"
        return None
    if raw:
        return _to_int(raw)
    m = re.search(r"(\d+)-к", title.lower()) if title else None
    return int(m.group(1)) if m else None


def _extract_external_id(item: dict[str, Any], url: str | None) -> str | None:
    pid = item.get("id")
    payload_id = str(pid) if pid is not None else None
    url_id = external_id_from_url(url)
    if payload_id and url_id and payload_id != url_id:
        logger.warning("external_id mismatch (payload=%s url=%s) — trusting payload",
                       payload_id, url_id)
    return payload_id or url_id


def _item_to_raw(
    item: dict[str, Any], params_items: Any, url: str | None, *, from_search: bool
) -> RawListing | None:
    """Map one Avito item (listing ``buyerItem.item`` or a SERP item) to Raw."""
    if from_search:
        up = item.get("urlPath")
        item_url = ("https://www.avito.ru" + up) if isinstance(up, str) else (url or "")
        price = _to_int((item.get("priceDetailed") or {}).get("value"))
        postfix = _clean((item.get("priceDetailed") or {}).get("postfix"))
    else:
        item_url = url or ""
        price = _to_int(item.get("price"))
        postfix = None

    external_id = _extract_external_id(item, item_url)
    if not external_id:
        logger.warning("skipping Avito item with no external_id (url=%s)", url)
        return None
    if price is None or price <= 0:
        logger.info("skipping Avito item %s: no usable price (по запросу?)", external_id)
        return None

    extra: dict[str, Any] = {}
    params = _parse_params(params_items)
    title = _clean(item.get("title"))

    rooms = _resolve_rooms(params, title, extra)
    area_total = _area_from(params.get(_P_AREA)) or _area_from(title)
    living = _area_from(params.get(_P_LIVING))
    kitchen = _area_from(params.get(_P_KITCHEN))
    if living is not None:
        extra["area_living"] = living
    if kitchen is not None:
        extra["area_kitchen"] = kitchen
    floor, floors_total = _floor_from(params.get(_P_FLOOR))
    if floor is None:
        floor, floors_total = _floor_from(title)

    address = _clean(item.get("address")) or _clean((item.get("geo") or {}).get("address"))

    if postfix and "мес" in postfix.lower():
        extra["rent_period"] = "monthly"
    elif not from_search and "/arenda" in (url or "").lower():
        extra["rent_period"] = "monthly"

    extra["raw"] = {
        "id": item.get("id"),
        "price": price,
        "title": title,
        "address": address,
        "isActive": item.get("isActive"),
    }

    return RawListing(
        source="avito",
        external_id=external_id,
        url=item_url,
        price=price,
        title=title,
        address=address,
        rooms=rooms,
        area_total=area_total,
        floor=floor,
        floors_total=floors_total,
        extra=extra,
    )


# --------------------------------------------------------------------------- #
# Pure parse entry points (fixture-tested)
# --------------------------------------------------------------------------- #
def parse_listing(html: str, url: str | None = None) -> RawListing | None:
    """Parse a single Avito listing page. Soft failures return ``None``."""
    status = classify_page(html)
    if status == "captcha":
        logger.warning("Avito blocked (firewall/captcha) for %s", url)
        return None
    if status == "removed":
        logger.info("Avito listing removed/closed: %s", url)
        return None
    if status != "ok":
        logger.warning("Avito page not recognized (no state blob): %s", url)
        return None

    loader = _load_hydration_loader(html)
    buyer_item = (loader or {}).get("buyerItem") or {}
    item = buyer_item.get("item")
    if not isinstance(item, dict):
        logger.info("no buyerItem (unsupported url type, e.g. profile): %s", url)
        return None
    params_items = (buyer_item.get("paramsBlock") or {}).get("items", [])
    return _item_to_raw(item, params_items, url, from_search=False)


def parse_search(html: str) -> list[RawListing]:
    """Parse an Avito search page's embedded items directly (no click-through)."""
    if classify_page(html) != "ok":
        logger.warning("Avito search page soft-failed (blocked/removed)")
        return []
    catalog = _load_serp_catalog(html)
    items = (catalog or {}).get("items") or []
    out: list[RawListing] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = _item_to_raw(item, [], None, from_search=True)
        if raw is not None:
            out.append(raw)
    return out


# --------------------------------------------------------------------------- #
# Thin sync-Playwright fetch layer (NOT unit-tested; smoke-tested by hand)
# --------------------------------------------------------------------------- #
class AvitoAdapter:
    """SiteAdapter for Avito. One lazy persistent browser context per instance."""

    source = "avito"

    def __init__(self, settings: Any = None) -> None:
        if settings is None:
            from config import settings as default_settings

            settings = default_settings
        self.settings = settings
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None

    def _ensure_context(self) -> None:
        if self._context is not None:
            return
        from src.adapters.browser import launch_context

        self._pw, self._browser, self._context = launch_context(self.settings, USER_AGENT)

    def close(self) -> None:
        from src.adapters.browser import close_context

        close_context(self._pw, self._browser, self._context)
        self._context = self._browser = self._pw = None

    def __enter__(self) -> "AvitoAdapter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

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
            logger.warning("Avito blocked (firewall/captcha) while fetching %s", url)
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
                logger.warning("Avito blocked (firewall) while fetching search %s", page_url)
                break
            batch = parse_search(html)
            if not batch:
                break
            results.extend(batch)
        return results


# --------------------------------------------------------------------------- #
# PII redaction for hand-captured fixtures (site-specific; §14/§15).
# --------------------------------------------------------------------------- #
_PHONE_NUMBER_RE = re.compile(
    r"(?:\+?7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
)
_SELLER_NAME_RE = re.compile(
    r'"(name|managerName|firstName|lastName|contactName)"\s*:\s*"[^"]*"'
)
_CONTACTS_RE = re.compile(r'"contacts"\s*:\s*\[[^\]]*\]')


def redact(html: str) -> str:
    """Mask seller PII before a raw Avito payload touches disk (§14).

    Public property data (price, area, address, rooms) is untouched; only
    contact PII is masked. Best-effort — eyeball the output before committing.
    """
    html = _CONTACTS_RE.sub('"contacts":[]', html)
    html = _PHONE_NUMBER_RE.sub("+7XXXXXXXXXX", html)
    html = _SELLER_NAME_RE.sub(lambda m: f'"{m.group(1)}":"REDACTED"', html)
    return html


def _with_page(url: str, page_no: int) -> str:
    sep = "&" if "?" in url else "?"
    if re.search(r"[?&]p=\d+", url):
        return re.sub(r"([?&]p=)\d+", lambda m: f"{m.group(1)}{page_no}", url)
    return f"{url}{sep}p={page_no}"
