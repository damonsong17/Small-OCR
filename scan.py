#!/usr/bin/env python3
"""Run before a trade: scan channel quotes vs live Bloomberg FX for arbitrage.

Offline (mock market data), for development:
    python scan.py --db data/output/quotes.db --date 2026-07-16

Live (pulls current FX from the Bloomberg terminal on this machine):
    python scan.py --db data/output/quotes.db --date 2026-07-16 --live

Prints every opportunity with its P&L in bps and risk type. Running it right
before a trade guarantees the market side uses the latest FXFA/market data.
"""
from __future__ import annotations

import argparse

from quote_ocr import arb
from quote_ocr.bloomberg import BloombergClient, build_fx_all, demo_mock_fx


DEMO_SURFACE = {  # illustrative funding rates (decimal) when no --db given
    "USD": {"bid": {"1M": 0.0380, "3M": 0.0400, "6M": 0.0411, "1Y": 0.0422},
            "offer": {"1M": 0.0385, "3M": 0.0405, "6M": 0.0418, "1Y": 0.0445}},
    "EUR": {"bid": {"1M": 0.0230, "3M": 0.0240, "6M": 0.0250, "1Y": 0.0270},
            "offer": {"1M": 0.0250, "3M": 0.0260, "6M": 0.0275, "1Y": 0.0285}},
    "CNH": {"bid": {"1M": 0.0125, "3M": 0.0135, "6M": 0.0140, "1Y": 0.0130},
            "offer": {"1M": 0.0155, "3M": 0.0155, "6M": 0.0160, "1Y": 0.0170}},
}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=None, help="quotes.db with channel funding rates.")
    p.add_argument("--date", default=None, help="Quote date, e.g. 2026-07-16.")
    p.add_argument("--channel", default="AFS", help="Channel/source name (pilot: AFS).")
    p.add_argument("--pairs", default="", help="FX pairs to check (explicit).")
    p.add_argument("--ccy", default="USD,CNH,CHF,EUR,HKD",
                   help="Currencies; all pair combinations are checked "
                        "(ignored if --pairs is given).")
    p.add_argument("--tenors", default="1M,3M,6M,1Y", help="Tenors to check.")
    p.add_argument("--threshold", type=float, default=0.5, help="Min bps to flag.")
    p.add_argument("--live", action="store_true", help="Use the Bloomberg terminal.")
    args = p.parse_args(argv)

    tenors = [x.strip() for x in args.tenors.split(",")]
    if args.pairs.strip():
        pairs = [x.strip() for x in args.pairs.split(",") if x.strip()]
    else:
        from quote_ocr.bloomberg import all_pairs
        pairs = all_pairs([c.strip().upper() for c in args.ccy.split(",") if c.strip()])
    currencies = sorted({c for pr in pairs for c in (pr[:3], pr[3:])})
    print(f"pairs: {', '.join(pairs)}")

    # 1) channel funding surface (from OCR store, or demo)
    if args.db and args.date:
        from quote_ocr.store import QuoteStore
        with QuoteStore(args.db) as store:
            surface = arb.surface_from_store(store, args.date, currencies)
    else:
        print("(no --db/--date; using demo funding surface)")
        surface = DEMO_SURFACE

    # 2) FX market data (live terminal or mock)
    client = BloombergClient() if args.live else demo_mock_fx()
    with client as c:
        fx = build_fx_all(c, pairs, tenors)

    # 2b) show the raw Bloomberg bid/ask actually captured (ticker verification)
    for pr, m in fx.items():
        for t, fp in m.items():
            print(f"[FX] {pr} {t}: spot {fp.spot_bid}/{fp.spot_ask}  "
                  f"fwd {fp.fwd_bid}/{fp.fwd_ask}  pts {fp.points}")

    # 3) scan the channel surface for cross-currency arbitrage
    opps = arb.scan_surface_noarb(surface, fx, pairs, tenors,
                                  channel=args.channel, threshold_bps=args.threshold)
    _report(args.channel, opps)


def _report(channel, opps):
    print(f"\n{channel}: {len(opps)} arbitrage opportunity(ies) "
          f"(most profitable first)\n")
    if not opps:
        print("  none above threshold.")
        return
    cols = [("pair", 8), ("tenor", 6), ("pnl_bps", 9), ("risk_type", 34), ("detail", 60)]
    print("  ".join(h.ljust(w) for h, w in cols))
    print("-" * 120)
    for o in sorted(opps, key=lambda x: -x.pnl_bps):
        d = o.as_row()
        print("  ".join(str(d[h]).ljust(w) for h, w in cols))


if __name__ == "__main__":
    raise SystemExit(main())
