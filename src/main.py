"""Realty Tracker CLI entrypoint.

Phase 3 provides two DEV/DEBUG commands so the CIAN adapter can be smoke-tested
without the tracker (which is a later phase):

    python -m src.main fetch   --source cian --url "<listing-or-search-url>"
    python -m src.main capture --source cian --url "<url>" --out <path>

Other §11 commands (init-db, add, run-once, run, serve) belong to their own
build-order phases and are intentionally not implemented yet.
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

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
