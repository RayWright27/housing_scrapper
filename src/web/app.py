"""FastAPI app for the local dashboard (CLAUDE.md §9a).

Thin presentation layer: it reads via :mod:`src.web.service` and its only writes
are add/deactivate on ``tracked_sources``. The POST immediate-fetch reuses the
existing single-source pipeline seam (:func:`src.tracker.process_source`) — no
scraping or business logic is duplicated here.

The app is built via :func:`create_app` with injected seams so tests can drive
it with an in-memory DB and a fake adapter (no network). Endpoints are sync
``def`` so FastAPI runs them in a threadpool — the CIAN adapter uses *sync*
Playwright, which must never run inside the asyncio event loop.

The dashboard add's immediate fetch also NOTIFIES: the events it persists are
handed to an injected ``notify`` seam (built from :mod:`src.notify.sink` at the
composition root), so a newly-tracked listing pings Telegram just like a
``run-once`` pass — respecting ``NOTIFY_ON_NEW`` and, with no connectivity,
queueing to the outbox for later. The web layer stays thin: it does not format
or send anything itself, only calls the injected seam.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, ContextManager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src import tracker
from src.storage import repository as repo
from src.web import service

logger = logging.getLogger("realty.web")
STATIC_DIR = Path(__file__).parent / "static"

ConnFactory = Callable[[], ContextManager]
AdapterFactory = Callable[[str], ContextManager]  # source -> ctx yielding adapter|None
NotifySink = Callable[[object, list], None]        # (conn, events) -> None


class AddBody(BaseModel):
    url: str
    note: str | None = None


def _no_notify(conn, events) -> None:
    """Default notify seam: do nothing (used by offline tests)."""


def create_app(
    *,
    get_conn: ConnFactory,
    build_adapter: AdapterFactory,
    settings,
    notify: NotifySink = _no_notify,
) -> FastAPI:
    app = FastAPI(title="Realty Tracker", docs_url=None, redoc_url=None)

    @app.get("/api/summary")
    def api_summary() -> dict:
        with get_conn() as conn:
            return service.summary(conn)

    @app.get("/api/listings")
    def api_listings() -> list[dict]:
        with get_conn() as conn:
            return service.listing_rows(conn)

    @app.get("/api/listings/{listing_id}/history")
    def api_history(listing_id: int, range: str = "all") -> dict:
        if range not in ("all", "90d", "30d"):
            range = "all"
        with get_conn() as conn:
            data = service.history(conn, listing_id, range)
        if data is None:
            raise HTTPException(status_code=404, detail="listing not found")
        return data

    @app.post("/api/tracked")
    def api_add(body: AddBody) -> dict:
        source = service.detect_source(body.url)
        if source is None:
            raise HTTPException(
                status_code=400,
                detail="Unrecognized host — expected a cian.ru or avito.ru URL.",
            )
        with get_conn() as conn:
            # Build the adapter (may be a real sync-Playwright browser) inside the
            # threadpool-run sync endpoint, never on the event loop.
            with build_adapter(source) as adapter:
                classify = getattr(adapter, "classify_url", None)
                kind = classify(body.url) if classify else "listing"
                tracked_id = repo.add_tracked_source(
                    conn, source=source, url=body.url, kind=kind, note=body.note
                )
                src_row = _tracked_row(conn, tracked_id)
                events = []
                if adapter is not None:
                    events = tracker.process_source(
                        {source: adapter}, conn, settings, src_row,
                        now=service._now_iso(),
                    )
            # Push notifications for what the immediate fetch found, while the
            # connection is still open (the seam's outbox/get_meta read it). The
            # source is already persisted, so a notification problem must never
            # fail the add — guard defensively even though the seam swallows.
            try:
                notify(conn, events)
            except Exception:  # noqa: BLE001 - delivery must not break the add
                logger.exception("notify seam raised during add; ignoring")
            fetched = sum(1 for e in events if e.type == tracker.EventType.NOW_TRACKING)
        if adapter is None:
            message = (f"Added to the watchlist. {source} is tracked via "
                       "`grab` or `ingest` (auto-fetch is off).")
        elif fetched:
            message = f"Added and fetched {fetched} listing(s)."
        else:
            message = ("Added, but couldn't fetch yet (blocked or empty). "
                       "It will fill in on the next run.")
        return {"tracked_id": tracked_id, "source": source, "kind": kind,
                "fetched": fetched, "message": message}

    @app.delete("/api/tracked/{tracked_id}")
    def api_remove(tracked_id: int) -> dict:
        with get_conn() as conn:
            existing = {r["id"] for r in repo.get_tracked(conn, active_only=False)}
            if tracked_id not in existing:
                raise HTTPException(status_code=404, detail="tracked source not found")
            repo.deactivate_tracked_source(conn, tracked_id)
        return {"id": tracked_id, "status": "deactivated"}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def _tracked_row(conn, tracked_id: int):
    for row in repo.get_tracked(conn, active_only=False):
        if row["id"] == tracked_id:
            return row
    raise HTTPException(status_code=500, detail="tracked source vanished after insert")


# --------------------------------------------------------------------------- #
# production composition root — used by `python -m src.main serve`
# --------------------------------------------------------------------------- #
def build_production_app():
    """Wire the app to the real DB and the live CIAN adapter (loopback use)."""
    from config import settings
    from src.notify import sink
    from src.storage import db

    @contextmanager
    def get_conn():
        conn = db.connect(settings.db_path)
        db.bootstrap(conn)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def build_adapter(source: str):
        if source == "cian":
            from src.adapters.cian import CianAdapter
            with CianAdapter() as adapter:  # sync Playwright; endpoint is threadpooled
                yield adapter
        else:
            # Avito is not auto-fetched (firewall/§7); the add is still recorded,
            # and Avito is tracked via `grab` / `ingest`.
            yield None

    def notify(conn, events) -> None:
        sink.notify_events(conn, events, settings)

    return create_app(get_conn=get_conn, build_adapter=build_adapter,
                       settings=settings, notify=notify)
