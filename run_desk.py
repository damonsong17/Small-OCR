#!/usr/bin/env python3
"""Desk run: scan every currency pair for arbitrage AND publish an FTP surface.

Run this before a trade so the market leg uses the latest Bloomberg data.

    # offline (mock FX) -- develop/verify
    python run_desk.py --db data/output/quotes.db --date 2026-07-16

    # live terminal
    python run_desk.py --db data/output/quotes.db --date 2026-07-16 --live

    # extra channels from text files (future sources)
    python run_desk.py --txt hq_funding.txt --txt broker2.txt --live

Counterparties we have no KYC relationship with (Korean / Taiwanese / Indian /
ISLAMIC on the AFS sheet) are excluded automatically: their quotes are real but
we cannot obtain those prices, so trading on them would flag arbitrage we could
never execute. Edit segments.json to change access as KYC relationships change.
"""
from __future__ import annotations

import argparse
import csv

from quote_ocr import arb, ftp as ftp_mod, sources
from quote_ocr.bloomberg import (
    BloombergClient,
    all_pairs,
    build_fx_all,
    demo_mock_fx,
)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=None, help="quotes.db (OCR channel).")
    p.add_argument("--date", default=None, help="Quote date, e.g. 2026-07-16.")
    p.add_argument("--txt", action="append", default=[],
                   help="Extra quote file (.txt/.csv); repeatable.")
    p.add_argument("--docx", action="append", default=[],
                   help="Internal rate email saved as .docx; repeatable. "
                        "Read directly (exact) -- do NOT print to PDF and OCR.")
    p.add_argument("--quote", action="append", default=[],
                   help="Ad-hoc quote, e.g. --quote \"USD,3M,4.00,4.10\"; "
                        "repeatable. For a broker's quote typed in on the spot.")
    p.add_argument("--ccy", default="USD,CNH,CHF,EUR,HKD", help="Currencies.")
    p.add_argument("--tenors", default="auto",
                   help="Tenors to scan. 'auto' (default) uses every tenor the "
                        "quote sources actually contain -- never hardcode.")
    p.add_argument("--threshold", type=float, default=0.5, help="Min bps to flag.")
    p.add_argument("--no-mismatch", action="store_true",
                   help="Only same-tenor round trips (default allows a tenor "
                        "mismatch, flagged as gap/rollover risk).")
    p.add_argument("--margin-bps", type=float, default=5.0, help="FTP margin (bps).")
    p.add_argument("--mode", default="bid", choices=["bid", "offer"],
                   help="Which side to tighten when enforcing FTP no-arb.")
    p.add_argument("--include-untradeable", action="store_true",
                   help="Use every counterparty, including ones we have no KYC "
                        "with (their prices are not obtainable -- diagnostics only).")
    p.add_argument("--access", default=sources.ACCESS_FILE,
                   help="Counterparty KYC access config (JSON).")
    p.add_argument("--out", default=None, help="Write FTP surface to this CSV.")
    p.add_argument("--tickers", default="fx_tickers.json",
                   help="Cross-pair ticker overrides (see fx_resolve.py).")
    p.add_argument("--live", action="store_true", help="Use the Bloomberg terminal.")
    args = p.parse_args(argv)

    sources.load_access(args.access)
    ccys = [c.strip().upper() for c in args.ccy.split(",") if c.strip()]
    pairs = all_pairs(ccys)
    excl = set() if args.include_untradeable else None

    # ---- 1. channel surfaces -------------------------------------------------
    surfaces = []
    if args.db and args.date:
        from quote_ocr.store import QuoteStore
        with QuoteStore(args.db) as store:
            surfaces.append(sources.surface_from_store(store, args.date, ccys, excl))
    for path in args.txt:
        surfaces.append(sources.surface_from_text(path, excl))
    for path in args.docx:
        from quote_ocr.docx_reader import parse_docx
        surf_d, meta_d = parse_docx(path)
        if meta_d.get("reference_only"):
            print(f"    note: {', '.join(meta_d['reference_only'])} are single "
                  f"reference rates (bid==offer), not tradeable two-way prices")
        if meta_d.get("settle"):
            print(f"    note: settlement {meta_d['settle']} -- confirm it matches "
                  f"the other channels before comparing")
        surfaces.append(surf_d)
    if args.quote:
        surfaces.append(sources.surface_from_lines(args.quote, "manual quotes"))
    if not surfaces:
        print("(no --db/--txt given; using demo funding surface)")
        surfaces.append(_demo_surface())
    surface = sources.merge_surfaces(*surfaces)

    # Follow the data: scan every tenor the sources actually quote.
    if args.tenors.strip().lower() == "auto":
        tenors = sources.tenors_in(surface)
        print(f"tenors (auto-discovered from the quotes): {', '.join(tenors)}")
    else:
        tenors = [t.strip() for t in args.tenors.split(",") if t.strip()]

    print(f"currencies: {', '.join(ccys)}")
    print(f"pairs ({len(pairs)}): {', '.join(pairs)}")
    print()

    # ---- 2. FX market --------------------------------------------------------
    from quote_ocr import tickers as tk
    cross_tickers = tk.load_overrides(args.tickers)
    if cross_tickers:
        print(f"using {len(cross_tickers)} cross ticker mapping(s) from {args.tickers}")
    client = BloombergClient() if args.live else demo_mock_fx()
    with client as c:
        fx = build_fx_all(c, pairs, tenors, cross_tickers=cross_tickers)
    _fx_coverage(fx, pairs, tenors)

    # ---- 3. arbitrage across every pair -------------------------------------
    opps = arb.scan_surface_noarb(surface, fx, pairs, tenors,
                                  channel="CHANNELS", threshold_bps=args.threshold,
                                  allow_mismatch=not args.no_mismatch)
    _print_opps(opps)

    # ---- 4. FTP surface, made arbitrage free --------------------------------
    ftp_surface, adjustments = ftp_mod.build_ftp(
        surface, fx, pairs, tenors, margin_bps=args.margin_bps, mode=args.mode)
    _print_ftp(ftp_surface, tenors, adjustments)

    # ---- 5. verify the published FTP cannot be arbitraged --------------------
    # Same-tenor only: a maturity mismatch is a gap position with real risk,
    # not a closed arbitrage, so it must not block publishing.
    residual = arb.scan_surface_noarb(ftp_surface, fx, pairs, tenors,
                                      channel="OUR_FTP", kind="ftp_self_arb",
                                      threshold_bps=0.01, allow_mismatch=False)
    if residual:
        print(f"\n!! FTP STILL ARBITRAGEABLE: {len(residual)} route(s) -- do not publish")
        for o in residual[:5]:
            print("   ", o.as_row()["detail"])
    else:
        print("\nFTP check: no cross-currency arbitrage remaining -> safe to publish")

    if args.out:
        try:
            _write_csv(ftp_surface, tenors, args.out)
            print(f"wrote {args.out}")
        except PermissionError:
            print(f"\n! could not write {args.out} -- it is open in another "
                  f"program (Excel?). Close it, or pass a different --out. "
                  f"Everything above is still valid.")


