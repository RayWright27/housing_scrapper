"""Dashboard tests — pure computed fields + endpoint shape, offline (no network)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.adapters.base import RawListing
from src.storage import repository as repo
from src.storage.db import init_db
from src.web import service

NOW = "2026-07-04T12:00:00+00:00"


# --------------------------------------------------------------------------- #
# pure numeric helpers
# --------------------------------------------------------------------------- #
def obs(*pairs):
    return [{"price": p, "observed_at": t} for p, t in pairs]


def test_price_per_m2_rounds_and_handles_missing_area() -> None:
    assert service.price_per_m2(14_800_000, 56.6) == 261_484
    assert service.price_per_m2(14_800_000, None) is None
    assert service.price_per_m2(14_800_000, 0) is None
    assert service.price_per_m2(None, 56.6) is None


def test_delta_total_pct_sign_and_rounding() -> None:
    assert service.delta_total_pct(10_000_000, 9_000_000) == -10.0
    assert service.delta_total_pct(8_000_000, 10_000_000) == 25.0
    assert service.delta_total_pct(None, 5) is None
    assert service.delta_total_pct(0, 5) is None


def test_days_since_change_none_for_single_observation() -> None:
    assert service.days_since_change(obs((100, "2026-07-01T12:00:00+00:00")), NOW) is None
    two = obs((100, "2026-06-01T12:00:00+00:00"), (90, "2026-07-01T12:00:00+00:00"))
    assert service.days_since_change(two, NOW) == 3


def test_min_max_and_change_count() -> None:
    o = obs((100, "a"), (120, "b"), (90, "c"))
    assert service.min_max(o) == (90, 120)
    assert service.change_count(o) == 2
    assert service.min_max([]) == (None, None)
    assert service.change_count([]) == 0
    assert service.change_count(obs((100, "a"))) == 0  # first-seen only, no change


def test_detect_source() -> None:
    assert service.detect_source("https://www.cian.ru/sale/flat/1/") == "cian"
    assert service.detect_source("https://spb.cian.ru/x") == "cian"
    assert service.detect_source("https://m2.cian.ru/x") == "cian"
    assert service.detect_source("https://www.avito.ru/x") == "avito"
    assert service.detect_source("https://example.com/x") is None


# --------------------------------------------------------------------------- #
# service functions against an in-memory DB
# --------------------------------------------------------------------------- #
@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _seed(conn, ext, area, prices_at):
    lid = repo.upsert_listing(conn, source="cian", external_id=ext,
                              url=f"https://cian.ru/sale/flat/{ext}/", now=NOW,
                              rooms=2, area_total=area)
    for price, at in prices_at:
        repo.record_price(conn, lid, price, at)
    return lid


def test_listing_rows_computes_fields(conn) -> None:
    src = repo.add_tracked_source(conn, "cian", "u", "listing", note="mine")
    lid = _seed(conn, "1", 50.0, [(10_000_000, "2026-06-01T12:00:00+00:00"),
                                  (9_000_000, "2026-07-01T12:00:00+00:00")])
    repo.link_source_listing(conn, src, lid)

    row = next(r for r in service.listing_rows(conn, now=NOW) if r["id"] == lid)
    assert row["current_price"] == 9_000_000
    assert row["price_per_m2"] == 180_000        # 9_000_000 / 50
    assert row["delta_total_pct"] == -10.0
    assert row["days_since_change"] == 3
    assert row["change_count"] == 1
    assert row["min_price"] == 9_000_000 and row["max_price"] == 10_000_000
    assert row["note"] == "mine" and row["tracked_source_id"] == src
    assert row["status"] == "active"


def test_summary_counts_changes_in_7d(conn) -> None:
    # listing A: first-seen 30d ago, changed 2d ago  -> 1 change in window
    _seed(conn, "A", 50.0, [(100, "2026-06-04T12:00:00+00:00"),
                            (110, "2026-07-02T12:00:00+00:00")])
    # listing B: first-seen 1d ago (baseline only)   -> 0 changes in window
    _seed(conn, "B", 50.0, [(200, "2026-07-03T12:00:00+00:00")])
    s = service.summary(conn, now=NOW)
    assert s["total"] == 2 and s["active"] == 2 and s["delisted"] == 0
    assert s["changes_7d"] == 1


def test_history_prepends_pre_window_anchor(conn) -> None:
    lid = _seed(conn, "1", 50.0, [(100, "2026-01-01T12:00:00+00:00"),   # far before
                                  (120, "2026-07-03T12:00:00+00:00")])  # in 30d window
    h = service.history(conn, lid, "30d", now=NOW)
    prices = [o["price"] for o in h["observations"]]
    assert prices == [100, 120]  # the old 100 is kept as the window's starting value
    assert h["area_total"] == 50.0
    assert service.history(conn, 999, "all", now=NOW) is None


def test_scheduler_status_unknown_then_known(conn) -> None:
    from src import scheduler as sched

    # Fresh DB: no pass has run, so the status is neutral/unknown.
    st = service.scheduler_status(conn)
    assert st["known"] is False and st["last_run_at"] is None

    # After a recorded pass the heartbeat surfaces for the dashboard indicator.
    sched.record_pass_result(conn, ["e1"], status="ok", error="",
                             next_run_at="2026-07-04T18:00:00+00:00", now=NOW)
    st = service.scheduler_status(conn)
    assert st["known"] is True
    assert st["last_run_at"] == NOW and st["last_status"] == "ok"
    assert st["last_events"] == 1 and st["last_error"] is None


def test_delisted_listing_flagged(conn) -> None:
    lid = _seed(conn, "1", 50.0, [(100, NOW)])
    conn.execute("UPDATE listings SET is_active = 0 WHERE id = ?", (lid,))
    conn.commit()
    row = service.listing_rows(conn, now=NOW)[0]
    assert row["status"] == "delisted"


# --------------------------------------------------------------------------- #
# endpoint shape via TestClient (fake adapter, in-memory DB, no network)
# --------------------------------------------------------------------------- #
class FakeCian:
    source = "cian"

    def classify_url(self, url):
        return "listing"

    def fetch_listing(self, url):
        return RawListing(source="cian", external_id="777", url=url, price=5_000_000,
                          area_total=40.0, rooms=1)

    def fetch_search(self, url):
        return []


def _client(conn):
    from fastapi.testclient import TestClient

    from src.web.app import create_app

    @contextmanager
    def get_conn():
        yield conn

    @contextmanager
    def build_adapter(source):
        yield FakeCian() if source == "cian" else None

    settings = SimpleNamespace(delist_after_misses=3)
    return TestClient(create_app(get_conn=get_conn, build_adapter=build_adapter,
                                 settings=settings))


def test_endpoints_and_post_immediate_fetch(conn) -> None:
    client = _client(conn)

    assert client.get("/api/summary").json() == {
        "total": 0, "active": 0, "delisted": 0, "changes_7d": 0}
    assert client.get("/api/listings").json() == []

    # POST detects source, inserts, and fetches once via the fake adapter
    r = client.post("/api/tracked", json={"url": "https://spb.cian.ru/sale/flat/777/",
                                          "note": "test"})
    body = r.json()
    assert r.status_code == 200 and body["source"] == "cian" and body["fetched"] == 1

    listings = client.get("/api/listings").json()
    assert len(listings) == 1 and listings[0]["external_id"] == "777"
    assert listings[0]["price_per_m2"] == 125_000  # 5_000_000 / 40

    hist = client.get(f"/api/listings/{listings[0]['id']}/history").json()
    assert len(hist["observations"]) == 1 and hist["observations"][0]["price"] == 5_000_000

    # DELETE deactivates the tracked source (history kept)
    sid = listings[0]["tracked_source_id"]
    assert client.delete(f"/api/tracked/{sid}").status_code == 200
    assert client.get("/api/listings").json()[0]["current_price"] == 5_000_000  # kept


def test_target_delta_pct() -> None:
    assert service.target_delta_pct(11_000_000, 10_000_000) == 10.0   # 10% above
    assert service.target_delta_pct(9_500_000, 10_000_000) == -5.0    # below target
    assert service.target_delta_pct(None, 10_000_000) is None
    assert service.target_delta_pct(10_000_000, None) is None
    assert service.target_delta_pct(10_000_000, 0) is None


def test_set_and_clear_target_endpoints(conn) -> None:
    client = _client(conn)
    client.post("/api/tracked", json={"url": "https://spb.cian.ru/sale/flat/777/"})
    lid = client.get("/api/listings").json()[0]["id"]

    # No target initially.
    assert client.get("/api/listings").json()[0]["target_price"] is None

    # Set it -> row exposes target_price + distance (current 5M vs target 4M = +25%).
    assert client.put(f"/api/listings/{lid}/target",
                      json={"target_price": 4_000_000}).status_code == 200
    row = client.get("/api/listings").json()[0]
    assert row["target_price"] == 4_000_000 and row["target_delta_pct"] == 25.0

    # A non-positive target is rejected; an unknown listing is 404.
    assert client.put(f"/api/listings/{lid}/target",
                      json={"target_price": 0}).status_code == 400
    assert client.put("/api/listings/9999/target",
                      json={"target_price": 5}).status_code == 404

    # Clear it.
    assert client.delete(f"/api/listings/{lid}/target").status_code == 200
    assert client.get("/api/listings").json()[0]["target_price"] is None


def test_target_reaches_notifier_via_meta_lookup(conn) -> None:
    # A set target must flow into the notifier's ListingMeta (distance-to-target
    # line) through the shared get_meta seam — not just the dashboard.
    from src.notify.sink import make_meta_lookup

    lid = repo.upsert_listing(conn, source="cian", external_id="1", url="u", now=NOW,
                              area_total=50.0)
    repo.set_target(conn, lid, 12_000_000, NOW)
    meta = make_meta_lookup(conn)(lid)
    assert meta is not None and meta.target_price == 12_000_000


def test_post_unknown_host_is_400(conn) -> None:
    r = _client(conn).post("/api/tracked", json={"url": "https://example.com/x"})
    assert r.status_code == 400
    assert "cian.ru" in r.json()["detail"]


def _client_with_notify(conn, recorder):
    from fastapi.testclient import TestClient

    from src.web.app import create_app

    @contextmanager
    def get_conn():
        yield conn

    @contextmanager
    def build_adapter(source):
        yield FakeCian() if source == "cian" else None

    settings = SimpleNamespace(delist_after_misses=3)
    return TestClient(create_app(get_conn=get_conn, build_adapter=build_adapter,
                                 settings=settings, notify=recorder))


def test_dashboard_add_notifies_via_injected_seam(conn) -> None:
    # The add's immediate fetch must hand its events to the notify seam so a
    # newly-tracked listing pings Telegram (or queues to the outbox offline).
    seen: list = []

    def recorder(_conn, events) -> None:
        seen.extend(events)

    client = _client_with_notify(conn, recorder)
    r = client.post("/api/tracked", json={"url": "https://spb.cian.ru/sale/flat/777/"})
    assert r.status_code == 200
    types = {e.type.value for e in seen}
    assert "now_tracking" in types  # the new listing was announced to the seam


def test_refresh_runs_cian_pass_and_launches_avito(conn) -> None:
    from fastapi.testclient import TestClient

    from src.web.app import create_app

    @contextmanager
    def get_conn():
        yield conn

    @contextmanager
    def build_adapter(source):
        yield FakeCian() if source == "cian" else None

    launched = []
    settings = SimpleNamespace(delist_after_misses=3)
    app = create_app(get_conn=get_conn, build_adapter=build_adapter, settings=settings,
                     launch_avito_grab=lambda: (launched.append(1), True)[1])
    client = TestClient(app)

    # a tracked CIAN listing that the pass will fetch via the fake adapter
    repo.add_tracked_source(conn, "cian", "https://spb.cian.ru/sale/flat/777/", "listing")
    # an active Avito source so the grab launcher is invoked (guarded otherwise)
    repo.add_tracked_source(conn, "avito", "https://www.avito.ru/x_9", "listing")

    body = client.post("/api/refresh").json()
    assert body["fetched"] == 1                # the CIAN pass ran and found it
    assert body["avito_launched"] is True      # the Avito grab was kicked off
    assert launched == [1]
    assert client.get("/api/listings").json()[0]["external_id"] == "777"


def test_refresh_skips_avito_grab_when_no_avito_tracked(conn) -> None:
    from fastapi.testclient import TestClient

    from src.web.app import create_app

    @contextmanager
    def get_conn():
        yield conn

    @contextmanager
    def build_adapter(source):
        yield FakeCian() if source == "cian" else None

    launched = []
    settings = SimpleNamespace(delist_after_misses=3)
    app = create_app(get_conn=get_conn, build_adapter=build_adapter, settings=settings,
                     launch_avito_grab=lambda: (launched.append(1), True)[1])
    client = TestClient(app)
    repo.add_tracked_source(conn, "cian", "https://spb.cian.ru/sale/flat/777/", "listing")

    body = client.post("/api/refresh").json()
    assert body["avito_launched"] is False     # nothing to grab -> not launched
    assert launched == []                      # the console is not opened


def test_dashboard_add_notify_failure_does_not_break_add(conn) -> None:
    # A delivery problem in the seam must never fail the add itself: the source
    # is already persisted, so the endpoint guards the notify call defensively.
    def boom(_conn, _events) -> None:
        raise RuntimeError("telegram exploded")

    client = _client_with_notify(conn, boom)
    r = client.post("/api/tracked", json={"url": "https://spb.cian.ru/sale/flat/777/"})
    assert r.status_code == 200 and r.json()["fetched"] == 1  # add still succeeded
    assert len(client.get("/api/listings").json()) == 1       # and was persisted
