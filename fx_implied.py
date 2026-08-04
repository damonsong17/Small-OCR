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
    p.add_argument("--rate", type=float, required=True,
                   help="Known QUOTE-ccy rate in percent, e.g. 1.50 for CNH.")
    p.add_argument("--tenors", default="1M,3M,6M,1Y", help="Tenors.")
    p.add_argument("--act", type=int, default=None,
                   help="Override actual days (skip the SETTLE_DT lookup).")
    p.add_argument("--live", action="store_true", help="Use the Bloomberg terminal.")
    args = p.parse_args(argv)

    pair = args.pair.upper()
    tenors = [t.strip() for t in args.tenors.split(",") if t.strip()]
    base, quote = pair[:3], pair[3:]
    r_quote = args.rate / 100.0

    client = BloombergClient() if args.live else demo_mock_fx()
    with client as c:
        # SETTLE_DT comes back on each FxPoint, so `act` is the real
        # settle-to-settle day count for today's trade date.
        fx = build_fx_market(c, pair, tenors)

    any_fp = next(iter(fx.values()), None)
    spot_settle = any_fp.spot_settle if any_fp else None
    print(f"{pair}: implying {base} (ACT/{basis_of(base)}) from "
          f"{quote} {args.rate:.4f}% (ACT/{basis_of(quote)})")
    print(f"  spot settle: {spot_settle or 'n/a'}\n")
    print(f"  {'tenor':6} {'act':>4} {'fwd settle':>12} {'implied_bid':>12} {'implied_ask':>12}")
    for t in tenors:
        fp = fx.get(t)
        act = args.act or (fp.act if fp else None)
        if fp is None or act is None or None in (fp.spot_bid, fp.spot_ask,
                                                 fp.fwd_bid, fp.fwd_ask):
            got = fp.fwd_settle if fp else None
            print(f"  {t:6} {'-':>4} {str(got or '-'):>12}  (missing data)")
            continue
        r = implied_base_yield(pair, t, act, fp.spot_bid, fp.spot_ask,
                               fp.fwd_bid, fp.fwd_ask, r_quote)
        print(f"  {t:6} {act:>4} {str(fp.fwd_settle or '-'):>12} "
              f"{r.bid*100:>11.6f}% {r.ask*100:>11.6f}%")


if __name__ == "__main__":
    raise SystemExit(main())
