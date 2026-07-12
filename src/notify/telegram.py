"""Telegram push notifications — a thin, DB-free presentation sink (§9b).

This layer only FORMATS events into messages and SENDS them. It contains no
business logic, no scraping, and no DB access: the listing metadata it needs
(rooms/area/title) is supplied by an injected ``get_meta`` callable, so the DB
read happens at the CLI seam, not here (keeping this module trivially testable).

Mirrors the phase-3 fetch/parse split:
- pure ``format_*`` functions (Event -> text) — deterministic, unit-tested;
- a thin :class:`TelegramNotifier` whose ``transport`` does the actual HTTPS
  call (stdlib ``urllib`` — no extra dependency) and is injectable/faked in
  tests.

Telegram is optional (§10): with no token/chat id the notifier is a silent
no-op. A send never aborts a run. When delivery fails (e.g. Telegram is
unreachable with no VPN) the rendered message is not lost: it is stored in a
persistent outbox and replayed, oldest first, the next time a send succeeds.
The outbox is injected (a DB-free port, like ``get_meta``) so this module keeps
no DB access. Secrets (token, chat id) are never logged.
"""

from __future__ import annotations

import html
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from src.tracker import Event, EventType

logger = logging.getLogger("realty.notify")

PARSE_MODE = "HTML"
# Non-breaking space (U+00A0) for money/units so a grouped number never wraps
# mid-value in the Telegram message (the Russian typographic convention).
NB = " "
# A transport delivers one rendered message to ONE recipient: (chat_id, text).
# It raises PermanentSendError when that recipient will never accept the message
# (bad id / bot not started / blocked); any other exception is treated as a
# transient failure worth queueing and retrying.
Transport = Callable[[str, str], None]
MetaLookup = Callable[[int], "ListingMeta | None"]


class PermanentSendError(Exception):
    """A recipient rejected the message for good (not worth retrying/queueing)."""


@dataclass(frozen=True)
class PendingMessage:
    """One rendered, undelivered message for one recipient, replayed from the
    outbox. Queuing is per-recipient so a replay never re-sends to a chat that
    already received the message."""

    id: int
    chat_id: str
    text: str


class Outbox(Protocol):
    """A persistent queue of (recipient, message) pairs that failed to send
    (CLAUDE.md §2, §9b).

    Injected so this module stays DB-free: the concrete implementation lives at
    the CLI seam (``main.py``) over the repository, exactly like ``get_meta``.
    """

    def pending(self) -> list[PendingMessage]: ...
    def remember(self, chat_id: str, text: str) -> None: ...
    def forget(self, message_id: int) -> None: ...
    def attempted(self, message_id: int, error: str) -> None: ...


@dataclass(frozen=True)
class ListingMeta:
    """The listing-detail fields a message needs but the Event does not carry.
    Supplied by the caller (via ``get_meta``) so this module stays DB-free."""

    rooms: int | None = None
    area_total: float | None = None
    title: str | None = None
    address: str | None = None
    # Optional user-set price target (rubles). Purely message CONTEXT: the target
    # does not gate whether a message is sent — a price change already does. When
    # present, the message shows how the current price sits against the target.
    target_price: int | None = None


# --------------------------------------------------------------------------- #
# pure formatting (zero I/O, fully unit-tested)
# --------------------------------------------------------------------------- #
def _group(value: int) -> str:
    """Integer with non-breaking-space thousands separators (signed ok)."""
    return f"{value:,}".replace(",", NB)


def _rub(value: int) -> str:
    """Integer rubles -> human string, e.g. '14 800 000 ₽' (NB-spaced)."""
    return _group(value) + NB + "₽"


def _esc(text: str) -> str:
    """HTML-escape any listing/user-derived text so it can't corrupt the message."""
    return html.escape(text, quote=False)


def _label(event: Event, meta: ListingMeta | None) -> str:
    """Build 'rooms · area · «note»' from whatever is available (all optional)."""
    parts: list[str] = []
    if meta is not None:
        if meta.rooms is not None:
            parts.append("студия" if meta.rooms == 0 else f"{meta.rooms}-комн.")
        if meta.area_total is not None:
            parts.append(f"{meta.area_total:g}{NB}м²")
    if event.note:
        parts.append(f"«{_esc(event.note)}»")
    return " · ".join(parts) if parts else _esc(event.external_id)


_SOURCE_LABELS = {"cian": "CIAN", "avito": "Avito"}


def _src(event: Event) -> str:
    return _SOURCE_LABELS.get(event.source, (event.source or "").upper())


