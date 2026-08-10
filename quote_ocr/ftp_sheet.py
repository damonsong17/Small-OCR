"""Read and write the desk's FTP workbook (the published quote sheet).

The sheet is a wide grid: one row per tenor, and two blocks of currency
columns -- ``Offer Side`` then ``Bid Side``:

    Tenor | Offer Side                    | Bid Side
          | USD  CNH  HKD  EUR  CHF       | USD  CNH  HKD  EUR  CHF
    O/N   | ...  ...  -    ...  ...       | ...  ...  -    ...  ...

Two jobs, both needed:

  * **read** an FTP that has already been published, so the arbitrage engine
    can check it (nobody may borrow one currency from us, FX-swap it, and place
    another back with us at a profit);
  * **write** a computed FTP back into the same layout.

Writing goes into a COPY OF THE REAL WORKBOOK rather than a fresh file: the
sheet also carries settlement notes, the counterparty spread table and the
market-reference block, none of which this pipeline models. Rebuilding the file
would silently drop them; filling in the cells cannot.

Percent handling is the trap here. Excel stores a percent-formatted 3.85% as
0.0385, but a plain cell holding 3.85 means the same rate. Getting it wrong is
a 100x error in the middle of a funding curve, so the interpretation used for
every cell is decided from its number format and reported, never guessed
silently.
"""
from __future__ import annotations

import datetime
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_OFFER_HDR = re.compile(r"offer\s*side", re.I)
_BID_HDR = re.compile(r"bid\s*side", re.I)
_CCY_RE = re.compile(r"^[A-Z]{3}$")
_TENOR_RE = re.compile(r"^\s*(O/?N|T/?N|S/?N|\d{1,2}\s*[DWMY])\s*$", re.I)
# 'x.xx%', 'X.XX', '-' -- a template placeholder, NOT a rate
_PLACEHOLDER_RE = re.compile(r"^[\sxX.\-–—%]*$")


def _require_openpyxl():
    try:
        import openpyxl  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            f"reading/writing the FTP workbook needs openpyxl ({e}).\n"
            f"  online:  pip install openpyxl\n"
            f"  offline: it is already in requirements.txt and the wheel bundle"
        )


def _norm_tenor(s) -> Optional[str]:
    from .parser import canonical_tenor
    m = _TENOR_RE.match(str(s or ""))
    return canonical_tenor(m.group(1)) if m else None


def _rate(cell) -> Tuple[Optional[float], str]:
    """(rate as a decimal, note). Returns (None, why) when there is no rate.

    A percent-formatted number is already a fraction; a bare number is read as
    percent. Placeholders and '-' come back as None with a reason, so an unfilled
    cell can never be mistaken for 0%.
    """
    v = cell.value
    if v is None:
        return None, "empty"
    if isinstance(v, str):
        s = v.strip()
        if not s or _PLACEHOLDER_RE.match(s):
            return None, f"placeholder {s!r}" if s else "empty"
        s2 = s.rstrip("%").replace(",", "").strip()
        try:
            num = float(s2)
        except ValueError:
            return None, f"not a number: {s!r}"
        # text like "3.85%" or "3.85" -- both mean 3.85%
        return num / 100.0, ""
    if isinstance(v, (int, float)):
        if "%" in (cell.number_format or ""):
            return float(v), ""          # Excel already stores 0.0385
        return float(v) / 100.0, ""      # bare 3.85 means 3.85%
    return None, f"unsupported cell type {type(v).__name__}"


