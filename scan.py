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
    p.add_argument("--source", default="",
                   help="Restrict to ONE source in the store (AFS, MM, MP). "
                        "Default: every source, combined best-of. Use "
                        "run_desk.py to find arbitrage BETWEEN sources.")
    p.add_argument("--channel", default="",
                   help="Label for the report. Defaults to the sources actually "
                        "used, so the heading can never name the wrong desk.")
    p.add_argument("--pairs", default="", help="FX pairs to check (explicit).")
    p.add_argument("--ccy", default="USD,CNH,CHF,EUR,HKD",
                   help="Currencies; all pair combinations are checked "
                        "(ignored if --pairs is given).")
    p.add_argument("--tenors", default="auto",
                   help="Tenors to scan. 'auto' (default) uses every tenor the "
                        "quote sources actually contain -- never hardcode.")
    p.add_argument("--threshold", type=float, default=0.5, help="Min bps to flag.")
    p.add_argument("--no-mismatch", action="store_true",
                   help="Only same-tenor round trips.")
    p.add_argument("--live", action="store_true", help="Use the Bloomberg terminal.")
    args = p.parse_args(argv)

    if args.pairs.strip():
        pairs = [x.strip() for x in args.pairs.split(",") if x.strip()]
    else:
        from quote_ocr.bloomberg import all_pairs
        pairs = all_pairs([c.strip().upper() for c in args.ccy.split(",") if c.strip()])
    currencies = sorted({c for pr in pairs for c in (pr[:3], pr[3:])})
    print(f"pairs: {', '.join(pairs)}")

    # 1) channel funding surface (from OCR store, or demo)
    label = args.channel
    if args.db and args.date:
        from quote_ocr import sources as _src
        from quote_ocr.store import QuoteStore
        with QuoteStore(args.db) as store:
            used = [args.source] if args.source else store.sources(args.date)
            # Per source, then an explicit best-of merge (lowest offer, highest
            # bid). Loading every source into one surface would let whichever
            # row happened to be read last win, which is not a price anyone
            # quoted. run_desk.py keeps them separate to trade BETWEEN sources.
            per_source = [_src.surface_from_store(store, args.date, currencies,
                                                  source=s) for s in used]
            surface = _src.merge_surfaces(*per_source) if per_source else {}
            for s, surf in zip(used, per_source):
                for ccy, sides in surf.items():
                    if sides.get("mid"):
                        surface.setdefault(ccy, {"bid": {}, "offer": {}}) \
                               .setdefault("mid", {}).update(sides["mid"])
        # A store now holds AFS, MM and MP together, so the heading must state
        # which sources are actually in the surface rather than assume AFS.
        label = label or "+".join(used) or "(empty)"
        # One-sided reference rates are not executable; widen them explicitly
        # so they are visible here rather than silently absent.
        surface, ind = _src.apply_reference_sides(surface)
        if ind:
            print(f"  note: {', '.join(sorted({c for c, _, _ in ind}))} include "
                  f"one-sided reference rates -- routes using them are indicative")
    else:
        print("(no --db/--date; using demo funding surface)")
        surface = DEMO_SURFACE
        ind = set()
        label = label or "demo"

    if args.tenors.strip().lower() == "auto":
        from quote_ocr import sources as _src
        tenors = _src.tenors_in(surface)
        print(f"tenors (auto-discovered from the quotes): {', '.join(tenors)}")
    else:
        tenors = [x.strip() for x in args.tenors.split(",") if x.strip()]

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
                                  channel=label, threshold_bps=args.threshold,
                                  allow_mismatch=not args.no_mismatch,
                                  indicative=ind)
    _report(label, opps)


def _report(channel, opps):
    print(f"\n{channel}: {len(opps)} arbitrage opportunity(ies) "
          f"(most profitable first)\n")
    if not opps:
        print("  none above threshold.")
        return
    cols = [("pair", 8), ("pnl_bps", 9), ("flags", 12), ("risk_type", 40),
            ("detail", 86)]
    print("  ".join(h.ljust(w) for h, w in cols))
    print("-" * 132)
    n_ind = 0
    for o in sorted(opps, key=lambda x: -x.pnl_bps):
        d = o.as_row()
        flags = []
        if d["indicative"]:
            flags.append("INDIC")
            n_ind += 1
        if d["mismatch"]:
            flags.append("MISMATCH")
        d["flags"] = "/".join(flags)
        print("  ".join(str(d[h]).ljust(w) for h, w in cols))
    if n_ind:
        print(f"\n  INDIC = a leg came from a one-sided reference rate, not a "
              f"two-way price. Confirm the real quote before trading.")


if __name__ == "__main__":
    raise SystemExit(main())
