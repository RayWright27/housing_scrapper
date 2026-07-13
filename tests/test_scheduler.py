"""Scheduler-loop + heartbeat + daily-backup tests — offline, no real sleeping.

The loop is driven with a fake pass and an on-disk temp DB; ``stop_event`` ends it
after one iteration so nothing blocks. No network, no Playwright, no wall-clock
waits (CLAUDE.md §13)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import scheduler
from src.storage import repository as repo
from src.storage.db import connect, init_db


def _settings(db_path: str):
    return SimpleNamespace(poll_interval_hours=6, db_path=db_path, db_backup_keep=3)


def _conn_factory(db_path: str):
    def factory():
        c = connect(db_path)
        c.execute("PRAGMA foreign_keys = ON;")
        return c
    return factory


# --------------------------------------------------------------------------- #
# app_meta + targets round-trip
# --------------------------------------------------------------------------- #
def test_meta_set_get_roundtrip() -> None:
    conn = init_db(":memory:")
    assert repo.get_meta(conn, "x") is None
    repo.set_meta(conn, "x", "1")
    repo.set_meta(conn, "x", "2")          # upsert overwrites
    assert repo.get_meta(conn, "x") == "2"
    repo.set_meta(conn, "y", "z")
    assert repo.get_all_meta(conn) == {"x": "2", "y": "z"}
    conn.close()


def test_targets_set_clear_get() -> None:
    conn = init_db(":memory:")
    lid = repo.upsert_listing(conn, source="cian", external_id="1",
                              url="u", now="2026-07-01T00:00:00+00:00")
    assert repo.get_target(conn, lid) is None
    repo.set_target(conn, lid, 14_000_000, "2026-07-01T00:00:00+00:00")
    assert repo.get_target(conn, lid) == 14_000_000
    repo.set_target(conn, lid, 13_500_000, "2026-07-02T00:00:00+00:00")  # replace
    assert repo.get_target(conn, lid) == 13_500_000
    assert repo.get_all_targets(conn) == {lid: 13_500_000}
    repo.clear_target(conn, lid)
    assert repo.get_target(conn, lid) is None
    conn.close()


# --------------------------------------------------------------------------- #
# heartbeat
# --------------------------------------------------------------------------- #
def test_record_pass_result_writes_heartbeat() -> None:
    conn = init_db(":memory:")
    scheduler.record_pass_result(conn, [1, 2, 3], status="ok", error="",
                                 next_run_at="2026-07-01T06:00:00+00:00",
                                 now="2026-07-01T00:00:00+00:00")
    m = repo.get_all_meta(conn)
    assert m[scheduler.LAST_STATUS] == "ok"
    assert m[scheduler.LAST_EVENTS] == "3"
    assert m[scheduler.NEXT_RUN_AT] == "2026-07-01T06:00:00+00:00"
    conn.close()


# --------------------------------------------------------------------------- #
# daily (due-based) backup
# --------------------------------------------------------------------------- #
def test_backup_if_due_respects_interval(tmp_path) -> None:
    db_file = tmp_path / "tracker.db"
    conn = init_db(str(db_file))
    repo.add_tracked_source(conn, "cian", "u", "listing")

    first = scheduler.backup_if_due(conn, str(db_file), keep=3)
    assert first is not None                                   # nothing recorded yet
    assert repo.get_meta(conn, scheduler.LAST_BACKUP_AT) is not None

    # Immediately after, a backup is not due -> skipped.
    assert scheduler.backup_if_due(conn, str(db_file), keep=3) is None

    # 25h later it is due again.
    future = datetime.now(timezone.utc) + timedelta(hours=25)
    assert scheduler.backup_if_due(conn, str(db_file), keep=3, now=future) is not None
    conn.close()


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #
def test_run_scheduler_runs_one_pass_then_stops(tmp_path) -> None:
    db_file = tmp_path / "tracker.db"
    init_db(str(db_file)).close()
    settings = _settings(str(db_file))
    stop = threading.Event()
    calls: list = []

    def pass_fn(conn, _settings):
        calls.append(1)
        stop.set()                          # end the loop after this single pass
        return ["ev1", "ev2"]

    scheduler.run_scheduler(pass_fn, settings, conn_factory=_conn_factory(str(db_file)),
                            stop_event=stop, on_startup_backup=False)

    assert calls == [1]                     # exactly one pass ran
    conn = connect(str(db_file))
    assert repo.get_meta(conn, scheduler.LAST_STATUS) == "ok"
    assert repo.get_meta(conn, scheduler.LAST_EVENTS) == "2"
    conn.close()


def test_run_scheduler_records_error_and_survives(tmp_path) -> None:
    db_file = tmp_path / "tracker.db"
    init_db(str(db_file)).close()
    settings = _settings(str(db_file))
    stop = threading.Event()

    def boom(conn, _settings):
        stop.set()
        raise RuntimeError("scrape blew up")

    # A failing pass must be recorded, not raised (the loop has to survive).
    scheduler.run_scheduler(boom, settings, conn_factory=_conn_factory(str(db_file)),
                            stop_event=stop, on_startup_backup=False)

    conn = connect(str(db_file))
    assert repo.get_meta(conn, scheduler.LAST_STATUS) == "error"
    assert repo.get_meta(conn, scheduler.LAST_ERROR) == "RuntimeError"
    conn.close()
