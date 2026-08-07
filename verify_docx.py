#!/usr/bin/env python3
"""Check what a .docx rate email parses to -- run this on the REAL (un-anonymised)
files before trusting them.

    python verify_docx.py MM_20260805.docx MP_20260805.docx
    python verify_docx.py MM.docx --raw        # dump the raw table cells too

Prints every currency/tenor/bid/offer that was extracted plus the flags that
matter (settlement basis, reference-only vs two-way, skipped sections), and runs
sanity checks so a silent mis-parse is visible rather than trusted.
"""
from __future__ import annotations

import argparse
import sys

from quote_ocr.docx_reader import parse_docx, read_tables


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", help=".docx rate emails to check.")
    p.add_argument("--raw", action="store_true", help="Also dump raw table cells.")
    p.add_argument("--max-rate", type=float, default=25.0,
                   help="Flag rates above this %% as implausible (default 25).")
    args = p.parse_args(argv)

    bad = 0
    for path in args.files:
        print(f"\n{'='*70}\n{path}\n{'='*70}")
        if args.raw:
            for ti, table in enumerate(read_tables(path)):
                print(f"-- raw table {ti} --")
                for row in table:
                    line = " | ".join(row)
                    print("   " + (line[:150] + " ...") if len(line) > 150 else "   " + line)
        try:
            surf, meta = parse_docx(path, verbose=False)
        except Exception as e:
            print(f"  PARSE FAILED: {e}")
            bad += 1
            continue

        print(f"  settlement: {meta.get('settle') or '(not stated)'}")
        if meta.get("skipped"):
            print(f"  skipped sections: {', '.join(meta['skipped'])}")
        if meta.get("sections"):
            for s in meta["sections"]:
                print(f"  section: {s}")
        print()
        print(f"  {'ccy':5} {'tenor':6} {'bid%':>10} {'offer%':>10} {'mid%':>10}  note")
        print("  " + "-" * 64)
        n = 0
        for ccy in sorted(surf):
            b = surf[ccy]["bid"]
            o = surf[ccy]["offer"]
            m = surf[ccy].get("mid", {})
            for t in sorted(set(b) | set(o) | set(m), key=lambda x: _order(x)):
                bv, ov, mv = b.get(t), o.get(t), m.get(t)
                # A one-sided rate must show up as one-sided here too, otherwise
                # this check would confirm a two-way price that does not exist.
                note = "reference only (one-sided)" if mv is not None else ""
                print(f"  {ccy:5} {t:6} {_f(bv):>10} {_f(ov):>10} {_f(mv):>10}  {note}")
                n += 1
        print(f"\n  {n} quote row(s) for {len(surf)} currency(ies)")
        bad += _checks(surf, meta, args.max_rate)

    print(f"\n{'ALL CHECKS PASSED' if bad == 0 else f'{bad} PROBLEM(S) FOUND'}")
    return 1 if bad else 0


def _checks(surf, meta, max_rate) -> int:
    """Sanity checks that catch a silent mis-parse."""
    problems = []
    if not surf:
        problems.append("no quotes extracted at all")
    for ccy, sides in surf.items():
        mid = sides.get("mid", {})
        for t in set(sides["bid"]) | set(sides["offer"]) | set(mid):
            bv, ov, mv = sides["bid"].get(t), sides["offer"].get(t), mid.get(t)
            for name, v in (("bid", bv), ("offer", ov), ("mid", mv)):
                if v is None:
                    continue
                if not (-0.05 <= v <= max_rate / 100.0):
                    problems.append(f"{ccy} {t} {name} = {v*100:.4f}% is implausible")
            if bv is not None and ov is not None and bv > ov + 1e-9:
                problems.append(f"{ccy} {t}: bid {bv*100:.4f} > offer {ov*100:.4f}")
            # a reference rate must NOT have been duplicated onto both sides
            if mv is not None and (bv is not None or ov is not None):
                problems.append(f"{ccy} {t}: one-sided reference rate also has a "
                                f"bid/offer -- it must stay one-sided")
    if not meta.get("settle"):
        problems.append("settlement basis not found -- confirm T+0 vs T+2 by hand")
    for pr in problems:
        print(f"  ! CHECK: {pr}")
    return len(problems)


def _order(t):
    seq = ["O/N", "T/N", "1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y", "3Y", "5Y"]
    return seq.index(t) if t in seq else 99


def _f(v):
    return "-" if v is None else f"{v*100:.4f}"


if __name__ == "__main__":
    sys.exit(main())
