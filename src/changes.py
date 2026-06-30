"""Pure price-change detection (CLAUDE.md §8).

This is the part of the system we trust most, so it is deliberately decoupled
from scraping, the database, and notifications: a single pure function over a
previous price and a new observation. ``tracker.py`` will call this in a later
phase, but it imports nothing site-specific and touches no I/O, so it is fully
unit-testable on fabricated data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EventKind = Literal["first_seen", "changed", "unchanged"]


@dataclass(frozen=True)
class PriceEvent:
    """The outcome of comparing a new price against the previous one.

    - ``first_seen``: no prior price existed; only ``new_price`` is set.
    - ``changed``: price differs; ``old_price``, ``delta`` and ``percent`` set.
    - ``unchanged``: price is identical; ``old_price`` set, ``delta`` is 0.
    """

    kind: EventKind
    new_price: int
    old_price: int | None = None
    delta: int | None = None        # new_price - old_price
    percent: float | None = None    # delta / old_price * 100


def detect_price_change(previous: int | None, new_price: int) -> PriceEvent:
    """Classify a new observation against the previous last price.

    ``previous is None`` means this listing has no prior observation.
    Money is integer rubles; ``percent`` is a float relative to the old price.
    """
    if previous is None:
        return PriceEvent(kind="first_seen", new_price=new_price)

    if new_price == previous:
        return PriceEvent(
            kind="unchanged",
            new_price=new_price,
            old_price=previous,
            delta=0,
            percent=0.0,
        )

    delta = new_price - previous
    percent = (delta / previous * 100) if previous != 0 else 0.0
    return PriceEvent(
        kind="changed",
        new_price=new_price,
        old_price=previous,
        delta=delta,
        percent=percent,
    )
