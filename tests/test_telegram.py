"""Telegram notifier tests — offline only, fake transport, no real network."""

from __future__ import annotations

from src.notify.telegram import (
    ListingMeta,
    PendingMessage,
    TelegramNotifier,
    format_delisted,
    format_now_tracking,
    format_price_changed,
)
from src.tracker import Event, EventType

URL = "https://spb.cian.ru/sale/flat/1/"
META = ListingMeta(rooms=2, area_total=56.6, title="2-room", address=None)


def N(text: str) -> str:
    """Normalize non-breaking spaces to plain spaces for readable assertions."""
    return text.replace(" ", " ")


def price_changed(delta_down: bool = True) -> Event:
    if delta_down:
        return Event(type=EventType.PRICE_CHANGED, source="cian", listing_id=1,
                     external_id="1", url=URL, note="my flat",
                     old_price=14_800_000, new_price=14_000_000,
                     delta=-800_000, percent=-5.405)
    return Event(type=EventType.PRICE_CHANGED, source="cian", listing_id=1,
                 external_id="1", url=URL, note="my flat",
                 old_price=10_000_000, new_price=11_000_000,
                 delta=1_000_000, percent=10.0)


def now_tracking(price: int = 14_800_000) -> Event:
    return Event(type=EventType.NOW_TRACKING, source="cian", listing_id=1,
                 external_id="1", url=URL, note="my flat", price=price)


def delisted() -> Event:
    return Event(type=EventType.DELISTED, source="cian", listing_id=1,
                 external_id="1", url=URL, note="my flat")


