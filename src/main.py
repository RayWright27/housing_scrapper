"""Realty Tracker CLI entrypoint.

Dev/debug commands (smoke-test the adapter without the tracker):

    python -m src.main fetch   --source cian --url "<listing-or-search-url>"
    python -m src.main capture --source cian --url "<url>" --out <path>

Operational commands:

    python -m src.main init-db
    python -m src.main add --source cian --url "<url>" --kind listing --note "..."
    python -m src.main run-once

Remaining §11 commands (run, serve) belong to their own build-order phases.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

logger = logging.getLogger("realty.cli")

_ADAPTERS = {"cian", "avito"}


def _get_adapter(source: str):
    if source == "cian":
        from src.adapters.cian import CianAdapter

        return CianAdapter()
    if source == "avito":
        from src.adapters.avito import AvitoAdapter

        return AvitoAdapter()
    raise SystemExit(f"unknown/unsupported source: {source!r} (have: {sorted(_ADAPTERS)})")


def _open_db():
    """Open the configured DB and ensure the schema exists (idempotent)."""
    from config import settings
    from src.storage.db import bootstrap, connect

    conn = connect(settings.db_path)
    bootstrap(conn)
    return conn


# --------------------------------------------------------------------------- #
# fetch (dev/debug): fetch once and pretty-print the RawListing(s)
# --------------------------------------------------------------------------- #
def cmd_fetch(args: argparse.Namespace) -> int:
    adapter = _get_adapter(args.source)
    with adapter:
        if args.search:
            items = adapter.fetch_search(args.url)
            if not items:
                print("(no results — blocked, removed, or empty search)")
                return 1
            print(f"{len(items)} listing(s):\n")
            for raw in items:
                _print_raw(raw)
            return 0
        raw = adapter.fetch_listing(args.url)
        if raw is None:
            print(
                "SOFT FAILURE: no RawListing returned "
                "(blocked/captcha, removed/404, unsupported url, or missing price). "
                "See the log lines above for which."
            )
            return 1
        _print_raw(raw)
        return 0


def _print_raw(raw) -> None:
    d = dataclasses.asdict(raw)
    extra = d.pop("extra", None) or {}
    print("RawListing:")
    for key, value in d.items():
        print(f"  {key:12} = {value!r}")
    if extra:
        print("  extra:")
        for key, value in extra.items():
            print(f"    {key:10} = {value!r}")
    print()


# --------------------------------------------------------------------------- #
# capture (dev/debug): save the raw page payload, redacting personal data first
# --------------------------------------------------------------------------- #
def cmd_capture(args: argparse.Namespace) -> int:
    from src.adapters.avito import redact as avito_redact
    from src.adapters.cian import redact as cian_redact

    redactors = {"cian": cian_redact, "avito": avito_redact}
    adapter = _get_adapter(args.source)
    with adapter:
        html, final_url = adapter.fetch_raw(args.url)
    if html is None:
        print("SOFT FAILURE: could not fetch the page (timeout/block). Nothing saved.")
        return 1
    redacted = redactors[args.source](html)  # site-specific PII redaction lives in the adapter
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(redacted, encoding="utf-8")
    print(f"saved redacted capture ({len(redacted):,} bytes) -> {out}")
    print(f"final url: {final_url}")
    print("NOTE: redaction is best-effort — review before committing as a fixture.")
    return 0


# --------------------------------------------------------------------------- #
# init-db: create the database file from the schema
# --------------------------------------------------------------------------- #
def cmd_init_db(args: argparse.Namespace) -> int:
    from config import settings

    conn = _open_db()
    conn.close()
    print(f"database ready at {settings.db_path}")
    return 0


# --------------------------------------------------------------------------- #
# add: register one listing/search URL in tracked_sources
# --------------------------------------------------------------------------- #
def cmd_add(args: argparse.Namespace) -> int:
    from src.storage import repository as repo

    conn = _open_db()
    tracked_id = repo.add_tracked_source(
        conn, source=args.source, url=args.url, kind=args.kind, note=args.note
    )
    conn.close()
    print(f"added tracked_source #{tracked_id}: {args.source} {args.kind} {args.url}")
    return 0


# --------------------------------------------------------------------------- #
# list: show the watchlist (tracked_sources) with status + linked-listing count
# --------------------------------------------------------------------------- #
def cmd_list(args: argparse.Namespace) -> int:
    from src.storage import repository as repo

    conn = _open_db()
    rows = repo.get_tracked(conn, active_only=False)
    if not rows:
        print("no tracked sources. add one with: python -m src.main add --source cian --url ...")
    else:
        print(f"{len(rows)} tracked source(s):\n")
        for r in rows:
            status = "active" if r["active"] else "OFF   "
            n = repo.count_active_links(conn, r["id"])
            note = f"  «{r['note']}»" if r["note"] else ""
            print(f"  #{r['id']:<3} {status}  {r['source']:5} {r['kind']:7} "
                  f"{n:>3} listing(s){note}")
            print(f"        {r['url']}")
    conn.close()
    return 0


# --------------------------------------------------------------------------- #
# remove: deactivate (default) or --purge delete a tracked source. History kept.
# --------------------------------------------------------------------------- #
def cmd_remove(args: argparse.Namespace) -> int:
    from src.storage import repository as repo

    conn = _open_db()
    rows = {r["id"]: r for r in repo.get_tracked(conn, active_only=False)}
    row = rows.get(args.id)
    if row is None:
        conn.close()
        print(f"no tracked source with id {args.id}. Run 'list' to see ids.")
        return 1
    if args.purge:
        repo.delete_tracked_source(conn, args.id)
        print(f"purged tracked_source #{args.id} ({row['kind']} {row['url']}). "
              "Listing price history is kept.")
    else:
        repo.deactivate_tracked_source(conn, args.id)
        print(f"deactivated tracked_source #{args.id} ({row['kind']} {row['url']}). "
              "It will be skipped on the next run; use --purge to delete the row.")
    conn.close()
    return 0


# --------------------------------------------------------------------------- #
# run-once: one full tracking pass. The ONLY place the live adapter is built.
# --------------------------------------------------------------------------- #
def cmd_run_once(args: argparse.Namespace) -> int:
    from config import settings
    from src.adapters.cian import CianAdapter
    from src.notify import sink
    from src.tracker import run_once

    conn = _open_db()
    try:
        # Only CIAN is auto-fetched. Avito is intentionally excluded: its firewall
        # blocks automation and repeated attempts flag the IP (§7). Avito is
        # tracked via `grab` (read your open tabs) or `ingest` (a saved page).
        with CianAdapter() as cian:
            adapters = {"cian": cian}
            events = run_once(adapters, conn, settings)

        # Deliver notifications AFTER persistence, while the DB is still open so
        # the notifier's injected get_meta can read listing details (§9b). The
        # notifier is built only at this seam — never inside tracker.py.
        sink.notify_events(conn, events, settings)
    finally:
        conn.close()

    if not events:
        print("no events (nothing new, no price changes, no delistings)")
        return 0
    print(f"{len(events)} event(s):\n")
    _print_events(events)
    return 0


def _print_events(events) -> None:
    for ev in events:
        if ev.type.value == "price_changed":
            print(f"  PRICE_CHANGED  {ev.source} {ev.external_id}: "
                  f"{ev.old_price:,} -> {ev.new_price:,} ({ev.delta:+,}, "
                  f"{ev.percent:+.1f}%)  {ev.url}")
        elif ev.type.value == "now_tracking":
            print(f"  NOW_TRACKING   {ev.source} {ev.external_id}: "
                  f"{ev.price:,}  {ev.url}")
        else:
            print(f"  DELISTED       {ev.source} {ev.external_id}  {ev.url}")


def _ensure_tracked_source(conn, source: str, url: str, kind: str, note: str | None):
    """Return the tracked_source for (source, url), creating it if absent."""
    from src.storage import repository as repo

    for row in repo.get_tracked(conn, active_only=False):
        if row["source"] == source and row["url"] == url:
            return row
    tracked_id = repo.add_tracked_source(conn, source, url, kind, note)
    return next(r for r in repo.get_tracked(conn, active_only=False)
               if r["id"] == tracked_id)


# --------------------------------------------------------------------------- #
# warmup: open a headful, persistent-profile browser and WAIT so you can solve
# a challenge / sign in once. The cookie is saved into the profile dir, so the
# dashboard and run-once (same profile) reuse it. Best-effort (§7): you solve
# the challenge by hand — nothing is defeated in code.
# --------------------------------------------------------------------------- #
def cmd_warmup(args: argparse.Namespace) -> int:
    from config import settings

    if not settings.browser_user_data_dir:
        print("Set BROWSER_USER_DATA_DIR (a dedicated folder) and BROWSER_HEADLESS=0 "
              "in .env first, then re-run. Also stop `serve` — it locks the profile.")
        return 1
    if settings.browser_headless:
        print("BROWSER_HEADLESS=0 is required so the window is visible. Set it and re-run.")
        return 1

    from src.adapters.avito import USER_AGENT
    from src.adapters.browser import close_context, launch_context

    pw, browser, ctx = launch_context(settings, USER_AGENT)
    page = ctx.new_page()
    try:
        page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:  # noqa: BLE001 - just report; the window is still usable
        print(f"(navigation note: {type(exc).__name__}) — the window is open anyway.")
    print(f"\nA browser window is open at:\n  {args.url}\n")
    print("Solve any captcha / sign in, open a real listing so it loads normally,")
    print("then return here and press Enter to save cookies and close.")
    try:
        input()
    except EOFError:
        pass
    close_context(pw, browser, ctx)
    print(f"\nSaved browser state to {settings.browser_user_data_dir}.")
    print("Now start `serve` (or run `run-once`) — it reuses this profile's cookies.")
    return 0


# --------------------------------------------------------------------------- #
# ingest (fallback): parse a hand-saved HTML page and track it — no fetching.
# For sites that hard-block automation (Avito, §7), you save the page in your
# real browser and feed it here; the parse->normalize->track->notify pipeline is
# reused verbatim.
# --------------------------------------------------------------------------- #
def _recover_view_source(html: str) -> str:
    """Unwrap a browser 'view-source' save back to real HTML, if needed."""
    if 'class="html-tag"' in html or 'class="line-content"' in html:
        import html as _html
        import re as _re

        return _html.unescape(_re.sub(r"<[^>]+>", "", html))
    return html


def cmd_ingest(args: argparse.Namespace) -> int:
    from pathlib import Path

    from config import settings
    from src import tracker
    from src.notify import sink

    mod = _parse_module(args.source)
    html = _recover_view_source(Path(args.file).read_text(encoding="utf-8", errors="replace"))
    kind = "search" if args.search else "listing"
    if args.search:
        raws = mod.parse_search(html)
    else:
        one = mod.parse_listing(html, args.url)
        raws = [one] if one is not None else []
    if not raws:
        print("No listings parsed. The saved page may be a block/captcha page, the "
              "wrong --source, or a 'view-source' of a search — re-save as 'HTML Only'.")
        return 1

    conn = _open_db()
    try:
        src_row = _ensure_tracked_source(conn, args.source, args.url, kind, args.note)
        events = tracker.ingest_raws(conn, src_row, raws)
        sink.notify_events(conn, events, settings)
    finally:
        conn.close()

    print(f"ingested {len(raws)} listing(s) from {args.file}; {len(events)} event(s):")
    _print_events(events)
    return 0


# --------------------------------------------------------------------------- #
# grab: read the CIAN/Avito tabs already open in YOUR browser and track them.
# You browse the pages yourself (as a human Avito trusts); this attaches to the
# running Chrome over its debug port and reads content that is ALREADY loaded —
# no navigation, no request to the site, nothing to detect (§7-clean). Launch
# Chrome first with:  chrome --remote-debugging-port=9222 --user-data-dir=<dir>
# --------------------------------------------------------------------------- #
def cmd_grab(args: argparse.Namespace) -> int:
    from config import settings
    from playwright.sync_api import sync_playwright

    from src import tracker
    from src.notify import sink
    from src.web.service import detect_source

    conn = _open_db()
    all_events = []
    grabbed = 0
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(args.cdp_url)
            except Exception as exc:  # noqa: BLE001
                print(f"Could not connect to Chrome at {args.cdp_url} "
                      f"({type(exc).__name__}). Launch Chrome first with "
                      f"--remote-debugging-port=9222 and browse your tabs.")
                return 1
            pages = [pg for ctx in browser.contexts for pg in ctx.pages]
            print(f"connected; scanning {len(pages)} open tab(s)…")
            for page in pages:
                try:
                    url = page.url
                    source = detect_source(url)
                    if source not in ("cian", "avito"):
                        continue
                    mod = _parse_module(source)
                    kind = mod.classify_url(url)
                    html = page.content()  # already-loaded DOM; no site request
                    raws = (mod.parse_search(html) if kind == "search"
                            else ([r for r in [mod.parse_listing(html, url)] if r]))
                    if not raws:
                        print(f"  · no data parsed: {url[:72]}")
                        continue
                    src_row = _ensure_tracked_source(conn, source, url, kind, args.note)
                    all_events += tracker.ingest_raws(conn, src_row, raws)
                    grabbed += len(raws)
                    print(f"  · {len(raws):>2} from {source} {kind}: {url[:64]}")
                except Exception as exc:  # noqa: BLE001 - one bad tab must not abort
                    print(f"  · skipped a tab ({type(exc).__name__})")
            browser.close()  # detaches; does NOT close your Chrome
        sink.notify_events(conn, all_events, settings)
    finally:
        conn.close()

    print(f"\ngrabbed {grabbed} listing(s) from open tabs; {len(all_events)} event(s):")
    _print_events(all_events)
    return 0


# --------------------------------------------------------------------------- #
# urls: print tracked URLs, one per line (for scripts that open browser tabs)
# --------------------------------------------------------------------------- #
def cmd_urls(args: argparse.Namespace) -> int:
    from src.storage import repository as repo

    conn = _open_db()
    try:
        for row in repo.get_tracked(conn, active_only=not args.all):
            if args.source and row["source"] != args.source:
                continue
            print(row["url"])
    finally:
        conn.close()
    return 0


def _parse_module(source: str):
    if source == "cian":
        from src.adapters import cian

        return cian
    if source == "avito":
        from src.adapters import avito

        return avito
    raise SystemExit(f"unknown source: {source!r}")


# --------------------------------------------------------------------------- #
# notify-test (dev/debug): send one sample of each event type to your chat
# --------------------------------------------------------------------------- #
def cmd_notify_test(args: argparse.Namespace) -> int:
    from config import settings
    from src.notify.telegram import ListingMeta, TelegramNotifier
    from src.tracker import Event, EventType

    notifier = TelegramNotifier.from_settings(settings)
    if not notifier.enabled:
        print("Telegram is DISABLED — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
              "in your .env, then re-run. Nothing sent.")
        return 1

    url = "https://spb.cian.ru/sale/flat/328700780/"
    samples = [
        Event(type=EventType.NOW_TRACKING, source="cian", listing_id=1,
              external_id="328700780", url=url, note="sample flat",
              price=14_800_000),
        Event(type=EventType.PRICE_CHANGED, source="cian", listing_id=1,
              external_id="328700780", url=url, note="sample flat",
              old_price=14_800_000, new_price=14_000_000, delta=-800_000,
              percent=-5.405),
        Event(type=EventType.DELISTED, source="cian", listing_id=1,
              external_id="328700780", url=url, note="sample flat"),
    ]
    meta = ListingMeta(rooms=2, area_total=56.6, title="2-room flat", address=None)
    notifier.notify(samples, lambda _lid: meta)
    print(f"sent {len(samples)} sample message(s) to your configured chat "
          "(check Telegram; failures are logged above).")
    return 0


# --------------------------------------------------------------------------- #
# chat-ids (setup helper): list chats that recently messaged the bot, so you can
# copy an id into TELEGRAM_CHAT_ID. A bot can only message chats it knows about,
# so each recipient must send it a message (e.g. /start) at least once first.
# --------------------------------------------------------------------------- #
def cmd_chat_ids(args: argparse.Namespace) -> int:
    import json
    import urllib.request

    from config import settings

    if not settings.telegram_bot_token:
        print("Set TELEGRAM_BOT_TOKEN in your .env first.")
        return 1

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
            data = json.load(resp)
    except Exception as exc:  # noqa: BLE001 - report and stop; never crash
        print(f"getUpdates failed ({type(exc).__name__}). Check the token and that "
              "your VPN/connection can reach Telegram, then retry.")
        return 1

    if not data.get("ok"):
        print(f"Telegram returned an error: {data.get('description', data)}")
        return 1

    seen: dict[object, str] = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        label = (chat.get("username")
                 or " ".join(filter(None, (chat.get("first_name"),
                                           chat.get("last_name"))))
                 or chat.get("title") or chat.get("type") or "?")
        seen[cid] = label

    if not seen:
        print("No recent chats. Ask each person to open the bot and send /start "
              "(or any message), then re-run this within ~24h.")
        return 0

    print("Chats that recently messaged the bot:")
    for cid, label in seen.items():
        configured = " (configured)" if str(cid) in settings.telegram_chat_ids else ""
        print(f"  {cid}\t{label}{configured}")
    print("\nPut the ids you want to notify into TELEGRAM_CHAT_ID in .env, "
          "comma-separated, e.g.  TELEGRAM_CHAT_ID=111111111,222222222")
    return 0


# --------------------------------------------------------------------------- #
# serve: start the local web dashboard (loopback only, §9a/§10)
# --------------------------------------------------------------------------- #
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from config import settings
    from src.web.app import build_production_app

    app = build_production_app()
    print(f"dashboard on http://{settings.web_host}:{settings.web_port}  (Ctrl+C to stop)")
    uvicorn.run(app, host=settings.web_host, port=settings.web_port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="src.main", description="Realty Tracker CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="[dev] fetch once and print the RawListing(s)")
    f.add_argument("--source", required=True, choices=sorted(_ADAPTERS))
    f.add_argument("--url", required=True)
    f.add_argument("--search", action="store_true", help="treat URL as a search page")
    f.set_defaults(func=cmd_fetch)

    c = sub.add_parser("capture", help="[dev] save the raw page payload (PII redacted)")
    c.add_argument("--source", required=True, choices=sorted(_ADAPTERS))
    c.add_argument("--url", required=True)
    c.add_argument("--out", required=True)
    c.set_defaults(func=cmd_capture)

    i = sub.add_parser("init-db", help="create the database from the schema")
    i.set_defaults(func=cmd_init_db)

    a = sub.add_parser("add", help="register a listing/search URL to track")
    a.add_argument("--source", required=True, choices=sorted(_ADAPTERS))
    a.add_argument("--url", required=True)
    a.add_argument("--kind", default="listing", choices=("listing", "search"))
    a.add_argument("--note", default=None)
    a.set_defaults(func=cmd_add)

    ls = sub.add_parser("list", help="list tracked sources (the watchlist)")
    ls.set_defaults(func=cmd_list)

    rm = sub.add_parser("remove", help="deactivate (or --purge delete) a tracked source")
    rm.add_argument("--id", type=int, required=True)
    rm.add_argument("--purge", action="store_true",
                    help="hard-delete the watchlist row (price history is kept)")
    rm.set_defaults(func=cmd_remove)

    r = sub.add_parser("run-once", help="run one tracking pass and print events")
    r.set_defaults(func=cmd_run_once)

    n = sub.add_parser("notify-test", help="[dev] send one sample Telegram message per event type")
    n.set_defaults(func=cmd_notify_test)

    ci = sub.add_parser("chat-ids", help="list chats that messaged the bot (to fill TELEGRAM_CHAT_ID)")
    ci.set_defaults(func=cmd_chat_ids)

    sv = sub.add_parser("serve", help="start the local web dashboard")
    sv.set_defaults(func=cmd_serve)

    wu = sub.add_parser("warmup", help="open a headful browser to solve a challenge / seed cookies")
    wu.add_argument("--url", default="https://www.avito.ru/",
                    help="page to open (solve the captcha here)")
    wu.set_defaults(func=cmd_warmup)

    ing = sub.add_parser("ingest", help="track a hand-saved HTML page (no fetch; for blocked sites)")
    ing.add_argument("--source", required=True, choices=sorted(_ADAPTERS))
    ing.add_argument("--url", required=True, help="the original listing/search URL")
    ing.add_argument("--file", required=True, help="path to the saved .html page")
    ing.add_argument("--search", action="store_true", help="the file is a search results page")
    ing.add_argument("--note", default=None)
    ing.set_defaults(func=cmd_ingest)

    gr = sub.add_parser("grab", help="track CIAN/Avito tabs already open in your browser (no fetch)")
    gr.add_argument("--cdp-url", dest="cdp_url", default="http://localhost:9222",
                    help="Chrome remote-debugging endpoint (default http://localhost:9222)")
    gr.add_argument("--note", default=None)
    gr.set_defaults(func=cmd_grab)

    ur = sub.add_parser("urls", help="print tracked URLs, one per line (for scripting)")
    ur.add_argument("--source", choices=sorted(_ADAPTERS), default=None)
    ur.add_argument("--all", action="store_true", help="include inactive sources")
    ur.set_defaults(func=cmd_urls)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
