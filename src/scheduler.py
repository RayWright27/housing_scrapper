"""The scheduled CIAN pass loop, shared by two entry points (CLAUDE.md §16.7).

One implementation, two callers:
- ``python -m src.main run`` — a dedicated, headless scheduler process.
- ``python -m src.main serve`` — the same loop on a background daemon thread, so
  a single autostarted process gives you both the dashboard AND periodic scraping
  (Decision A). It stays an in-process stdlib loop; no APScheduler (§3).

The loop is deliberately thin and dependency-injected: the actual pass
(:func:`~src.main._cian_pass`) and the connection factory are passed in, so the
loop can be driven in tests with a fake pass and an in-memory DB — no network,
no real sleeping. Site knowledge stays in the adapters (§2/§15); this module only
sequences passes, records a heartbeat, and takes a daily backup.

Only CIAN is scheduled. Avito needs a human to load its tabs / solve a challenge
and cannot run headlessly (§7), so it is never driven from the background loop —
it stays on the dashboard's Refresh button (which opens the interactive grab).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.storage import repository as repo
from src.storage.db import backup_db

logger = logging.getLogger("realty.scheduler")

# app_meta keys the dashboard reads to show the scheduler's health (§9a status).
LAST_RUN_AT = "scheduler.last_run_at"
NEXT_RUN_AT = "scheduler.next_run_at"
LAST_STATUS = "scheduler.last_status"      # "ok" | "error"
LAST_ERROR = "scheduler.last_error"        # exception type name on failure, else ""
LAST_EVENTS = "scheduler.last_events"      # events emitted by the last pass
LAST_BACKUP_AT = "scheduler.last_backup_at"
LAST_SUCCESS_AT = "scheduler.last_success_at"    # last pass that ended status=ok
HEALTH_WARNED_AT = "scheduler.health_warned_at"  # last stale-tracker Telegram ping

# A backup is taken at most this often; a crash-loop restarting `serve` will not
# spam snapshots, and a long-running process still snapshots once a day (§10).
BACKUP_MIN_INTERVAL_H = 24

# Stale-tracker warning: ping when the last SUCCESSFUL pass is older than this
# many poll intervals (i.e. at least one whole interval was lost), and repeat the
# ping at most once per cooldown so a long outage does not spam.
HEALTH_STALE_INTERVALS = 2
HEALTH_WARN_COOLDOWN_H = 24

# Injected seams. ``PassFn`` runs one full pass (build adapter, fetch, notify) and
# returns its events; ``ConnFactory`` opens a fresh connection for a pass;
# ``WarnFn`` delivers one operational message (conn, text) — e.g. via Telegram.
PassFn = Callable[[object, object], list]
ConnFactory = Callable[[], object]
WarnFn = Callable[[object, str], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record_pass_result(conn, events, *, status: str, error: str,
                       next_run_at: str, now: str) -> None:
    """Write the scheduler heartbeat into ``app_meta`` (last run, next, result)."""
    repo.set_meta(conn, LAST_RUN_AT, now)
    repo.set_meta(conn, NEXT_RUN_AT, next_run_at)
    repo.set_meta(conn, LAST_STATUS, status)
    repo.set_meta(conn, LAST_ERROR, error)
    repo.set_meta(conn, LAST_EVENTS, str(len(events)))
    if status == "ok":
        repo.set_meta(conn, LAST_SUCCESS_AT, now)


def _age_str(hours: float) -> str:
    """Human age for the warning message: '7 ч' / '3 дн'."""
    if hours < 48:
        return f"{int(hours)} ч"
    return f"{int(hours // 24)} дн"


def check_health(conn, settings, *, warn: WarnFn, now: datetime | None = None) -> bool:
    """Ping the user (via ``warn``) when the tracker has gone stale.

    Stale = the last SUCCESSFUL pass is older than ``HEALTH_STALE_INTERVALS``
    poll intervals — i.e. runs keep failing, so the dashboard's history is
    silently rotting. Called from the loop's error path; the ping repeats at
    most once per ``HEALTH_WARN_COOLDOWN_H`` so a long outage does not spam.
    A tracker that has never succeeded is not warned about (fresh DB).
    Returns True when a warning was sent (for tests). Never raises.
    """
    now = now or _utcnow()
    last_success = repo.get_meta(conn, LAST_SUCCESS_AT)
    if not last_success:
        return False
    try:
        age_h = (now - datetime.fromisoformat(last_success)).total_seconds() / 3600
    except ValueError:
        return False
    threshold_h = HEALTH_STALE_INTERVALS * max(1, settings.poll_interval_hours)
    if age_h < threshold_h:
        return False
    warned = repo.get_meta(conn, HEALTH_WARNED_AT)
    if warned:
        try:
            if (now - datetime.fromisoformat(warned)) < timedelta(
                    hours=HEALTH_WARN_COOLDOWN_H):
                return False
        except ValueError:
            pass  # unparseable stored value: fall through and warn
    error = repo.get_meta(conn, LAST_ERROR) or ""
    text = (f"⚠️ Трекер давно не обновлялся: последняя успешная проверка "
            f"{_age_str(age_h)} назад."
            + (f"\nПоследняя ошибка: {error}" if error else ""))
    try:
        warn(conn, text)
    except Exception:  # noqa: BLE001 - a ping failure must not hurt the loop
        logger.warning("stale-tracker warning failed to send", exc_info=True)
        return False
    repo.set_meta(conn, HEALTH_WARNED_AT, now.isoformat())
    logger.warning("stale-tracker warning sent (no successful pass for %.0fh)", age_h)
    return True


def backup_if_due(conn, db_path: str, keep: int, *, now: datetime | None = None,
                  min_interval_h: int = BACKUP_MIN_INTERVAL_H):
    """Take a DB snapshot only if ``min_interval_h`` has passed since the last one.

    Records ``last_backup_at`` in ``app_meta`` on success. Returns the snapshot
    path, or ``None`` if a backup was not due (or was skipped/failed). Guards the
    irreplaceable price history (§2) without snapshotting on every restart."""
    now = now or _utcnow()
    last = repo.get_meta(conn, LAST_BACKUP_AT)
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(hours=min_interval_h):
                return None  # a recent backup already exists
        except ValueError:
            pass  # unparseable stored value: fall through and back up
    dest = backup_db(db_path, keep)
    if dest is not None:
        repo.set_meta(conn, LAST_BACKUP_AT, now.isoformat())
    return dest


def run_scheduler(
    pass_fn: PassFn,
    settings,
    *,
    conn_factory: ConnFactory,
    stop_event: threading.Event | None = None,
    on_startup_backup: bool = True,
    warn_fn: WarnFn | None = None,
) -> None:
    """Run ``pass_fn`` now, then every ``POLL_INTERVAL_HOURS`` until stopped.

    A fresh connection is opened per pass (WAL lets the loop write while the
    dashboard reads — §10). A failing pass is logged and recorded, never fatal:
    the loop survives so a transient block or crash does not stop tracking (§8).
    Waiting uses an interruptible :class:`threading.Event`, so the ``serve``
    daemon thread stops promptly and the ``run`` process still honours Ctrl+C.
    """
    stop = stop_event or threading.Event()
    interval_h = max(1, settings.poll_interval_hours)

    if on_startup_backup:
        startup_backup(conn_factory, settings)

    while not stop.is_set():
        _run_one(pass_fn, settings, conn_factory, interval_h, warn_fn=warn_fn)
        # Interruptible sleep: returns True immediately when stop is set.
        if stop.wait(timeout=interval_h * 3600):
            break
    logger.info("scheduler loop stopped")


def _run_one(pass_fn: PassFn, settings, conn_factory: ConnFactory,
             interval_h: int, warn_fn: WarnFn | None = None) -> list:
    """Execute one pass, record the heartbeat, and take a daily backup if due.

    Returns the pass's events (for the ``run`` CLI's console summary)."""
    now_dt = _utcnow()
    now = now_dt.isoformat()
    next_run_at = (now_dt + timedelta(hours=interval_h)).isoformat()
    conn = conn_factory()
    try:
        try:
            events = pass_fn(conn, settings)
            record_pass_result(conn, events, status="ok", error="",
                               next_run_at=next_run_at, now=now)
        except Exception as exc:  # noqa: BLE001 - a bad pass must not kill the loop
            logger.exception("scheduled pass failed; will retry next interval")
            record_pass_result(conn, [], status="error", error=type(exc).__name__,
                               next_run_at=next_run_at, now=now)
            if warn_fn is not None:
                check_health(conn, settings, warn=warn_fn, now=now_dt)
            return []
        try:
            backup_if_due(conn, settings.db_path, settings.db_backup_keep, now=now_dt)
        except Exception:  # noqa: BLE001 - backup is best-effort, never fatal
            logger.warning("periodic backup failed", exc_info=True)
        return events
    finally:
        conn.close()


def startup_backup(conn_factory: ConnFactory, settings) -> None:
    """Take a due-based snapshot at startup (best-effort; never blocks start)."""
    try:
        conn = conn_factory()
        try:
            backup_if_due(conn, settings.db_path, settings.db_backup_keep)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - startup backup must never prevent serving
        logger.warning("startup backup failed", exc_info=True)


def start_background(pass_fn: PassFn, settings, *, conn_factory: ConnFactory,
                     warn_fn: WarnFn | None = None,
                     ) -> tuple[threading.Thread, threading.Event]:
    """Start :func:`run_scheduler` on a daemon thread; return (thread, stop_event).

    Used by ``serve`` (Decision A): the CIAN loop runs alongside the dashboard in
    one process. A daemon thread is correct here — the loop uses *sync* Playwright,
    which must never run on the asyncio event loop, and a plain thread is neither
    the loop nor the request threadpool. The returned event stops it on shutdown.
    """
    stop = threading.Event()

    def _loop() -> None:
        try:
            run_scheduler(pass_fn, settings, conn_factory=conn_factory,
                          stop_event=stop, warn_fn=warn_fn)
        except Exception:  # noqa: BLE001 - never let the thread die silently
            logger.exception("background scheduler thread crashed")

    thread = threading.Thread(target=_loop, name="cian-scheduler", daemon=True)
    thread.start()
    logger.info("background CIAN scheduler started (every %dh)",
                max(1, settings.poll_interval_hours))
    return thread, stop
