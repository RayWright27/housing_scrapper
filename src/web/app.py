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

``POST /api/refresh`` runs one CIAN pass on demand (server-side) and kicks off
the interactive Avito grab via another injected seam (``launch_avito_grab``):
Avito needs a human to load its tabs / solve a challenge, so the grab runs in a
separate console — the web request cannot do that headlessly (§7).
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
GrabLauncher = Callable[[], bool]                   # kick off the Avito grab; True if started


class AddBody(BaseModel):
    url: str
    note: str | None = None


def _no_notify(conn, events) -> None:
    """Default notify seam: do nothing (used by offline tests)."""


def _no_grab() -> bool:
    """Default Avito-grab launcher: do nothing (used by offline tests)."""
    return False


def create_app(
    *,
    get_conn: ConnFactory,
    build_adapter: AdapterFactory,
    settings,
    notify: NotifySink = _no_notify,
    launch_avito_grab: GrabLauncher = _no_grab,
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

    @app.post("/api/refresh")
    def api_refresh() -> dict:
        """Run one CIAN tracking pass now (like `run-once`) and kick off the
        interactive Avito grab. CIAN is fetched server-side; Avito needs a human
        to load its tabs / solve a challenge, so we launch `avito-grab.ps1` in a
        separate console where you finish it — the web request cannot do that
        part headlessly (§7)."""
        with get_conn() as conn:
            with build_adapter("cian") as adapter:
                events = []
                if adapter is not None:
                    events = tracker.run_once({"cian": adapter}, conn, settings)
            # Notify on what the pass found (guarded — see api_add).
            try:
                notify(conn, events)
            except Exception:  # noqa: BLE001 - delivery must not break refresh
                logger.exception("notify seam raised during refresh; ignoring")
            fetched = sum(1 for e in events if e.type == tracker.EventType.NOW_TRACKING)
            changed = sum(1 for e in events if e.type == tracker.EventType.PRICE_CHANGED)
        avito_launched = False
        try:
            avito_launched = bool(launch_avito_grab())
        except Exception:  # noqa: BLE001 - a launch failure must not fail refresh
            logger.exception("failed to launch the Avito grab")
        return {"cian_events": len(events), "fetched": fetched, "changed": changed,
                "avito_launched": avito_launched}

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

    def launch_avito_grab() -> bool:
        """Open the interactive Avito grab (scripts/avito-grab.ps1) in its own
        console window, so the user can load the tabs / solve a challenge and
        press Enter there — exactly as running the script by hand."""
        import subprocess
        import sys

        script = Path(__file__).resolve().parents[2] / "scripts" / "avito-grab.ps1"
        if not script.exists():
            logger.warning("avito-grab.ps1 not found at %s; skipping Avito", script)
            return False
        # CREATE_NEW_CONSOLE (0x10) gives the interactive script its own window.
        creationflags = 0x00000010 if sys.platform == "win32" else 0
        subprocess.Popen(  # noqa: S603 - fixed local script, no user input
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=str(script.parents[1]),
            creationflags=creationflags,
        )
        return True

    return create_app(get_conn=get_conn, build_adapter=build_adapter,
                       settings=settings, notify=notify,
                       launch_avito_grab=launch_avito_grab)
