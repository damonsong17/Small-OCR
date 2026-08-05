#!/usr/bin/env python3
"""Smallest possible component test -- try any tickers and see what comes back.

Designed for the offline machine: no new dependencies, nothing to rebuild.
Run it, read the output, and if a ticker is wrong just pin the right one into
fx_tickers.json (plain JSON) -- no code change needed.

    # test whatever you like, directly
    python try_ticker.py --live "CGEU12M Curncy" "EURCNH12M Curncy" "EURCNH Curncy"

    # show every candidate the resolver would try for a pair/tenor
    python try_ticker.py --candidates EURCNH 1Y

    # probe all candidates for one pair/tenor and report which works
    python try_ticker.py --live --probe EURCNH 1Y

    # pin a ticker you confirmed (writes fx_tickers.json, no rebuild needed)
    python try_ticker.py --pin "EURCNH|1Y=CGEU12M Curncy"
"""
from __future__ import annotations

import argparse
import sys

from quote_ocr import tickers as tk

FIELDS = ["PX_BID", "PX_ASK", "PX_LAST", "SETTLE_DT"]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("securities", nargs="*", help="Tickers to test verbatim.")
    p.add_argument("--candidates", nargs="+", metavar=("PAIR", "TENOR"),
                   help="Print candidates for PAIR [TENOR] and exit.")
    p.add_argument("--probe", nargs="+", metavar=("PAIR", "TENOR"),
                   help="Probe all candidates for PAIR [TENOR].")
    p.add_argument("--pin", action="append", default=[],
                   help="\"PAIR|TENOR=TICKER Curncy\"; repeatable.")
    p.add_argument("--file", default=tk.OVERRIDES_FILE, help="Overrides JSON.")
    p.add_argument("--live", action="store_true", help="Use the Bloomberg terminal.")
    args = p.parse_args(argv)

    if args.pin:
        ov = tk.load_overrides(args.file)
        for pin in args.pin:
            if "=" not in pin:
                print(f"  ! ignoring '{pin}' (expected PAIR|TENOR=TICKER)")
                continue
            k, v = pin.split("=", 1)
            ov[k.strip()] = v.strip()
            print(f"pinned {k.strip()} -> {v.strip()}")
        tk.save_overrides(ov, args.file)
        print(f"saved {args.file}")
        if not (args.securities or args.probe):
            return 0

    if args.candidates:
        pair = args.candidates[0].upper()
        tenor = args.candidates[1] if len(args.candidates) > 1 else None
        print(f"candidates for {pair} {tenor or 'SPOT'}:")
        for c in tk.candidates(pair, tenor):
            print("   ", c)
        return 0

    secs = list(args.securities)
    if args.probe:
        pair = args.probe[0].upper()
        tenor = args.probe[1] if len(args.probe) > 1 else None
        secs += tk.candidates(pair, tenor)
    if not secs:
        print("nothing to test; pass tickers, --probe or --candidates")
        return 1

    if args.live:
        from quote_ocr.bloomberg import BloombergClient
        client = BloombergClient()
    else:
        from quote_ocr.bloomberg import demo_mock_fx
        client = demo_mock_fx()
        print("(offline mock -- pass --live on the terminal)\n")

    try:
        with client as c:
            data = c.reference(secs, FIELDS)
    except Exception as e:
        print(f"request failed: {e}")
        return 1

    ok = []
    for s in secs:
        rec = data.get(s) or {}
        if rec.get("__error__"):
            print(f"  {s:28} ERROR  {str(rec['__error__'])[:70]}")
            continue
        bid, ask, last = rec.get("PX_BID"), rec.get("PX_ASK"), rec.get("PX_LAST")
        if bid is None and ask is None and last is None:
            fe = rec.get("__fieldErrors__")
            print(f"  {s:28} no data{'  ' + str(fe)[:50] if fe else ''}")
            continue
        ok.append(s)
        print(f"  {s:28} bid={_f(bid)} ask={_f(ask)} last={_f(last)} "
              f"settle={rec.get('SETTLE_DT')}")

    print(f"\n{len(ok)}/{len(secs)} returned data")
    if ok and args.probe:
        pair = args.probe[0].upper()
        tenor = args.probe[1] if len(args.probe) > 1 else None
        print(f"\nTo pin the winner:\n  python try_ticker.py --pin "
              f"\"{tk.key(pair, tenor)}={ok[0]}\"")
    return 0


def _f(v):
    return "-" if v is None else f"{v:.6f}"


if __name__ == "__main__":
    sys.exit(main())
