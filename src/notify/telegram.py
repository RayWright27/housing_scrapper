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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

from src.tracker import Event, EventType

logger = logging.getLogger("realty.notify")

PARSE_MODE = "HTML"
# Non-breaking space (U+00A0) for money/units so a grouped number never wraps
# mid-value in the Telegram message (the Russian typographic convention).
NB = " "
Transport = Callable[[str], None]
MetaLookup = Callable[[int], "ListingMeta | None"]


@dataclass(frozen=True)
class PendingMessage:
    """One rendered, undelivered message replayed from the outbox."""

    id: int
    text: str


class Outbox(Protocol):
    """A persistent queue of messages that failed to send (CLAUDE.md §2, §9b).

    Injected so this module stays DB-free: the concrete implementation lives at
    the CLI seam (``main.py``) over the repository, exactly like ``get_meta``.
    """

    def pending(self) -> list[PendingMessage]: ...
    def remember(self, text: str) -> None: ...
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
            parts.append("studio" if meta.rooms == 0 else f"{meta.rooms}-room")
        if meta.area_total is not None:
            parts.append(f"{meta.area_total:g}{NB}m²")
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
    return _rub(round(price / meta.area_total)) + "/m²"


def format_price_changed(event: Event, meta: ListingMeta | None) -> str:
    down = (event.delta or 0) < 0
    arrow = "\U0001f4c9 ↓" if down else "\U0001f4c8 ↑"
    delta = f"{event.delta:+,}".replace(",", NB) + NB + "₽"  # keep explicit sign
    pct = f"{event.percent:+.1f}%"
    return (
        f"{arrow} Price changed ({pct}) · {_src(event)}\n"
        f"{_label(event, meta)}\n"
        f"{_rub(event.old_price)} → {_rub(event.new_price)} ({delta})\n"
        f"{_link(event)}"
    )


def format_delisted(event: Event, meta: ListingMeta | None) -> str:
    # Neutral: a delisting often means sold, but we don't assert it as fact.
    return (
        f"⚪️ Removed from listing · {_src(event)} (may be sold or withdrawn)\n"
        f"{_label(event, meta)}\n"
        f"{_link(event)}"
    )


def format_now_tracking(event: Event, meta: ListingMeta | None) -> str:
    per_m2 = _price_per_m2(event.price, meta)
    price_line = _rub(event.price)
    if per_m2 is not None:
        price_line += f" (≈ {per_m2})"
    return (
        f"\U0001f195 Now tracking · {_src(event)}\n"
        f"{_label(event, meta)}\n"
        f"{price_line}\n"
        f"{_link(event)}"
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
def _http_transport(token: str, chat_id: str, timeout: float) -> Transport:
    """Build a transport that POSTs one message to the Bot API via stdlib."""
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(text: str) -> None:
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": PARSE_MODE,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                raise RuntimeError(f"telegram HTTP {resp.status}")

    return send


class TelegramNotifier:
    """Delivers events to Telegram. Optional-by-config; failures are swallowed."""

    def __init__(
        self,
        token: str | None,
        chat_id: str | None,
        *,
        notify_on_new: bool = True,
        transport: Transport | None = None,
        outbox: Outbox | None = None,
        timeout: float = 10.0,
        max_attempts: int = 2,
        retry_delay: float = 1.0,
    ) -> None:
        self.enabled = bool(token and chat_id)
        self.notify_on_new = notify_on_new
        self._outbox = outbox
        self._max_attempts = max(1, max_attempts)
        self._retry_delay = retry_delay
        if transport is not None:
            self._transport: Transport | None = transport
        elif self.enabled:
            self._transport = _http_transport(token, chat_id, timeout)  # type: ignore[arg-type]
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
            settings.telegram_chat_id,
            notify_on_new=settings.notify_on_new,
            transport=transport,
            outbox=outbox,
        )

    def notify(self, events, get_meta: MetaLookup) -> None:
        """Format and send each relevant event. Never raises.

        A send that fails (e.g. Telegram unreachable — no VPN) is not dropped:
        the rendered message is queued in the outbox and replayed, oldest first,
        the next time delivery succeeds. Any existing backlog is flushed before
        this run's new events so messages keep their chronological order.
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
            if online:
                ok, _err = self._deliver(text)
                if ok:
                    continue
                online = False  # connection just dropped mid-batch
            self._queue(text)

    def _flush_pending(self) -> bool:
        """Replay queued messages oldest-first. Return False if Telegram is
        unreachable (so the caller stops attempting new sends this pass).

        With no outbox configured there is nothing to replay and we optimistically
        report online, preserving the original drop-on-failure behaviour."""
        if self._outbox is None:
            return True
        for item in self._outbox.pending():
            ok, err = self._deliver(item.text)
            if not ok:
                self._outbox.attempted(item.id, err or "unknown")
                return False
            self._outbox.forget(item.id)
        return True

    def _queue(self, text: str) -> None:
        """Persist an undeliverable message for later, or drop it if no outbox."""
        if self._outbox is not None:
            self._outbox.remember(text)
        else:
            logger.error("telegram send gave up after %d attempts and no outbox "
                         "is configured; event dropped (price already persisted)",
                         self._max_attempts)

    def _deliver(self, text: str) -> tuple[bool, str | None]:
        """Attempt one message with a small bounded retry. Never raises.

        Returns ``(True, None)`` on success, or ``(False, error_type_name)`` after
        exhausting attempts — the caller decides whether to queue it."""
        assert self._transport is not None  # enabled implies a transport
        error: str | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._transport(text)
                return True, None
            except Exception as exc:  # noqa: BLE001 - delivery must never crash a run
                # Log the failure REASON only — never the endpoint (embeds token).
                error = type(exc).__name__
                logger.warning("telegram send failed (attempt %d/%d): %s",
                               attempt, self._max_attempts, error)
                if attempt < self._max_attempts:
                    time.sleep(self._retry_delay)
        return False, error