def _fx_coverage(fx, pairs, tenors):
    """Report coverage explicitly -- a missing quote is never swallowed."""
    from quote_ocr import tickers as tk
    missing = [(pr, t) for pr in pairs for t in tenors
               if (fx.get(pr, {}).get(t) is None
                   or fx[pr][t].spot is None or fx[pr][t].points is None)]
    total = len(pairs) * len(tenors)
    print(f"FX coverage: {total - len(missing)}/{total} pair-tenors")
    if missing:
        print("  MISSING (every candidate ticker failed, and no USD legs to "
              "triangulate from):")
        for pr, t in missing[:15]:
            cands = ", ".join(c.replace(" Curncy", "")
                              for c in tk.candidates(pr, t))
            print(f"    {pr} {t:4} -- tried: {cands}")
        if len(missing) > 15:
            print(f"    ... {len(missing)-15} more")
        print("  Fix without rebuilding: add the correct ticker to "
              "fx_tickers.json as \"PAIR|TENOR\": \"TICKER Curncy\" and rerun.")
    print()


def _print_opps(opps):
    print(f"=== ARBITRAGE: {len(opps)} opportunity(ies) ===")
    if not opps:
        print("  none above threshold.\n")
        return
    cols = [("pair", 8), ("pnl_bps", 9), ("risk_type", 40), ("detail", 86)]
    print("  ".join(h.ljust(w) for h, w in cols))
    print("-" * 132)
    for o in sorted(opps, key=lambda x: -x.pnl_bps):
        d = o.as_row()
        print("  ".join(str(d[h])[:w].ljust(w) for h, w in cols))
    print()


def _print_ftp(ftp_surface, tenors, adjustments):
    from quote_ocr.pricing import lookup_tenor
    print("=== FTP SURFACE (%, arbitrage-free) ===")
    hdr = "  ccy   " + "".join(f"{t:>18}" for t in tenors)
    print(hdr)
    print("-" * len(hdr))
    for ccy in sorted(ftp_surface):
        cells = []
        for t in tenors:
            b = lookup_tenor(ftp_surface[ccy]["bid"], t)
            o = lookup_tenor(ftp_surface[ccy]["offer"], t)
            cells.append(f"{_p(b)}/{_p(o)}".rjust(18))
        print(f"  {ccy:5} " + "".join(cells))
    if adjustments:
        print(f"\n  {len(adjustments)} no-arb adjustment(s) applied:")
        for a in adjustments[:10]:
            r = a.as_row()
            print(f"    {r['ccy']} {r['tenor']} {r['side']}: "
                  f"{r['before_pct']:.4f} -> {r['after_pct']:.4f} "
                  f"({r['moved_bps']:+.1f} bps)  [{r['reason']}]")
        if len(adjustments) > 10:
            print(f"    ... {len(adjustments)-10} more")


def _p(v):
    return "-" if v is None else f"{v*100:.4f}"


def _write_csv(ftp_surface, tenors, path):
    from quote_ocr.pricing import lookup_tenor
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["currency", "tenor", "ftp_bid_pct", "ftp_offer_pct"])
        for ccy in sorted(ftp_surface):
            for t in tenors:
                b = ftp_surface[ccy]["bid"].get(t)
                o = ftp_surface[ccy]["offer"].get(t)
                if b is None and o is None:
                    continue
                w.writerow([ccy, t,
                            "" if b is None else round(b * 100, 6),
                            "" if o is None else round(o * 100, 6)])


def _demo_surface():
    return {
        "USD": {"bid": {"1M": 0.0380, "3M": 0.0400, "6M": 0.0411, "1Y": 0.0422},
                "offer": {"1M": 0.0385, "3M": 0.0405, "6M": 0.0418, "1Y": 0.0445}},
        "EUR": {"bid": {"1M": 0.0230, "3M": 0.0240, "6M": 0.0250, "1Y": 0.0270},
                "offer": {"1M": 0.0250, "3M": 0.0260, "6M": 0.0275, "1Y": 0.0285}},
        "CNH": {"bid": {"1M": 0.0125, "3M": 0.0135, "6M": 0.0140, "1Y": 0.0130},
                "offer": {"1M": 0.0155, "3M": 0.0155, "6M": 0.0160, "1Y": 0.0170}},
    }


if __name__ == "__main__":
    raise SystemExit(main())