def read_ftp_sheet(path: str, sheet: str = None,
                   verbose: bool = True) -> Tuple[Dict, Dict]:
    """Parse a published FTP workbook into (surface, meta).

    surface: {ccy: {'bid': {tenor: rate}, 'offer': {tenor: rate}}}, decimals.
    meta:    {'date', 'sheet', 'tenors', 'currencies', 'blank', 'unparsed'}

    Anchored on the 'Offer Side' / 'Bid Side' header text rather than fixed cell
    references, so inserting a row above the table does not silently shift every
    rate by one tenor.
    """
    _require_openpyxl()
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]

    hdr_row = off_cols = bid_cols = None
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 40)):
        offer_at = [c.column for c in row if isinstance(c.value, str) and _OFFER_HDR.search(c.value)]
        bid_at = [c.column for c in row if isinstance(c.value, str) and _BID_HDR.search(c.value)]
        if offer_at and bid_at:
            hdr_row = row[0].row
            off_cols, bid_cols = min(offer_at), min(bid_at)
            break
    if hdr_row is None:
        raise ValueError(
            f"{Path(path).name}: could not find the 'Offer Side' / 'Bid Side' "
            f"header row -- is this the FTP sheet? (pass --sheet to choose one)")

    # the row under the header names the currency of each column
    ccy_row = hdr_row + 1
    offer_map, bid_map = {}, {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(row=ccy_row, column=c).value
        if isinstance(v, str) and _CCY_RE.match(v.strip().upper()):
            (offer_map if c < bid_cols else bid_map)[c] = v.strip().upper()
    if not offer_map or not bid_map:
        raise ValueError(
            f"{Path(path).name}: row {ccy_row} under the header has no currency "
            f"codes; expected e.g. USD | CNH | HKD | EUR | CHF on each side")

    tenor_col = _tenor_column(ws, hdr_row, off_cols)

    surface: Dict = {}
    tenors: List[str] = []
    blank: List[str] = []
    unparsed: List[str] = []

    for r in range(ccy_row + 1, ws.max_row + 1):
        tenor = _norm_tenor(ws.cell(row=r, column=tenor_col).value)
        if tenor is None:
            if tenors:
                break        # the notes paragraph under the grid ends the block
            continue
        tenors.append(tenor)
        for cmap, side in ((offer_map, "offer"), (bid_map, "bid")):
            for col, ccy in cmap.items():
                cell = ws.cell(row=r, column=col)
                rate, why = _rate(cell)
                e = surface.setdefault(ccy, {"bid": {}, "offer": {}})
                if rate is None:
                    where = f"{ccy} {tenor} {side}"
                    (blank if why in ("empty",) or why.startswith("placeholder")
                     else unparsed).append(f"{where} ({why})")
                    continue
                e[side][tenor] = rate

    meta = {
        "date": _sheet_date(ws),
        "sheet": ws.title,
        "tenors": tenors,
        "currencies": sorted(set(offer_map.values()) | set(bid_map.values())),
        "blank": blank,
        "unparsed": unparsed,
        "header_row": hdr_row,
        "tenor_col": tenor_col,
        "offer_cols": offer_map,
        "bid_cols": bid_map,
    }
    if verbose:
        n = sum(len(s["bid"]) + len(s["offer"]) for s in surface.values())
        print(f"  {Path(path).name} [{ws.title}]: {n} rate(s), "
              f"{len(meta['currencies'])} currencies, tenors "
              f"{', '.join(tenors) or '-'}"
              + (f" | as of {meta['date']}" if meta["date"] else ""))
        if blank:
            print(f"  not filled in ({len(blank)}): {', '.join(blank[:8])}"
                  + (" ..." if len(blank) > 8 else ""))
        if unparsed:
            # never silent: an unreadable cell is reported, not treated as zero
            print(f"  ! COULD NOT READ ({len(unparsed)}): {', '.join(unparsed[:8])}"
                  + (" ..." if len(unparsed) > 8 else ""))
    return surface, meta


def _tenor_column(ws, hdr_row: int, first_rate_col: int) -> int:
    """Which column holds the tenor labels.

    The 'Tenor' label may sit on the header row or be merged across it, so fall
    back to the column just left of the first rate column rather than assuming.
    """
    for r in (hdr_row, hdr_row + 1):
        for c in range(1, first_rate_col):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, str) and v.strip().lower().startswith("tenor"):
                return c
    return max(1, first_rate_col - 1)


def _sheet_date(ws) -> str:
    for row in ws.iter_rows(min_row=1, max_row=6):
        for c in row:
            if isinstance(c.value, str) and c.value.strip().lower().startswith("date"):
                for off in (1, 2):
                    v = ws.cell(row=c.row, column=c.column + off).value
                    if isinstance(v, (datetime.datetime, datetime.date)):
                        return v.strftime("%Y-%m-%d")
                    if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v.strip()):
                        return v.strip()[:10]
    return ""


def write_ftp_sheet(surface: Dict, template: str, out: str,
                    date: str = None, sheet: str = None,
                    verbose: bool = True) -> dict:
    """Fill a computed FTP surface into a copy of the real workbook.

    The template is copied first, then only the rate cells are written, so the
    settlement notes, counterparty spread table and market-reference block --
    none of which this pipeline models -- survive untouched.
    """
    _require_openpyxl()
    import openpyxl
    from .pricing import lookup_tenor

    if Path(out).resolve() == Path(template).resolve():
        raise ValueError("refusing to overwrite the template in place; "
                         "give a different --out path")
    shutil.copyfile(template, out)

    # locate the grid on the COPY, using the same anchors as the reader
    _, meta = read_ftp_sheet(out, sheet=sheet, verbose=False)
    wb = openpyxl.load_workbook(out)
    ws = wb[meta["sheet"]]
    hdr_row = meta["header_row"]

    written, missing = 0, []
    r = hdr_row + 2
    while r <= ws.max_row:
        tenor = _norm_tenor(ws.cell(row=r, column=meta["tenor_col"]).value)
        if tenor is None:
            if written or missing:
                break
            r += 1
            continue
        for cmap, side in ((meta["offer_cols"], "offer"), (meta["bid_cols"], "bid")):
            for col, ccy in cmap.items():
                v = lookup_tenor(surface.get(ccy, {}).get(side, {}), tenor)
                cell = ws.cell(row=r, column=col)
                if v is None:
                    # leave the template's own marker; do NOT write 0
                    missing.append(f"{ccy} {tenor} {side}")
                    continue
                cell.value = v if "%" in (cell.number_format or "") else v * 100
                written += 1
        r += 1

    if date:
        for row in ws.iter_rows(min_row=1, max_row=6):
            for c in row:
                if isinstance(c.value, str) and c.value.strip().lower().startswith("date"):
                    tgt = ws.cell(row=c.row, column=c.column + 1)
                    if tgt.coordinate not in {m.coord.split(":")[0]
                                              for m in ws.merged_cells.ranges} or True:
                        try:
                            tgt.value = date
                        except AttributeError:      # merged continuation cell
                            pass
                    break

    wb.save(out)
    if verbose:
        print(f"  wrote {written} rate(s) into {out}")
        if missing:
            print(f"  left blank ({len(missing)}), no rate computed: "
                  + ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else ""))
    return {"written": written, "missing": missing, "out": out}
