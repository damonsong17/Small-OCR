#!/usr/bin/env python3
"""Discover which Bloomberg tickers actually work for cross-currency pairs.

Bloomberg's cross forward naming is inconsistent and undocumented, so instead of
guessing we probe candidate formats and keep whatever returns a price. Results
are saved to fx_tickers.json and used automatically by run_desk.py.

    python fx_resolve.py --ccy USD,CNH,CHF,EUR,HKD --tenors 1M,3M,6M,1Y --live

Pin a ticker you verified by hand (e.g. the CHFHKD 1Y exception):

    python fx_resolve.py --pin "CHFHKD|1Y=HDSF1Y Curncy"

Anything unresolved is fine -- run_desk.py triangulates it from the USD legs.
"""
from __future__ import annotations

import argparse

from quote_ocr import tickers
from quote_ocr.bloomberg import BloombergClient, all_pairs, demo_mock_fx, is_usd_pair


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ccy", default="USD,CNH,CHF,EUR,HKD", help="Currencies.")
    p.add_argument("--tenors", default="1M,3M,6M,1Y", help="Tenors.")
    p.add_argument("--file", default=tickers.OVERRIDES_FILE, help="Overrides JSON.")
    p.add_argument("--pin", action="append", default=[],
                   help="Pin a mapping, e.g. \"CHFHKD|1Y=HDSF1Y Curncy\"; repeatable.")
    p.add_argument("--live", action="store_true", help="Use the Bloomberg terminal.")
    args = p.parse_args(argv)

    overrides = tickers.load_overrides(args.file)

    for pin in args.pin:
        if "=" in pin:
            k, v = pin.split("=", 1)
            overrides[k.strip()] = v.strip()
            print(f"pinned {k.strip()} -> {v.strip()}")
    if args.pin and not args.live:
        tickers.save_overrides(overrides, args.file)
        print(f"saved {args.file}")
        return 0

    ccys = [c.strip().upper() for c in args.ccy.split(",") if c.strip()]
    tenors = [t.strip() for t in args.tenors.split(",") if t.strip()]
    crosses = [p for p in all_pairs(ccys) if not is_usd_pair(p)]
    print(f"probing {len(crosses)} cross pair(s): {', '.join(crosses)}\n")

    client = BloombergClient() if args.live else demo_mock_fx()
    with client as c:
        found = tickers.resolve(c, crosses, tenors, overrides)

    tickers.save_overrides(found, args.file)
    print(f"\nsaved {len(found)} mapping(s) to {args.file}")
    print("Unresolved pairs are triangulated from their USD legs at run time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