def _link(event: Event) -> str:
    return f'<a href="{html.escape(event.url, quote=True)}">{_src(event)} →</a>'


def _price_per_m2(price: int, meta: ListingMeta | None) -> str | None:
    if meta is None or not meta.area_total:
        return None
    return _rub(round(price / meta.area_total)) + "/м²"


def _target_line(current: int | None, meta: ListingMeta | None) -> str | None:
    """A '🎯 target …' / '✅ target reached' context line, or ``None`` if no target.

    Pure display: shows the current price's distance to the user's target. The
    target never decides whether a message is sent (a price change does)."""
    if meta is None or meta.target_price is None or current is None:
        return None
    target = meta.target_price
    gap = current - target
    if gap > 0:
        return f"\U0001f3af Цель: {_rub(target)} (на {_group(gap)}{NB}₽ выше)"
    if gap < 0:
        return f"✅ Ниже цели {_rub(target)} (на {_group(-gap)}{NB}₽)"
    return f"✅ Цель {_rub(target)} достигнута"


def format_price_changed(event: Event, meta: ListingMeta | None) -> str:
    down = (event.delta or 0) < 0
    arrow = "\U0001f4c9 ↓" if down else "\U0001f4c8 ↑"
    delta = f"{event.delta:+,}".replace(",", NB) + NB + "₽"  # keep explicit sign
    pct = f"{event.percent:+.1f}%"
    target = _target_line(event.new_price, meta)
    return (
        f"{arrow} Цена изменилась ({pct}) · {_src(event)}\n"
        f"{_label(event, meta)}\n"
        f"{_rub(event.old_price)} → {_rub(event.new_price)} ({delta})\n"
        + (f"{target}\n" if target else "")
        + f"{_link(event)}"
    )


def format_delisted(event: Event, meta: ListingMeta | None) -> str:
    # Neutral: a delisting often means sold, but we don't assert it as fact.
    return (
        f"⚪️ Снято с публикации · {_src(event)} (возможно, продано или снято)\n"
        f"{_label(event, meta)}\n"
        f"{_link(event)}"
    )


def format_now_tracking(event: Event, meta: ListingMeta | None) -> str:
    per_m2 = _price_per_m2(event.price, meta)
    price_line = _rub(event.price)
    if per_m2 is not None:
        price_line += f" (≈ {per_m2})"
    target = _target_line(event.price, meta)
    return (
        f"\U0001f195 Отслеживаем · {_src(event)}\n"
        f"{_label(event, meta)}\n"
        f"{price_line}\n"
        + (f"{target}\n" if target else "")
        + f"{_link(event)}"
    )


def format_event(event: Event, meta: ListingMeta | None) -> str | None:
    """Dispatch to the right formatter, or ``None`` for an unknown type."""
    if event.type == EventType.PRICE_CHANGED:
        return format_price_changed(event, meta)
    if event.type == EventType.DELISTED:
        return format_delisted(event, meta)
    if event.type == EventType.NOW_TRACKING:
        return format_now_tracking(event, meta)
    return None


# --------------------------------------------------------------------------- #
# thin send layer (network; injectable transport)
# --------------------------------------------------------------------------- #
def _as_chat_list(chat_ids: str | Sequence[str] | None) -> list[str]:
    """Normalize a single id or a sequence of ids to a list (empty if none)."""
    if not chat_ids:
        return []
    if isinstance(chat_ids, str):
        return [chat_ids]
    return [c for c in chat_ids if c]


def _http_transport(token: str, timeout: float) -> Transport:
    """Build a transport that POSTs one message to ONE recipient via stdlib.

    Delivery is per-recipient so the notifier can queue and replay each chat
    independently (no duplicate on partial delivery). Failure classes:
    - HTTP 4xx from Telegram (bad id, or the recipient never started / blocked
      the bot) -> :class:`PermanentSendError`: never worth retrying.
    - a network error (no connectivity — e.g. VPN off) propagates as-is and is
      treated as transient: the notifier queues it and replays later."""
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(chat_id: str, text: str) -> None:
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": PARSE_MODE,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                if resp.status != 200:
                    raise RuntimeError(f"telegram HTTP {resp.status}")
        except urllib.error.HTTPError as exc:  # 4xx/5xx: recipient rejected it
            raise PermanentSendError(f"HTTP {exc.code}") from exc

    return send


