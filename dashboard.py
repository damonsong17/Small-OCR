"""Treasury dashboard — load a folder of spreadsheets and serve the board.

    python dashboard.py --data data\\treasury --open
    python dashboard.py --data data\\treasury --check         # load report, no server
    python dashboard.py --data data\\treasury --snapshot      # store today's headlines
    python dashboard.py --data data\\treasury --export out.json

Runs entirely offline: standard library plus openpyxl, no network calls.
"""
from __future__ import annotations

import argparse
import json
import sys

from treasury import load_sources
from treasury.config import load_settings
from treasury.metrics import REGISTRY, catalogue
from treasury.server import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default="", help="folder of .xlsx/.csv source files")
    parser.add_argument("--host", default="", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="port (default 8787)")
    parser.add_argument("--base-currency", default="", help="reporting currency")
    parser.add_argument("--as-of", default="", help="reporting date (YYYY-MM-DD)")
    parser.add_argument("--quotes-db", default="",
                        help="the OCR pipeline's quotes.db, for live bid/offer")
    parser.add_argument("--open", action="store_true", help="open a browser")
    parser.add_argument("--check", action="store_true",
                        help="load the data, print what was understood, and exit")
    parser.add_argument("--snapshot", action="store_true",
                        help="store today's headline numbers for trend history and exit")
    parser.add_argument("--export", default="",
                        help="write every panel to a JSON file and exit")
    return parser


def _settings_from(args: argparse.Namespace):
    overrides = {}
    if args.base_currency:
        overrides["base_currency"] = args.base_currency.upper()
    if args.as_of:
        overrides["as_of"] = args.as_of
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    if args.quotes_db:
        overrides["quotes_db"] = args.quotes_db
    settings = load_settings(args.data or "", **overrides)
    if args.data:
        settings.data_dir = args.data
    return settings


def check(book) -> int:
    print(f"as of {book.as_of}   base {book.settings.base_currency}")
    print(f"settings: {book.settings.loaded_from or 'defaults'}")
    print()
    print(f"{'file':<34}{'sheet':<20}{'read as':<24}{'rows':>6}")
    print("-" * 84)
    for entry in book.log:
        detail = entry.as_dict()
        print(f"{detail['source'][:33]:<34}{detail['sheet'][:19]:<20}"
              f"{(detail['dataset'] or '(skipped: ' + detail['problem'] + ')')[:23]:<24}"
              f"{detail['rows']:>6}")
    print()
    for item in catalogue(book):
        mark = "ok " if item["available"] else "-- "
        missing = "" if item["available"] else f"  needs {', '.join(item['missing'])}"
        print(f"  {mark}{item['title']}{missing}")
    if book.warnings:
        print()
        for warning in book.warnings:
            print(f"  warning: {warning}")
    return 0 if any(e.dataset for e in book.log) else 1


def export(book, path: str) -> int:
    payload = {
        "as_of": book.as_of, "base_currency": book.settings.base_currency,
        "counts": book.counts(), "warnings": book.warnings,
        "panels": {spec.id: book.metric(spec.id) for spec in REGISTRY.values()},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"wrote {path}")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings_from(args)

    if args.check or args.snapshot or args.export:
        book = load_sources(settings.data_dir, args.quotes_db or settings.quotes_db,
                            **{k: v for k, v in
                               (("base_currency", settings.base_currency),
                                ("as_of", settings.as_of)) if v})
        if args.check:
            return check(book)
        if args.export:
            return export(book, args.export)
        from treasury.store import open_store
        store = open_store(book.settings)
        if store is None:
            print("could not open the snapshot store", file=sys.stderr)
            return 1
        try:
            panels = {spec.id: book.metric(spec.id) for spec in REGISTRY.values()
                      if not spec.missing(book) and spec.group != "Data"}
            written = store.snapshot(book, panels)
        finally:
            store.close()
        print(f"snapshot {book.as_of}: "
              + ", ".join(f"{k}={v}" for k, v in sorted(written.items()) if v))
        return 0

    serve(settings, args.quotes_db, open_browser=args.open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
