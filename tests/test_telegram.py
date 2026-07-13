"""Telegram notifier tests — offline only, fake transport, no real network."""

from __future__ import annotations

import urllib.error

import pytest

from src.notify import telegram as tg
from src.notify.telegram import (
    ListingMeta,
    PendingMessage,
    PermanentSendError,
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


def test_price_changed_shows_target_distance_above() -> None:
    # Current price above the target -> "🎯 target … (N above)".
    meta = ListingMeta(rooms=2, area_total=56.6, target_price=13_000_000)
    text = N(format_price_changed(price_changed(), meta))
    assert "🎯" in text and "13 000 000" in text and "выше" in text


def test_price_changed_shows_target_reached_when_below() -> None:
    # New price at/below the target -> "✅ below target".
    meta = ListingMeta(target_price=15_000_000)
    text = N(format_price_changed(price_changed(), meta))  # new_price 14_000_000
    assert "✅" in text and "15 000 000" in text


def test_no_target_line_when_target_unset() -> None:
    text = format_price_changed(price_changed(), ListingMeta(area_total=56.6))
    assert "🎯" not in text and "✅" not in text


def test_now_tracking_includes_target_line() -> None:
    meta = ListingMeta(area_total=50.0, target_price=10_000_000)
    text = N(format_now_tracking(now_tracking(14_800_000), meta))
    assert "🎯" in text and "10 000 000" in text


class FakeTransport:
    """Per-recipient transport (chat_id, text). Records what it is asked to send.

    ``fail`` may be flipped between ``notify`` calls to simulate connectivity
    coming back (VPN on). ``fail_for`` marks specific chat ids as failing (the
    rest succeed) to simulate a partial delivery. ``permanent_for`` raises a
    permanent rejection for the given chat ids."""

    def __init__(self, fail: bool = False, *, fail_for=(), permanent_for=()) -> None:
        self.sent: list[tuple[str, str]] = []  # (chat_id, text) that succeeded
        self.attempts: list[tuple[str, str]] = []  # every call, success or not
        self.fail = fail
        self.fail_for = set(fail_for)
        self.permanent_for = set(permanent_for)

    def __call__(self, chat_id: str, text: str) -> None:
        self.attempts.append((chat_id, text))
        if chat_id in self.permanent_for:
            raise PermanentSendError("HTTP 403")
        if self.fail or chat_id in self.fail_for:
            raise RuntimeError("simulated telegram failure")
        self.sent.append((chat_id, text))

    def texts_for(self, chat_id: str) -> list[str]:
        return [t for c, t in self.sent if c == chat_id]


class FakeOutbox:
    """In-memory stand-in for the DB-backed outbox (mirrors its semantics)."""

    def __init__(self) -> None:
        self._items: list[PendingMessage] = []
        self._next = 1
        self.attempts: list[int] = []  # ids marked as failed replays

    def pending(self) -> list[PendingMessage]:
        return list(self._items)

    def remember(self, chat_id: str, text: str) -> None:
        self._items.append(PendingMessage(id=self._next, chat_id=chat_id, text=text))
        self._next += 1

    def forget(self, message_id: int) -> None:
        self._items = [m for m in self._items if m.id != message_id]

    def attempted(self, message_id: int, error: str) -> None:
        self.attempts.append(message_id)


def _notifier(transport, *, chat_ids="chat", notify_on_new=True, max_attempts=2,
              outbox=None):
    return TelegramNotifier("tok", chat_ids, notify_on_new=notify_on_new,
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
    assert "2-комн. · 56.6 м² · «my flat»" in text


def test_price_changed_up_uses_up_arrow_and_positive_signs() -> None:
    text = N(format_price_changed(price_changed(delta_down=False), META))
    assert "↑" in text
    assert "+1 000 000 ₽" in text
    assert "+10.0%" in text


def test_now_tracking_includes_rounded_price_per_m2() -> None:
    text = N(format_now_tracking(now_tracking(14_800_000), META))
    # 14_800_000 / 56.6 = 261_484.1... -> rounded
    assert "261 484 ₽/м²" in text
    assert "14 800 000 ₽" in text


def test_now_tracking_omits_price_per_m2_when_area_missing() -> None:
    text = N(format_now_tracking(now_tracking(), ListingMeta(rooms=2, area_total=None)))
    assert "/м²" not in text
    assert "14 800 000 ₽" in text


def test_studio_and_missing_meta_label_gracefully() -> None:
    studio = format_now_tracking(now_tracking(), ListingMeta(rooms=0, area_total=30.0))
    assert "студия" in studio
    no_meta = format_now_tracking(now_tracking(), None)
    assert "«my flat»" in no_meta  # falls back to the note


def test_delisted_is_neutral_and_does_not_assert_sold() -> None:
    text = format_delisted(delisted(), META)
    assert "возможно, продано или снято" in text
    # neutral: hedged ("возможно"), never states removal as a definite sale
    assert "продано." not in text


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
    assert "Цена изменилась" in transport.sent[0][1]


def test_send_text_delivers_to_every_recipient() -> None:
    transport = FakeTransport()
    _notifier(transport, chat_ids=["1", "2"]).send_text("⚠️ warning")
    assert transport.sent == [("1", "⚠️ warning"), ("2", "⚠️ warning")]


def test_send_text_disabled_is_noop() -> None:
    transport = FakeTransport()
    TelegramNotifier(None, None, transport=transport).send_text("x")  # must not raise
    assert transport.sent == []


def test_send_text_transient_failure_goes_to_outbox() -> None:
    transport = FakeTransport(fail=True)
    outbox = FakeOutbox()
    _notifier(transport, chat_ids=["1", "2"], outbox=outbox).send_text("warn")
    assert transport.sent == []
    assert [(m.chat_id, m.text) for m in outbox.pending()] == [("1", "warn"), ("2", "warn")]

    # connectivity back: the queued warning is replayed on the next send_text
    transport.fail = False
    _notifier(transport, chat_ids=["1", "2"], outbox=outbox).send_text("second")
    assert ("1", "warn") in transport.sent and ("2", "warn") in transport.sent
    assert outbox.pending() == []


def test_now_tracking_suppressed_when_notify_on_new_false() -> None:
    transport = FakeTransport()
    _notifier(transport, notify_on_new=False).notify(
        [now_tracking(), price_changed()], lambda _l: META
    )
    # only the price change goes out; the new-listing event is skipped
    assert len(transport.sent) == 1
    assert "Цена изменилась" in transport.sent[0][1]


def test_send_failure_is_swallowed_and_retried_but_never_raises() -> None:
    transport = FakeTransport(fail=True)
    notifier = _notifier(transport, max_attempts=2)
    notifier.notify([price_changed()], lambda _l: META)  # must NOT raise
    assert len(transport.attempts) == 2  # bounded retry: attempted twice, gave up
    assert transport.sent == []          # nothing actually delivered


def test_get_meta_none_still_sends() -> None:
    transport = FakeTransport()
    _notifier(transport).notify([price_changed()], lambda _l: None)
    assert len(transport.sent) == 1  # falls back to note-only label, still delivers


# --------------------------------------------------------------------------- #
# outbox: undeliverable messages are queued per recipient and replayed
# --------------------------------------------------------------------------- #
def test_failed_send_is_queued_not_dropped() -> None:
    transport = FakeTransport(fail=True)  # Telegram unreachable (e.g. no VPN)
    outbox = FakeOutbox()
    _notifier(transport, outbox=outbox).notify([price_changed()], lambda _l: META)
    pending = outbox.pending()
    assert len(pending) == 1                       # not dropped — it is queued
    assert pending[0].chat_id == "chat"
    assert "Цена изменилась" in pending[0].text


def test_backlog_is_replayed_when_connection_returns() -> None:
    transport = FakeTransport(fail=True)
    outbox = FakeOutbox()
    notifier = _notifier(transport, outbox=outbox)
    notifier.notify([price_changed()], lambda _l: META)   # offline -> queued
    assert len(outbox.pending()) == 1

    transport.fail = False                                # VPN comes back
    notifier.notify([], lambda _l: META)                  # any next pass flushes
    assert outbox.pending() == []                         # delivered and forgotten
    assert any("Цена изменилась" in t for _c, t in transport.sent)


def test_backlog_is_flushed_before_new_events() -> None:
    transport = FakeTransport()                    # online
    outbox = FakeOutbox()
    outbox.remember("chat", "QUEUED-OLD")          # a pre-existing backlog message
    _notifier(transport, outbox=outbox).notify([now_tracking()], lambda _l: META)
    assert outbox.pending() == []                  # backlog cleared
    assert transport.sent[0][1] == "QUEUED-OLD"    # replayed FIRST (chronological)
    assert "Отслеживаем" in transport.sent[1][1]  # the new event goes out AFTER


def test_offline_retains_backlog_and_queues_new_events() -> None:
    transport = FakeTransport(fail=True)           # still offline
    outbox = FakeOutbox()
    outbox.remember("chat", "QUEUED-OLD")
    _notifier(transport, outbox=outbox).notify([now_tracking()], lambda _l: META)
    texts = [m.text for m in outbox.pending()]
    assert "QUEUED-OLD" in texts                   # existing backlog kept
    assert any("Отслеживаем" in t for t in texts)  # new event queued too
    assert outbox.attempts == [1]                  # the backlog replay was tried once


# --------------------------------------------------------------------------- #
# multiple recipients + per-recipient queueing (no duplicate on partial delivery)
# --------------------------------------------------------------------------- #
def test_delivers_to_every_recipient() -> None:
    transport = FakeTransport()
    _notifier(transport, chat_ids=["a", "b"]).notify([price_changed()], lambda _l: META)
    assert len(transport.texts_for("a")) == 1
    assert len(transport.texts_for("b")) == 1


def test_partial_failure_queues_only_the_unreached_recipient() -> None:
    # b is unreachable this pass; a receives it. Only b must be queued.
    transport = FakeTransport(fail_for=["b"])
    outbox = FakeOutbox()
    _notifier(transport, chat_ids=["a", "b"], outbox=outbox).notify(
        [price_changed()], lambda _l: META)
    assert len(transport.texts_for("a")) == 1          # a got it
    pending = outbox.pending()
    assert len(pending) == 1 and pending[0].chat_id == "b"  # only b queued


def test_replay_does_not_duplicate_to_already_delivered_recipient() -> None:
    # This is the bug being fixed: a partial failure must not re-send to the
    # recipient that already received the message.
    transport = FakeTransport(fail_for=["b"])
    outbox = FakeOutbox()
    notifier = _notifier(transport, chat_ids=["a", "b"], outbox=outbox)
    notifier.notify([price_changed()], lambda _l: META)   # a delivered, b queued

    transport.fail_for = set()                            # b reachable now
    notifier.notify([], lambda _l: META)                  # flush the backlog
    assert outbox.pending() == []
    assert len(transport.texts_for("a")) == 1             # a still only ONCE
    assert len(transport.texts_for("b")) == 1             # b finally gets it


def test_permanently_rejected_recipient_is_not_queued() -> None:
    transport = FakeTransport(permanent_for=["bad"])
    outbox = FakeOutbox()
    _notifier(transport, chat_ids=["good", "bad"], outbox=outbox).notify(
        [price_changed()], lambda _l: META)
    assert len(transport.texts_for("good")) == 1   # healthy recipient delivered
    assert outbox.pending() == []                  # bad recipient NOT queued


def test_as_chat_list_normalizes_single_and_many() -> None:
    assert tg._as_chat_list("111") == ["111"]          # a lone id still works
    assert tg._as_chat_list(["111", "222"]) == ["111", "222"]
    assert tg._as_chat_list(None) == []
    assert tg._as_chat_list(()) == []


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_transport_posts_chat_id_and_text(monkeypatch) -> None:
    bodies: list[str] = []

    def fake_urlopen(req, timeout=None):
        bodies.append(req.data.decode())
        return _Resp()

    monkeypatch.setattr(tg.urllib.request, "urlopen", fake_urlopen)
    tg._http_transport("tok", timeout=1)("111", "hello")
    assert len(bodies) == 1 and "chat_id=111" in bodies[0]


def test_http_transport_raises_permanent_on_http_error(monkeypatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("url", 403, "Forbidden", {}, None)

    monkeypatch.setattr(tg.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(tg.PermanentSendError):  # 4xx -> permanent, not retried
        tg._http_transport("tok", timeout=1)("bad", "hi")


def test_http_transport_propagates_transient_failure(monkeypatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(tg.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.URLError):  # transient -> notifier queues it
        tg._http_transport("tok", timeout=1)("111", "hi")