class FakeTransport:
    """Records the texts it is asked to send; can be told to fail.

    ``fail`` may be flipped between ``notify`` calls to simulate connectivity
    coming back (e.g. the VPN being turned on)."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    def __call__(self, text: str) -> None:
        self.sent.append(text)  # records the attempt (even a failing one)
        if self.fail:
            raise RuntimeError("simulated telegram failure")


class FakeOutbox:
    """In-memory stand-in for the DB-backed outbox (mirrors its semantics)."""

    def __init__(self) -> None:
        self._items: list[PendingMessage] = []
        self._next = 1
        self.attempts: list[int] = []  # ids marked as failed replays

    def pending(self) -> list[PendingMessage]:
        return list(self._items)

    def remember(self, text: str) -> None:
        self._items.append(PendingMessage(id=self._next, text=text))
        self._next += 1

    def forget(self, message_id: int) -> None:
        self._items = [m for m in self._items if m.id != message_id]

    def attempted(self, message_id: int, error: str) -> None:
        self.attempts.append(message_id)


def _notifier(transport, *, notify_on_new=True, max_attempts=2, outbox=None):
    return TelegramNotifier("tok", "chat", notify_on_new=notify_on_new,
                            transport=transport, outbox=outbox,
                            max_attempts=max_attempts, retry_delay=0)


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def test_price_changed_down_has_math_direction_and_link() -> None:
    text = N(format_price_changed(price_changed(delta_down=True), META))
    assert "↓" in text
    assert "14 800 000 ₽" in text and "14 000 000 ₽" in text   # separators
    assert "-800 000 ₽" in text                                # delta rubles
    assert "-5.4%" in text                                     # percent
    assert '<a href="https://spb.cian.ru/sale/flat/1/">' in text
    assert "2-room · 56.6 m² · «my flat»" in text


def test_price_changed_up_uses_up_arrow_and_positive_signs() -> None:
    text = N(format_price_changed(price_changed(delta_down=False), META))
    assert "↑" in text
    assert "+1 000 000 ₽" in text
    assert "+10.0%" in text


def test_now_tracking_includes_rounded_price_per_m2() -> None:
    text = N(format_now_tracking(now_tracking(14_800_000), META))
    # 14_800_000 / 56.6 = 261_484.1... -> rounded
    assert "261 484 ₽/m²" in text
    assert "14 800 000 ₽" in text


def test_now_tracking_omits_price_per_m2_when_area_missing() -> None:
    text = N(format_now_tracking(now_tracking(), ListingMeta(rooms=2, area_total=None)))
    assert "/m²" not in text
    assert "14 800 000 ₽" in text


def test_studio_and_missing_meta_label_gracefully() -> None:
    studio = format_now_tracking(now_tracking(), ListingMeta(rooms=0, area_total=30.0))
    assert "studio" in studio
    no_meta = format_now_tracking(now_tracking(), None)
    assert "«my flat»" in no_meta  # falls back to the note


def test_delisted_is_neutral_and_does_not_assert_sold() -> None:
    text = format_delisted(delisted(), META)
    assert "may be sold or withdrawn" in text
    # neutral: never states it as a fact
    assert "sold." not in text.lower()


def test_messages_show_the_source() -> None:
    # CIAN events say CIAN; an Avito event says Avito — so the two are distinct.
    assert "CIAN" in format_now_tracking(now_tracking(), META)
    assert "CIAN" in format_price_changed(price_changed(), META)
    assert "CIAN" in format_delisted(delisted(), META)
    avito_ev = Event(type=EventType.NOW_TRACKING, source="avito", listing_id=2,
                     external_id="9", url="https://www.avito.ru/x_9", note="flat",
                     price=5_000_000)
    text = format_now_tracking(avito_ev, ListingMeta(rooms=1, area_total=30.0))
    assert "Avito" in text and "CIAN" not in text


def test_special_characters_are_escaped() -> None:
    ev = Event(type=EventType.NOW_TRACKING, source="cian", listing_id=1,
               external_id="1", url=URL, note="A & B <script> \"x\"", price=1_000_000)
    text = format_now_tracking(ev, ListingMeta(rooms=1, area_total=20.0))
    assert "&amp;" in text and "&lt;script&gt;" in text
    assert "<script>" not in text  # raw tag must not survive into the message


# --------------------------------------------------------------------------- #
# send layer
# --------------------------------------------------------------------------- #
def test_disabled_notifier_is_a_silent_no_op() -> None:
    transport = FakeTransport()
    # No token/chat id -> disabled. Even with a transport, nothing is sent.
    notifier = TelegramNotifier(None, None, transport=transport)
    assert notifier.enabled is False
    notifier.notify([price_changed(), delisted()], lambda _l: META)  # must not raise
    assert transport.sent == []


def test_relevant_events_are_sent_with_formatted_text() -> None:
    transport = FakeTransport()
    _notifier(transport).notify([price_changed()], lambda _l: META)
    assert len(transport.sent) == 1
    assert "Price changed" in transport.sent[0]


def test_now_tracking_suppressed_when_notify_on_new_false() -> None:
    transport = FakeTransport()
    _notifier(transport, notify_on_new=False).notify(
        [now_tracking(), price_changed()], lambda _l: META
    )
    # only the price change goes out; the new-listing event is skipped
    assert len(transport.sent) == 1
    assert "Price changed" in transport.sent[0]


def test_send_failure_is_swallowed_and_retried_but_never_raises() -> None:
    transport = FakeTransport(fail=True)
    notifier = _notifier(transport, max_attempts=2)
    notifier.notify([price_changed()], lambda _l: META)  # must NOT raise
    assert len(transport.sent) == 2  # bounded retry: attempted twice, then gave up


def test_get_meta_none_still_sends() -> None:
    transport = FakeTransport()
    _notifier(transport).notify([price_changed()], lambda _l: None)
    assert len(transport.sent) == 1  # falls back to note-only label, still delivers


# --------------------------------------------------------------------------- #
# outbox: undeliverable messages are queued and replayed, never lost
# --------------------------------------------------------------------------- #
def test_failed_send_is_queued_not_dropped() -> None:
    transport = FakeTransport(fail=True)  # Telegram unreachable (e.g. no VPN)
    outbox = FakeOutbox()
    _notifier(transport, outbox=outbox).notify([price_changed()], lambda _l: META)
    pending = outbox.pending()
    assert len(pending) == 1                       # not dropped — it is queued
    assert "Price changed" in pending[0].text


def test_backlog_is_replayed_when_connection_returns() -> None:
    transport = FakeTransport(fail=True)
    outbox = FakeOutbox()
    notifier = _notifier(transport, outbox=outbox)
    notifier.notify([price_changed()], lambda _l: META)   # offline -> queued
    assert len(outbox.pending()) == 1

    transport.fail = False                                # VPN comes back
    notifier.notify([], lambda _l: META)                  # any next pass flushes
    assert outbox.pending() == []                         # delivered and forgotten
    assert any("Price changed" in t for t in transport.sent)


def test_backlog_is_flushed_before_new_events() -> None:
    transport = FakeTransport()                    # online
    outbox = FakeOutbox()
    outbox.remember("QUEUED-OLD")                  # a pre-existing backlog message
    _notifier(transport, outbox=outbox).notify([now_tracking()], lambda _l: META)
    assert outbox.pending() == []                  # backlog cleared
    assert transport.sent[0] == "QUEUED-OLD"       # replayed FIRST (chronological)
    assert "Now tracking" in transport.sent[1]     # the new event goes out AFTER


def test_offline_retains_backlog_and_queues_new_events() -> None:
    transport = FakeTransport(fail=True)           # still offline
    outbox = FakeOutbox()
    outbox.remember("QUEUED-OLD")
    _notifier(transport, outbox=outbox).notify([now_tracking()], lambda _l: META)
    texts = [m.text for m in outbox.pending()]
    assert "QUEUED-OLD" in texts                   # existing backlog kept
    assert any("Now tracking" in t for t in texts)  # new event queued too
    assert outbox.attempts == [1]                  # the backlog replay was tried once
