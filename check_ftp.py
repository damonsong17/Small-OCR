#!/usr/bin/env python3
"""Check a published FTP sheet for cross-currency arbitrage.

Answers one question before the sheet goes out: can anyone borrow one currency
from us, FX-swap it, and place another currency back with us at a profit?

    python check_ftp.py FTP.xlsx --live            # against live Bloomberg FX
    python check_ftp.py FTP.xlsx                   # mock FX, for a dry run
    python check_ftp.py FTP.xlsx --sheet Sheet1 --threshold 0.01

SAME TENOR ONLY. A maturity mismatch is a gap position -- it has to be rolled or
reinvested at a rate nobody knows yet -- so it is not something our published
quote can be held to. Every same-tenor round trip, by contrast, is closed on day
one and IS our liability. Pass --allow-mismatch to look at gap trades anyway;
they are reported as information, never as a reason to block publishing.

Exit code is 1 when a same-tenor arbitrage exists, so this can gate a release.
"""
from __future__ import annotations

import argparse

from quote_ocr import arb
from quote_ocr.bloomberg import BloombergClient, all_pairs, build_fx_all, demo_mock_fx
from quote_ocr.ftp_sheet import read_ftp_sheet


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("xlsx", help="The published FTP workbook.")
    p.add_argument("--sheet", default=None, help="Worksheet name (default: first).")
    p.add_argument("--ccy", default="", help="Currencies to check; default is "
                                             "every currency found in the sheet.")
    p.add_argument("--tenors", default="auto", help="Default: the sheet's own tenors.")
    p.add_argument("--threshold", type=float, default=0.01,
                   help="Min bps to flag (default 0.01 -- effectively any edge).")
    p.add_argument("--allow-mismatch", action="store_true",
                   help="Also report tenor-mismatched routes (gap positions, "
                        "reported for information only).")
    p.add_argument("--tickers", default="fx_tickers.json",
                   help="Cross-pair ticker overrides (see fx_resolve.py).")
    p.add_argument("--live", action="store_true", help="Use the Bloomberg terminal.")
    args = p.parse_args(argv)

    print(f"reading {args.xlsx}")
    surface, meta = read_ftp_sheet(args.xlsx, sheet=args.sheet)
    # A sheet with no rates in it is NOT an arbitrage-free sheet. Reporting
    # "none found" here would hand back a clean bill of health for a template
    # nobody has filled in yet, which is the worst possible failure of this tool.
    n_rates = sum(len(s["bid"]) + len(s["offer"]) for s in surface.values())
    if n_rates == 0:
        print("\n! NO RATES READ -- nothing was checked, and this is NOT a pass.")
        if meta["blank"]:
            print(f"  Every cell is still a placeholder ({len(meta['blank'])} of "
                  f"them), e.g. {meta['blank'][0]}.")
        print("  Fill the sheet in, or point --sheet at the right worksheet.")
        return 2

    ccys = ([c.strip().upper() for c in args.ccy.split(",") if c.strip()]
            or [c for c in meta["currencies"]])
    # a currency with no rate at all cannot take part in a round trip
    ccys = [c for c in ccys if surface.get(c, {}).get("bid") or surface.get(c, {}).get("offer")]
    pairs = all_pairs(ccys)
    tenors = (meta["tenors"] if args.tenors.strip().lower() == "auto"
              else [t.strip() for t in args.tenors.split(",") if t.strip()])
    print(f"currencies: {', '.join(ccys)}")
    print(f"tenors:     {', '.join(tenors)}")
    print(f"pairs ({len(pairs)}): {', '.join(pairs)}\n")

    from quote_ocr import tickers as tk
    cross = tk.load_overrides(args.tickers)
    client = BloombergClient() if args.live else demo_mock_fx()
    with client as c:
        fx = build_fx_all(c, pairs, tenors, cross_tickers=cross)
    _coverage(fx, pairs, tenors)

    opps = arb.scan_surface_noarb(
        surface, fx, pairs, tenors, channel="PUBLISHED_FTP", kind="ftp_self_arb",
        threshold_bps=args.threshold, allow_mismatch=False)

    # What was actually tested: a round trip needs an offer on one leg, a bid on
    # the other, AND an FX rate. Anything short of that was not checked, and the
    # verdict has to say so instead of implying full cover.
    checked, skipped = _tested(surface, fx, pairs, tenors)

    print(f"=== SAME-TENOR CHECK: {len(opps)} way(s) to arbitrage us ===")
    if not opps:
        print(f"  none found in the {checked} round trip(s) that could be tested.")
        if skipped:
            print(f"  ! {skipped} pair-tenor direction(s) were NOT tested (missing "
                  f"rate or missing FX) -- this is not a full pass.")
        else:
            print("  Every pair and tenor on the sheet was covered.")
        print()
    else:
        for o in sorted(opps, key=lambda x: -x.pnl_bps):
            print(f"  {o.pnl_bps:8.2f} bps | {o.legs()}")
            print(f"           {o.detail}")
        print("\n  Each line is a closed round trip a client can do against the "
              "published sheet.\n  Tighten the bid side of the currency being "
              "placed back with us, or widen the offer\n  side of the currency "
              "being borrowed, until these disappear.\n")

    if args.allow_mismatch:
        gaps = [o for o in arb.scan_surface_noarb(
            surface, fx, pairs, tenors, channel="PUBLISHED_FTP",
            kind="ftp_gap", threshold_bps=args.threshold, allow_mismatch=True)
            if o.mismatch]
        print(f"=== TENOR-MISMATCH (information only): {len(gaps)} route(s) ===")
        for o in sorted(gaps, key=lambda x: -x.pnl_bps)[:15]:
            print(f"  {o.pnl_bps:8.2f} bps | {o.legs()}")
        if gaps:
            print("  These are NOT closed arbitrage: the gap must be rolled at a "
                  "future rate.\n  They do not block publishing.\n")

    return 1 if opps else 0


def _tested(surface, fx, pairs, tenors):
    """(round trips actually tested, directions skipped for want of data)."""
    from quote_ocr.pricing import lookup_tenor

    def has(ccy, side, t):
        return lookup_tenor(surface.get(ccy, {}).get(side, {}), t) is not None

    checked = skipped = 0
    for pr in pairs:
        base, quote = pr[:3], pr[3:]
        for t in tenors:
            fp = fx.get(pr, {}).get(t)
            ok_fx = fp is not None and fp.spot is not None and fp.points is not None
            for borrow, place in ((base, quote), (quote, base)):
                if ok_fx and has(borrow, "offer", t) and has(place, "bid", t):
                    checked += 1
                else:
                    skipped += 1
    return checked, skipped


def _coverage(fx, pairs, tenors):
    missing = [(pr, t) for pr in pairs for t in tenors
               if (fx.get(pr, {}).get(t) is None
                   or fx[pr][t].spot is None or fx[pr][t].points is None)]
    total = len(pairs) * len(tenors)
    print(f"FX coverage: {total - len(missing)}/{total} pair-tenors")
    if missing:
        # An unchecked pair is NOT a clean pair -- say so, or a gap in the market
        # data reads as a clean bill of health.
        print(f"  ! {len(missing)} pair-tenor(s) had no FX data and were NOT "
              f"checked:")
        for pr, t in missing[:10]:
            print(f"      {pr} {t}")
        if len(missing) > 10:
            print(f"      ... {len(missing)-10} more")
        print("  Add the ticker to fx_tickers.json as \"PAIR|TENOR\": "
              "\"TICKER Curncy\" and rerun.")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
