"""Dashboard read-model: derived fields computed on read (CLAUDE.md §9a, §15).

Everything here is computed from ``listings`` + ``price_history`` at request
time — ₽/m², Δ-total %, days-since-change, min/max, change count, summary
counts. NONE of these are stored columns (§15). The web layer stays a thin
consumer: it reads state and derives display values; it never scrapes and its
only writes (elsewhere) are add/deactivate on ``tracked_sources``.

Pure helpers take plain observation lists so they are unit-testable on
fabricated rows with no DB.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from src.storage import repository as repo


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# source detection (host -> 'cian' | 'avito' | None)
# --------------------------------------------------------------------------- #
def detect_source(url: str) -> str | None:
    """Map a URL host to a known source, or ``None`` for an unknown host."""
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("cian.ru"):
        return "cian"
    if host.endswith("avito.ru"):
        return "avito"
    return None


# --------------------------------------------------------------------------- #
# pure numeric helpers (observations = ascending list of {price, observed_at})
# --------------------------------------------------------------------------- #
def price_per_m2(price: int | None, area_total: float | None) -> int | None:
    if price is None or not area_total or area_total <= 0:
        return None
    return round(price / area_total)


def delta_total_pct(first: int | None, current: int | None) -> float | None:
    """Percent change from the first observed price to the current one (1 dp)."""
    if first is None or current is None or first == 0:
        return None
    return round((current - first) / first * 100, 1)


def days_since_change(observations: list[dict], now: str) -> int | None:
    """Whole days since the last price CHANGE.

    A lone first observation is a baseline, not a change → ``None`` (also the
    natural value for a delisted object that never moved).
    """
    if len(observations) < 2:
        return None
    last = datetime.fromisoformat(observations[-1]["observed_at"])
    delta = datetime.fromisoformat(now) - last
    return max(0, delta.days)


def min_max(observations: list[dict]) -> tuple[int | None, int | None]:
    if not observations:
        return (None, None)
    prices = [o["price"] for o in observations]
    return (min(prices), max(prices))


def change_count(observations: list[dict]) -> int:
    """Number of price changes = observations after the first (append-only)."""
    return max(0, len(observations) - 1)


# --------------------------------------------------------------------------- #
# DB reads
# --------------------------------------------------------------------------- #
def _observations(conn: sqlite3.Connection, listing_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT price, observed_at FROM price_history WHERE listing_id = ? "
        "ORDER BY observed_at ASC, id ASC",
        (listing_id,),
    ).fetchall()
    return [{"price": int(r["price"]), "observed_at": r["observed_at"]} for r in rows]


def _primary_source(conn: sqlite3.Connection, listing_id: int) -> sqlite3.Row | None:
    """The tracked_source used for this object's note and its remove (×).

    Prefers a still-linked, pinned ('listing') source so removing unpins just
    this object; falls back to the search that discovered it (removing that
    stops the whole search — the client warns about this).
    """
    return conn.execute(
        "SELECT ts.id AS id, ts.kind AS kind, ts.note AS note "
        "FROM source_listings sl JOIN tracked_sources ts ON ts.id = sl.tracked_source_id "
        "WHERE sl.listing_id = ? "
        "ORDER BY sl.is_linked DESC, (ts.kind = 'listing') DESC, ts.id LIMIT 1",
        (listing_id,),
    ).fetchone()


def listing_rows(conn: sqlite3.Connection, now: str | None = None) -> list[dict]:
    """All discovered objects with their computed display fields (§9a)."""
    now = now or _now_iso()
    out: list[dict] = []
    for listing in repo.list_listings(conn):
        obs = _observations(conn, listing["id"])
        first = obs[0]["price"] if obs else None
        current = obs[-1]["price"] if obs else None
        low, high = min_max(obs)
        src = _primary_source(conn, listing["id"])
        out.append({
            "id": listing["id"],
            "source": listing["source"],
            "external_id": listing["external_id"],
            "url": listing["url"],
            "title": listing["title"],
            "address": listing["address"],
            "rooms": listing["rooms"],
            "area_total": listing["area_total"],
            "floor": listing["floor"],
            "floors_total": listing["floors_total"],
            "note": src["note"] if src else None,
            "tracked_source_id": src["id"] if src else None,
            "tracked_source_kind": src["kind"] if src else None,
            "status": "active" if listing["is_active"] else "delisted",
            "current_price": current,
            "price_per_m2": price_per_m2(current, listing["area_total"]),
            "delta_total_pct": delta_total_pct(first, current),
            "days_since_change": days_since_change(obs, now),
            "min_price": low,
            "max_price": high,
            "change_count": change_count(obs),
            "first_seen_at": listing["first_seen_at"],
            "last_seen_at": listing["last_seen_at"],
        })
    return out


def summary(conn: sqlite3.Connection, now: str | None = None) -> dict:
    """Metric-strip numbers: total / active / delisted / price changes in 7d."""
    now = now or _now_iso()
    total = conn.execute("SELECT COUNT(*) AS c FROM listings").fetchone()["c"]
    active = conn.execute(
        "SELECT COUNT(*) AS c FROM listings WHERE is_active = 1"
    ).fetchone()["c"]
    cutoff = (datetime.fromisoformat(now) - timedelta(days=7)).isoformat()
    rows_in_window = conn.execute(
        "SELECT COUNT(*) AS c FROM price_history WHERE observed_at >= ?", (cutoff,)
    ).fetchone()["c"]
    # A listing's earliest observation is a first-seen, not a change. Subtract
    # those that fall inside the window so only genuine changes are counted.
    firsts_in_window = conn.execute(
        "SELECT COUNT(*) AS c FROM ("
        "  SELECT listing_id, MIN(observed_at) AS first_at FROM price_history "
        "  GROUP BY listing_id"
        ") WHERE first_at >= ?",
        (cutoff,),
    ).fetchone()["c"]
    return {
        "total": int(total),
        "active": int(active),
        "delisted": int(total) - int(active),
        "changes_7d": max(0, int(rows_in_window) - int(firsts_in_window)),
    }


def history(
    conn: sqlite3.Connection, listing_id: int, range_: str = "all",
    now: str | None = None,
) -> dict | None:
    """Observations for the chart, plus area_total for client ₽/m² (§9a #4).

    For a bounded range the last observation *before* the window is prepended as
    an anchor so a flat price whose last change predates the window still renders
    as a correct step line.
    """
    now = now or _now_iso()
    listing = repo.get_listing(conn, listing_id)
    if listing is None:
        return None
    obs = _observations(conn, listing_id)
    days = {"90d": 90, "30d": 30}.get(range_)
    if days is not None:
        cutoff = (datetime.fromisoformat(now) - timedelta(days=days)).isoformat()
        in_window = [o for o in obs if o["observed_at"] >= cutoff]
        before = [o for o in obs if o["observed_at"] < cutoff]
        obs = ([before[-1]] + in_window) if before else in_window
    return {
        "listing_id": listing_id,
        "area_total": listing["area_total"],
        "observations": obs,
    }
