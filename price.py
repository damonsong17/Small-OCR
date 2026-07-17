#!/usr/bin/env python3
"""Demo: price OCR funding quotes against Bloomberg FX market data (USDCNH).

Runs OFFLINE by default using a Bloomberg mock, so the framework is testable
with no terminal:

    python price.py --db data/output/quotes.db --date 2026-07-16

Use the live Bloomberg terminal (Desktop API on this machine) with --live:

    python price.py --db data/output/quotes.db --date 2026-07-16 --live
"""
from __future__ import annotations

import argparse

from quote_ocr import pricing
from quote_ocr.bloomberg import BloombergClient, build_fx_market, demo_mock_usdcnh


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=None, help="quotes.db to read funding rates from.")
    p.add_argument("--date", default=None, help="Quote date, e.g. 2026-07-16.")
    p.add_argument("--pair", default="USDCNH", help="FX pair (default USDCNH).")
    p.add_argument("--base", default="USD", help="Base ccy funding (default USD).")
    p.add_argument("--quote", default="CNH", help="Quote ccy funding (default CNH).")
    p.add_argument("--tenors", default="1M,3M,6M,1Y", help="Comma-separated tenors.")
    p.add_argument("--live", action="store_true", help="Use the real Bloomberg terminal.")
    args = p.parse_args(argv)
    tenors = [t.strip() for t in args.tenors.split(",")]

    # 1) funding rates from the OCR store (or demo rates if no db)
    if args.db and args.date:
        from quote_ocr.store import QuoteStore
        with QuoteStore(args.db) as store:
            base_rates = pricing.rates_from_store(store, args.date, args.base)
            quote_rates = pricing.rates_from_store(store, args.date, args.quote)
    else:
        print("(no --db/--date given; using demo funding rates)")
        base_rates = {"1M": 0.0382, "3M": 0.0402, "6M": 0.0413, "1Y": 0.0424}
        quote_rates = {"1M": 0.0130, "3M": 0.0135, "6M": 0.0140, "1Y": 0.0130}

    # 2) FX market data from Bloomberg (mock offline, or --live terminal)
    client = BloombergClient() if args.live else demo_mock_usdcnh()
    with client as c:
        fx = build_fx_market(c, args.pair, tenors)

    # 3) compare
    rows = pricing.compare(fx, base_rates, quote_rates)
    _print(args.pair, rows)


def _print(pair, rows):
    cols = ["tenor", "spot", "r_base", "r_quote", "market_points", "fair_points",
            "pickup_points", "note"]
    print(f"\n{pair} CIP comparison")
    print("  ".join(c.ljust(13) for c in cols))
    print("-" * 96)
    for r in rows:
        d = r.as_row()
        print("  ".join(_fmt(d[c]).ljust(13) for c in cols))


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}" if abs(v) < 10 else f"{v:.1f}"
    return str(v)


if __name__ == "__main__":
    raise SystemExit(main())
