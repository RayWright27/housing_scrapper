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

_ADAPTERS = {"cian"}


def _get_adapter(source: str):
    if source == "cian":
        from src.adapters.cian import CianAdapter

        return CianAdapter()
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
    from src.adapters.cian import redact as cian_redact

    redactors = {"cian": cian_redact}
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
# run-once: one full tracking pass. The ONLY place the live adapter is built.
# --------------------------------------------------------------------------- #
def cmd_run_once(args: argparse.Namespace) -> int:
    from config import settings
    from src.adapters.cian import CianAdapter
    from src.tracker import run_once

    conn = _open_db()
    with CianAdapter() as cian:  # live adapter constructed here only
        adapters = {"cian": cian}
        events = run_once(adapters, conn, settings)
    conn.close()

    if not events:
        print("no events (nothing new, no price changes, no delistings)")
        return 0
    print(f"{len(events)} event(s):\n")
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

    r = sub.add_parser("run-once", help="run one tracking pass and print events")
    r.set_defaults(func=cmd_run_once)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
