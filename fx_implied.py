#!/usr/bin/env python3
"""Print FX-swap implied yields using live Bloomberg quotes + SETTLE_DT.

    python fx_implied.py --pair USDCNH --rate 1.50 --tenors 1M,3M,6M,1Y --live
    python fx_implied.py --pair USDHKD --rate 3.80 --live      # ACT/365 leg

--rate is the KNOWN quote-currency rate in percent (the curve you would select
on FXFA). act comes from Bloomberg's SETTLE_DT (spot -> forward), never a
nominal 30/90/180.
"""
from __future__ import annotations

import argparse

from quote_ocr.bloomberg import (
    BloombergClient,
    act_days,
    build_fx_market,
    demo_mock_fx,
    settle_dates,
)
from quote_ocr.implied import basis_of, implied_base_yield


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pair", default="USDCNH", help="FX pair, e.g. USDCNH.")
    p.add_argument("--rate", type=float, default=None,
                   help="Known QUOTE-ccy rate in percent (manual override).")
    p.add_argument("--source", default=None, choices=["channel", "benchmark", "market"],
                   help="Where the known-leg yield comes from. This changes what "
                        "the implied yield MEANS -- see quote_ocr/yields.py.")
    p.add_argument("--db", default=None, help="quotes.db (for channel/benchmark).")
    p.add_argument("--date", default=None, help="Quote date for the store.")
    p.add_argument("--tenors", default="1M,3M,6M,1Y", help="Tenors.")
    p.add_argument("--act", type=int, default=None,
                   help="Override actual days (skip the SETTLE_DT lookup).")
    p.add_argument("--live", action="store_true", help="Use the Bloomberg terminal.")
    args = p.parse_args(argv)

    pair = args.pair.upper()
    tenors = [t.strip() for t in args.tenors.split(",") if t.strip()]
    base, quote = pair[:3], pair[3:]

    if args.rate is None and not args.source:
        p.error("give --rate, or --source channel|benchmark|market")

    client = BloombergClient() if args.live else demo_mock_fx()
    rates: dict = {}
    with client as c:
        # SETTLE_DT comes back on each FxPoint, so `act` is the real
        # settle-to-settle day count for today's trade date.
        fx = build_fx_market(c, pair, tenors)
        if args.source == "market":
            from quote_ocr import yields
            rates = yields.from_market(c, quote, tenors, yields.load_overrides())

    if args.source in ("channel", "benchmark"):
        from quote_ocr import sources, yields
        from quote_ocr.store import QuoteStore
        if not (args.db and args.date):
            p.error(f"--source {args.source} needs --db and --date")
        with QuoteStore(args.db) as store:
            if args.source == "benchmark":
                rates = yields.from_benchmark(store, args.date, quote)
            else:
                surf = sources.surface_from_store(store, args.date, [quote])
                rates = yields.from_channel(surf, quote)

    if args.source:
        from quote_ocr import yields
        print(f"known-leg source: {args.source} -- {yields.describe(args.source)}")

    any_fp = next(iter(fx.values()), None)
    spot_settle = any_fp.spot_settle if any_fp else None
    print(f"{pair}: implying {base} (ACT/{basis_of(base)}) from "
          f"{quote} (ACT/{basis_of(quote)})")
    print(f"  spot settle: {spot_settle or 'n/a'}\n")
    print(f"  {'tenor':6} {'act':>4} {'fwd settle':>12} {quote+' rate':>10} "
          f"{'implied_bid':>12} {'implied_ask':>12}")
    for t in tenors:
        fp = fx.get(t)
        act = args.act or (fp.act if fp else None)
        r_quote = (args.rate / 100.0) if args.rate is not None else rates.get(t)
        if fp is None or act is None or r_quote is None or None in (
                fp.spot_bid, fp.spot_ask, fp.fwd_bid, fp.fwd_ask):
            why = "no rate" if r_quote is None else "missing FX"
            print(f"  {t:6} {'-':>4} {'-':>12} {'-':>10}  ({why})")
            continue
        r = implied_base_yield(pair, t, act, fp.spot_bid, fp.spot_ask,
                               fp.fwd_bid, fp.fwd_ask, r_quote)
        print(f"  {t:6} {act:>4} {str(fp.fwd_settle or '-'):>12} "
              f"{r_quote*100:>9.4f}% {r.bid*100:>11.6f}% {r.ask*100:>11.6f}%")


if __name__ == "__main__":
    raise SystemExit(main())
