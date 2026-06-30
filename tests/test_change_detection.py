"""Change-detection tests — pure logic, no DB, no network (CLAUDE.md §8, §13)."""

from __future__ import annotations

import pytest

from src.changes import PriceEvent, detect_price_change


def test_first_seen_when_no_previous() -> None:
    event = detect_price_change(None, 10_000_000)
    assert event == PriceEvent(kind="first_seen", new_price=10_000_000)
    assert event.old_price is None
    assert event.delta is None
    assert event.percent is None


def test_unchanged_when_equal() -> None:
    event = detect_price_change(10_000_000, 10_000_000)
    assert event.kind == "unchanged"
    assert event.old_price == 10_000_000
    assert event.delta == 0
    assert event.percent == 0.0


def test_changed_on_decrease() -> None:
    event = detect_price_change(10_000_000, 9_000_000)
    assert event.kind == "changed"
    assert event.old_price == 10_000_000
    assert event.new_price == 9_000_000
    assert event.delta == -1_000_000          # sign reflects direction
    assert event.percent == pytest.approx(-10.0)


def test_changed_on_increase() -> None:
    event = detect_price_change(10_000_000, 11_000_000)
    assert event.kind == "changed"
    assert event.delta == 1_000_000
    assert event.percent == pytest.approx(10.0)


def test_percent_uses_old_price_as_base() -> None:
    event = detect_price_change(8_000_000, 6_000_000)
    assert event.delta == -2_000_000
    assert event.percent == pytest.approx(-25.0)


def test_zero_previous_does_not_divide_by_zero() -> None:
    event = detect_price_change(0, 5_000)
    assert event.kind == "changed"
    assert event.delta == 5_000
    assert event.percent == 0.0
