#!/usr/bin/env python3
"""Verify Bloomberg FX tickers/fields and print full-precision bid/ask.

Covers every pair from a currency list and reports, per security, either the
values or the reason they are missing (bad ticker / no permission / field N/A).

    python fx_check.py --live
    python fx_check.py --ccy USD,CNH,CHF,EUR,HKD --tenors 1M,3M --live
    python fx_check.py --live --sources "" ,BGN,CMPN     # compare pricing sources
    python fx_check.py --live --raw "USDCHF Curncy,CHF+1M Curncy"
"""
from __future__ import annotations

import argparse

from quote_ocr.bloomberg import (
    FX_TICKERS,
    TENOR_CODE,
    BloombergClient,
    _fwd_key,
    all_pairs,
    demo_mock_fx,
    is_usd_pair,
)

FIELDS = ["PX_BID", "PX_ASK", "PX_LAST"]
INFO_FIELDS = ["PRICING_SOURCE", "CRNCY", "LAST_UPDATE_DT"]


def sec_variants(base_ticker: str, sources) -> list:
    """'USDCHF Curncy' + ['', 'BGN'] -> ['USDCHF Curncy', 'USDCHF BGN Curncy']"""
    root = base_ticker.replace(" Curncy", "")
    out = []
    for s in sources:
        s = s.strip()
        out.append(f"{root} {s} Curncy" if s else f"{root} Curncy")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ccy", default="USD,CNH,CHF,EUR,HKD", help="Currencies.")
    p.add_argument("--tenors", default="1M,3M,6M,1Y", help="Tenors.")
    p.add_argument("--sources", default="", help="Pricing sources to compare, "
                   "comma-separated; empty entry = default (e.g. ',BGN,CMPN').")
    p.add_argument("--raw", default="", help="Comma-separated raw tickers to test.")
    p.add_argument("--info", action="store_true", help="Also pull PRICING_SOURCE etc.")
    p.add_argument("--live", action="store_true", help="Use the Bloomberg terminal.")
    args = p.parse_args(argv)

    ccys = [c.strip().upper() for c in args.ccy.split(",") if c.strip()]
    tenors = [t.strip() for t in args.tenors.split(",") if t.strip()]
    sources = args.sources.split(",") if args.sources else [""]

    if args.raw:
        secs = [s.strip() for s in args.raw.split(",") if s.strip()]
    else:
        secs = []
        # Only USD pairs follow the '<other ccy>+<tenor> Curncy' convention;
        # crosses are derived by triangulation, so they are not checked here.
        for pair in [p for p in all_pairs(ccys) if is_usd_pair(p)]:
            secs += sec_variants(FX_TICKERS["spot"].format(pair=pair), sources)
            fwd = _fwd_key(pair)
            for t in tenors:
                tk = FX_TICKERS["outright"].format(fwd=fwd, tenor=TENOR_CODE.get(t, t))
                secs += sec_variants(tk, sources)

    fields = FIELDS + (INFO_FIELDS if args.info else [])
    client = BloombergClient() if args.live else demo_mock_fx()
    print(f"source: {'LIVE terminal' if args.live else 'offline mock'} | "
          f"{len(secs)} securities\n")

    with client as c:
        data = c.reference(secs, fields)

    ok = bad = 0
    for s in secs:
        rec = data.get(s)
        if rec is None:
            print(f"  {s:26} -> NO RESPONSE")
            bad += 1
            continue
        if rec.get("__error__"):
            print(f"  {s:26} -> ERROR {rec['__error__'][:90]}")
            bad += 1
            continue
        bid, ask = rec.get("PX_BID"), rec.get("PX_ASK")
        if bid is None and ask is None:
            note = rec.get("__fieldErrors__") or "no PX_BID/PX_ASK"
            last = rec.get("PX_LAST")
            print(f"  {s:26} -> {note if not last else f'PX_LAST={last:.6f} (no bid/ask)'}")
            bad += 1
            continue
        extra = ""
        if args.info:
            extra = f"  src={rec.get('PRICING_SOURCE')}  upd={rec.get('LAST_UPDATE_DT')}"
        print(f"  {s:26} -> bid={_f(bid)}  ask={_f(ask)}{extra}")
        ok += 1

    print(f"\n{ok} ok, {bad} missing/error")


def _f(v):
    return "-" if v is None else f"{v:.6f}"


if __name__ == "__main__":
    raise SystemExit(main())
