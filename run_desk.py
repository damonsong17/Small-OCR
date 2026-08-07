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
from pathlib import Path

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
    p.add_argument("--ref-spread-bps", type=float, default=0.0,
                   help="Assumed HALF spread (bps) around a one-sided reference "
                        "rate (the internal MM email quotes one number, not a "
                        "two-way price). 0 uses the reference rate on both "
                        "sides. Routes that use it are flagged [INDICATIVE].")
    p.add_argument("--no-reference", action="store_true",
                   help="Ignore one-sided reference rates entirely instead of "
                        "widening them into an indicative two-way price.")
    p.add_argument("--ftp-use-reference", action="store_true",
                   help="Let widened reference rates into the PUBLISHED FTP "
                        "surface. Off by default: FTP is quoted to the bank and "
                        "must not rest on an assumed spread.")
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
    channels = {}   # keep each source separate -- merging hides cross-channel arb
    if args.db and args.date:
        from quote_ocr.store import QuoteStore
        with QuoteStore(args.db) as store:
            for src in store.sources(args.date):
                channels[src] = sources.surface_from_store(
                    store, args.date, ccys, excl, source=src)
    for path in args.txt:
        channels[Path(path).stem] = sources.surface_from_text(path, excl)
    for path in args.docx:
        from quote_ocr.docx_reader import parse_docx
        surf_d, meta_d = parse_docx(path)
        channels[Path(path).stem] = surf_d
        if meta_d.get("reference_only"):
            print(f"    note: {', '.join(meta_d['reference_only'])} are single "
                  f"reference rates (stored as 'mid', NOT bid==offer)")
        if meta_d.get("settle"):
            print(f"    note: settlement {meta_d['settle']} -- confirm it matches "
                  f"the other channels before comparing")
    if args.quote:
        channels["manual"] = sources.surface_from_lines(args.quote, "manual quotes")
    if not channels:
        print("(no --db/--txt given; using demo funding surface)")
        channels["demo"] = _demo_surface()

    # One-sided reference rates: widen to an indicative two-way price so the
    # internal book can still be compared against AFS, and remember exactly
    # which sides we invented so those routes are never shown as firm.
    indicative = {}
    if not args.no_reference:
        for name, surf in list(channels.items()):
            widened, ind = sources.apply_reference_sides(surf, args.ref_spread_bps)
            if ind:
                channels[name], indicative[name] = widened, ind
                ccys = sorted({c for c, _, _ in ind})
                print(f"  {name}: {', '.join(ccys)} are one-sided reference rates "
                      f"-> widened by +/-{args.ref_spread_bps:g} bps for scanning; "
                      f"results marked [INDICATIVE]")
    surface = sources.merge_surfaces(*channels.values())

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
                                  allow_mismatch=not args.no_mismatch,
                                  indicative=_merged_indicative(
                                      channels, indicative, surface))
    _print_opps(opps)

    # ---- 3b. arbitrage BETWEEN sources (internal vs AFS etc.) ---------------
    if len(channels) > 1:
        print(f"channels: {', '.join(channels)}")
        x = arb.scan_across_channels(channels, fx, pairs, tenors,
                                     threshold_bps=args.threshold,
                                     allow_mismatch=not args.no_mismatch,
                                     indicative=indicative)
        _print_opps(x, title="CROSS-CHANNEL ARBITRAGE (borrow one source, lend another)")

    # ---- 4. FTP surface, made arbitrage free --------------------------------
    # The FTP surface is PUBLISHED, so it is built from firm two-way quotes
    # only. A widened reference rate is fine for spotting an opportunity to
    # investigate; it must not silently become a price we quote to the bank.
    ftp_input = surface
    merged_ind = _merged_indicative(channels, indicative, surface)
    if merged_ind and not args.ftp_use_reference:
        ftp_input = _drop_keys(surface, merged_ind)
        ccys = sorted({c for c, _, _ in merged_ind})
        print(f"FTP input: dropped indicative sides for {', '.join(ccys)} "
              f"(one-sided reference rates). Pass --ftp-use-reference to keep "
              f"them, knowing the published price would rest on an assumed spread.")
    ftp_surface, adjustments = ftp_mod.build_ftp(
        ftp_input, fx, pairs, tenors, margin_bps=args.margin_bps, mode=args.mode)
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


def _merged_indicative(channels, indicative, merged):
    """Keys whose WINNING value in the merged surface came from a reference rate.

    It is not enough that some channel quotes the currency firmly: merge_surfaces
    keeps the lowest offer / highest bid, and a widened reference rate can win.
    So compare values -- a merged price is firm only when some channel's genuine
    quote actually equals it.
    """
    out = set()
    for ccy, sides in merged.items():
        for side in ("bid", "offer"):
            for t, v in sides.get(side, {}).items():
                key = (ccy.upper(), side, t)
                firm = any(
                    key not in indicative.get(name, set())
                    and abs(surf.get(ccy, {}).get(side, {}).get(t, float("nan")) - v) < 1e-12
                    for name, surf in channels.items()
                    if t in surf.get(ccy, {}).get(side, {})
                )
                if not firm:
                    out.add(key)
    return out


def _drop_keys(surface, keys):
    """Copy of the surface without the given (ccy, side, tenor) entries."""
    out = {}
    for ccy, sides in surface.items():
        e = {}
        for side in ("bid", "offer"):
            e[side] = {t: v for t, v in sides.get(side, {}).items()
                       if (ccy.upper(), side, t) not in keys}
        out[ccy] = e
    return out


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


def _print_opps(opps, title="ARBITRAGE"):
    print(f"=== {title}: {len(opps)} opportunity(ies) ===")
    if not opps:
        print("  none above threshold.\n")
        return
    cols = [("pair", 8), ("pnl_bps", 9), ("flags", 12), ("risk_type", 40),
            ("legs", 52), ("detail", 86)]
    print("  ".join(h.ljust(w) for h, w in cols))
    print("-" * 200)
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
        print("  ".join(str(d[h])[:w].ljust(w) for h, w in cols))
    if n_ind:
        print(f"\n  INDIC = a leg came from a one-sided reference rate widened "
              f"into a two-way price. Indicative only -- confirm the real "
              f"two-way price before trading.")
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
