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
from pathlib import Path

from quote_ocr.docx_reader import parse_docx, read_tables


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", help=".docx rate emails to check.")
    p.add_argument("--raw", action="store_true", help="Also dump raw table cells.")
    p.add_argument("--side", default="offer", choices=["offer", "bid", "mid"],
                   help="Which side a table quoting ONE number per currency is. "
                        "The MM money-market email quotes the offer (the rate we "
                        "borrow at), so 'offer' is the default. Use 'mid' only "
                        "for a source that really publishes a mid.")
    p.add_argument("--max-rate", type=float, default=25.0,
                   help="Flag rates above this %% as implausible (default 25).")
    args = p.parse_args(argv)

    bad = 0
    for path in args.files:
        print(f"\n{'='*70}\n{path}\n{'='*70}")
        # Check the file before touching it: on the offline machine a stack
        # trace is no help, and the usual mistake is a path, not a bad document.
        if not Path(path).is_file():
            print(f"  FILE NOT FOUND: {path}")
            print(f"  (run from the repo root and give the path as it is on "
                  f"disk, e.g. data\\MM\\MM_20260810.docx){_nearby(path)}")
            bad += 1
            continue
        try:
            if args.raw:
                for ti, table in enumerate(read_tables(path)):
                    print(f"-- raw table {ti} --")
                    for row in table:
                        line = " | ".join(row)
                        print("   " + (line[:150] + " ...") if len(line) > 150 else "   " + line)
            surf, meta = parse_docx(path, verbose=False, single_side=args.side)
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
        side = meta.get("single_side", "offer")
        if meta.get("one_sided"):
            print(f"  one-sided ({side} only, no other side): "
                  f"{', '.join(meta['one_sided'])}")
        print()
        print(f"  {'ccy':5} {'tenor':6} {'bid%':>10} {'offer%':>10} {'mid%':>10}  note")
        print("  " + "-" * 66)
        n = 0
        one_sided = {c.upper() for c in meta.get("one_sided", [])}
        for ccy in sorted(surf):
            b = surf[ccy]["bid"]
            o = surf[ccy]["offer"]
            m = surf[ccy].get("mid", {})
            for t in sorted(set(b) | set(o) | set(m), key=lambda x: _order(x)):
                bv, ov, mv = b.get(t), o.get(t), m.get(t)
                # State which side is MISSING: that is what decides whether we
                # can borrow here, place here, or only look.
                note = ""
                if ccy in one_sided:
                    note = {"offer": "offer only - we can borrow, not place",
                            "bid": "bid only - we can place, not borrow",
                            "mid": "mid only - indicative, not executable"}[side]
                print(f"  {ccy:5} {t:6} {_f(bv):>10} {_f(ov):>10} {_f(mv):>10}  {note}")
                n += 1
        print(f"\n  {n} quote row(s) for {len(surf)} currency(ies)")
        bad += _checks(surf, meta, args.max_rate)

    print(f"\n{'ALL CHECKS PASSED' if bad == 0 else f'{bad} PROBLEM(S) FOUND'}")
    return 1 if bad else 0


def _nearby(path: str) -> str:
    """Point at the .docx files that ARE there, so the fix is one glance away."""
    p = Path(path)
    for folder in (p.parent, Path("data") / p.stem.split("_")[0], Path("data"), Path(".")):
        try:
            found = sorted(x for x in folder.glob("**/*.docx") if x.is_file())
        except OSError:
            continue
        if found:
            shown = ", ".join(str(x) for x in found[:6])
            more = f" ... (+{len(found)-6} more)" if len(found) > 6 else ""
            return f"\n  .docx files found under {folder}: {shown}{more}"
    return ""


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
            # a one-sided quote must stay one-sided: filling the other side in
            # would invent a spread nobody quoted
            if ccy.upper() in {c.upper() for c in meta.get("one_sided", [])}:
                filled = [n for n, v in (("bid", bv), ("offer", ov), ("mid", mv))
                          if v is not None]
                if len(filled) > 1:
                    problems.append(f"{ccy} {t}: one-sided quote ended up on "
                                    f"{'+'.join(filled)} -- it must stay one-sided")
    if meta.get("orphan_rows"):
        problems.append(f"{len(meta['orphan_rows'])} row(s) had a tenor but no "
                        f"recognised header, so they were NOT read: "
                        + "; ".join(meta["orphan_rows"][:3]))
    if meta.get("other"):
        # not an error -- state it so the row count adds up against the document
        print(f"  note: {len(meta['other'])} non-funding row(s) kept for the "
              f"record (segment "
              f"{', '.join(sorted({o['section'] for o in meta['other']}))})")
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