class TelegramNotifier:
    """Delivers events to Telegram. Optional-by-config; failures are swallowed."""

    def __init__(
        self,
        token: str | None,
        chat_ids: str | Sequence[str] | None,
        *,
        notify_on_new: bool = True,
        transport: Transport | None = None,
        outbox: Outbox | None = None,
        timeout: float = 10.0,
        max_attempts: int = 2,
        retry_delay: float = 1.0,
    ) -> None:
        # Accept a single id (str) or many (sequence); a message is delivered to
        # every recipient. A bot reaches only chats it is explicitly told about.
        self._recipients = _as_chat_list(chat_ids)
        self.enabled = bool(token and self._recipients)
        self.notify_on_new = notify_on_new
        self._outbox = outbox
        self._max_attempts = max(1, max_attempts)
        self._retry_delay = retry_delay
        if transport is not None:
            self._transport: Transport | None = transport
        elif self.enabled:
            self._transport = _http_transport(token, timeout)  # type: ignore[arg-type]
        else:
            self._transport = None

    @classmethod
    def from_settings(
        cls,
        settings,
        *,
        transport: Transport | None = None,
        outbox: Outbox | None = None,
    ) -> "TelegramNotifier":
        return cls(
            settings.telegram_bot_token,
            settings.telegram_chat_ids,
            notify_on_new=settings.notify_on_new,
            transport=transport,
            outbox=outbox,
        )

    def notify(self, events, get_meta: MetaLookup) -> None:
        """Format and send each relevant event to every recipient. Never raises.

        Delivery is tracked per recipient. A send that fails transiently (e.g.
        Telegram unreachable — no VPN) is not dropped: that (recipient, message)
        pair is queued in the outbox and replayed the next time delivery
        succeeds — and ONLY for the recipients it did not reach, so a replay
        never duplicates a message a chat already got. A permanent rejection
        (bad id / bot not started) is logged and skipped, never queued. Any
        existing backlog is flushed before this run's new events so messages
        keep their chronological order.
        """
        if not self.enabled:
            logger.info("telegram disabled (no token/chat id) — skipping %d event(s)",
                        len(events))
            return
        # Replay backlog first. If Telegram is still down this returns False and
        # we won't hammer it with new sends — new events go straight to the queue.
        online = self._flush_pending()
        for event in events:
            if event.type == EventType.NOW_TRACKING and not self.notify_on_new:
                continue
            meta = get_meta(event.listing_id)
            text = format_event(event, meta)
            if text is None:
                continue
            for chat_id in self._recipients:
                if online:
                    status = self._deliver(chat_id, text)
                    if status == "ok":
                        continue
                    if status == "permanent":
                        continue  # a bad recipient — do not queue it
                    online = False  # transient: connection just dropped
                self._queue(chat_id, text)

    def _flush_pending(self) -> bool:
        """Replay queued messages oldest-first. Return False if Telegram is
        unreachable (so the caller stops attempting new sends this pass).

        With no outbox configured there is nothing to replay and we optimistically
        report online, preserving the original drop-on-failure behaviour."""
        if self._outbox is None:
            return True
        for item in self._outbox.pending():
            status = self._deliver(item.chat_id, item.text)
            if status == "transient":
                self._outbox.attempted(item.id, "transient")
                return False
            # 'ok' delivered, or 'permanent' will never work — either way drop it.
            self._outbox.forget(item.id)
        return True

    def _queue(self, chat_id: str, text: str) -> None:
        """Persist an undeliverable message for one recipient, or drop if no outbox."""
        if self._outbox is not None:
            self._outbox.remember(chat_id, text)
        else:
            logger.error("telegram send gave up after %d attempts and no outbox "
                         "is configured; event dropped (price already persisted)",
                         self._max_attempts)

    def _deliver(self, chat_id: str, text: str) -> str:
        """Send one message to one recipient with a small bounded retry. Never
        raises. Returns ``"ok"``, ``"permanent"`` (recipient rejected it — do not
        retry), or ``"transient"`` (network failure — worth queueing)."""
        assert self._transport is not None  # enabled implies a transport
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._transport(chat_id, text)
                return "ok"
            except PermanentSendError as exc:
                logger.error("telegram rejected a recipient (%s); skipping it — "
                             "check its id / that it messaged the bot", exc)
                return "permanent"
            except Exception as exc:  # noqa: BLE001 - delivery must never crash a run
                # Log the failure REASON only — never the endpoint (embeds token).
                logger.warning("telegram send failed (attempt %d/%d): %s",
                               attempt, self._max_attempts, type(exc).__name__)
                if attempt < self._max_attempts:
                    time.sleep(self._retry_delay)
        return "transient"
