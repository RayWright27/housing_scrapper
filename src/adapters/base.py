"""Adapter contract shared by every site (CLAUDE.md §6).

This is the ONLY thing the rest of the system knows about scraping: a raw,
site-agnostic payload (:class:`RawListing`) and the shape every concrete
adapter must implement (:class:`SiteAdapter`). Site-specific parsing lives
exclusively in ``src/adapters/<site>.py`` — never here, never downstream.

An adapter only fetches and parses. It NEVER writes to the DB and NEVER sends
notifications; those side effects live in ``tracker.py`` (a later phase).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SiteBlocked(Exception):
    """The site could not be read this run (captcha / anti-bot / timeout).

    Distinct from "listing absent": a block means we simply *could not check*,
    so the tracker must NOT count it as a miss (that would falsely delist a
    listing after a few blocked runs). A genuine removal (a 404) still returns
    the ``None``/``[]`` sentinel and is counted as a miss (§8.7)."""


@dataclass
class RawListing:
    """A single listing as parsed straight from a site, before normalization.

    Only ``price`` and ``external_id`` are mandatory — every other field is
    optional and may be ``None`` when the site omits it. ``extra`` carries any
    site-specific data and stays opaque to everything downstream.
    """

    source: str
    external_id: str
    url: str
    price: int                       # rubles
    title: str | None = None
    address: str | None = None
    rooms: int | None = None
    area_total: float | None = None
    floor: int | None = None
    floors_total: int | None = None
    lat: float | None = None         # WGS-84 coordinates when the payload has them
    lon: float | None = None
    extra: dict | None = None        # anything site-specific, stays opaque


class SiteAdapter(Protocol):
    """Uniform interface every site adapter implements.

    On a soft failure (blocked, captcha, transient) implementations return the
    sentinel — ``None`` for a single listing, ``[]`` for a search — and must
    not crash the whole run.
    """

    source: str

    def fetch_listing(self, url: str) -> RawListing | None: ...

    def fetch_search(self, url: str) -> list[RawListing]: ...
