"""Turn a site-specific :class:`RawListing` into the site-agnostic ``Listing``
metadata that maps to the ``listings`` table (CLAUDE.md §2, §5, §6).

This is the boundary where site-specific knowledge ends. Everything downstream
of ``normalize`` works on :class:`Listing` and must never reference anything
CIAN- or Avito-specific. The mapping is pure (no I/O) and defensive: a missing
optional field becomes ``None``, never an exception. Only a missing identity
(``external_id``) is a hard error — a listing with no identity cannot be stored.

Price is deliberately absent here: it is carried on the ``RawListing`` and is a
later phase's concern (it lives in ``price_history``, not ``listings``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from src.adapters.base import RawListing


class NormalizationError(ValueError):
    """Raised when a :class:`RawListing` cannot be normalized (e.g. no id)."""


@dataclass
class Listing:
    """Site-agnostic stored metadata for one real-estate object.

    Maps 1:1 to the durable columns of the ``listings`` table. Intentionally
    excludes:
    - ``price`` — lives in ``price_history``, not here;
    - derived values (₽/m², deltas) — computed on read, never stored;
    - DB-owned fields (``id``, ``first_seen_at``, ``last_seen_at``,
      ``is_active``) — the repository manages those.
    """

    source: str
    external_id: str
    url: str
    title: str | None = None
    address: str | None = None
    rooms: int | None = None
    area_total: float | None = None
    area_living: float | None = None
    area_kitchen: float | None = None
    floor: int | None = None
    floors_total: int | None = None
    raw_json: dict[str, Any] | None = None


def _resolve_rooms(raw: RawListing, extra: dict[str, Any]) -> int | None:
    """Map room count, treating studios as ``0`` (CLAUDE.md §5).

    A studio may arrive either as ``rooms == 0`` (passed through) or as an
    ``is_studio`` flag in ``extra``. Neither path is site-specific.
    """
    if extra.get("is_studio"):
        return 0
    return raw.rooms


def normalize(raw: RawListing) -> Listing:
    """Map a :class:`RawListing` to a :class:`Listing`. Pure and defensive.

    Raises :class:`NormalizationError` if ``external_id`` is missing/empty.
    ``area_living`` / ``area_kitchen`` are not on ``RawListing``; they are read
    from ``raw.extra`` when present, else ``None``.
    """
    if not raw.external_id:
        raise NormalizationError(
            f"cannot normalize listing without external_id (source={raw.source!r}, "
            f"url={raw.url!r})"
        )

    extra = raw.extra or {}

    return Listing(
        source=raw.source,
        external_id=raw.external_id,
        url=raw.url,
        title=raw.title,
        address=raw.address,
        rooms=_resolve_rooms(raw, extra),
        area_total=raw.area_total,
        area_living=extra.get("area_living"),
        area_kitchen=extra.get("area_kitchen"),
        floor=raw.floor,
        floors_total=raw.floors_total,
        raw_json=asdict(raw),  # JSON-serializable snapshot for debugging
    )
