#!/usr/bin/env python3
"""Print the Bloomberg bid/ask captured for FX spot + forward outrights.

Offline (mock data), for development:
    python fx_quotes.py --pairs USDCNH,EURUSD --tenors 1M,3M,6M,1Y

Live (pulls from the Bloomberg terminal on this machine):
    python fx_quotes.py --pairs USDCNH --tenors 1M,3M,6M,1Y --live

Use this at the terminal to confirm the tickers/fields in FX_TICKERS return the
values you expect (spot bid/ask, forward outright bid/ask, derived points).
"""
from __future__ import annotations

import argparse

from quote_ocr.bloomberg import (
    FX_TICKERS,
    BloombergClient,
    TENOR_CODE,
    _fwd_key,
    build_fx_market,
    demo_mock_fx,
)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pairs", default="USDCNH,EURUSD", help="Comma-separated FX pairs.")
    p.add_argument("--tenors", default="1M,3M,6M,1Y", help="Comma-separated tenors.")
    p.add_argument("--live", action="store_true", help="Use the Bloomberg terminal.")
    args = p.parse_args(argv)

    pairs = [x.strip() for x in args.pairs.split(",")]
    tenors = [x.strip() for x in args.tenors.split(",")]

    client = BloombergClient() if args.live else demo_mock_fx()
    print(f"source: {'LIVE Bloomberg terminal' if args.live else 'offline mock'}\n")

    with client as c:
        for pair in pairs:
            fx = build_fx_market(c, pair, tenors)
            fwd_leg = _fwd_key(pair)
            spot_sec = FX_TICKERS["spot"].format(pair=pair)
            any_fp = next(iter(fx.values()), None)
            spot_bid = any_fp.spot_bid if any_fp else None
            spot_ask = any_fp.spot_ask if any_fp else None

            print(f"=== {pair} ===")
            print(f"  spot   [{spot_sec:>16}]  bid={_f(spot_bid)}  ask={_f(spot_ask)}  "
                  f"mid={_f(any_fp.spot) if any_fp else '-'}")
            print(f"  {'tenor':5}  {'ticker':>16}  {'bid':>10}  {'ask':>10}  "
                  f"{'points(mid)':>12}")
            for t in tenors:
                fp = fx.get(t)
                sec = FX_TICKERS["outright"].format(fwd=fwd_leg, tenor=TENOR_CODE.get(t, t))
                if fp is None:
                    print(f"  {t:5}  {sec:>16}  (no data)")
                    continue
                print(f"  {t:5}  {sec:>16}  {_f(fp.fwd_bid):>10}  {_f(fp.fwd_ask):>10}  "
                      f"{_f(fp.points):>12}")
            print()


def _f(v):
    if v is None:
        return "-"
    return f"{v:.5f}" if abs(v) < 100 else f"{v:.1f}"


if __name__ == "__main__":
    raise SystemExit(main())
