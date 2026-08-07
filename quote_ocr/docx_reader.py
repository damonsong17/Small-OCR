"""Read quote tables out of .docx files -- stdlib only, no new dependency.

Internal rate emails arrive as Lotus Notes messages pasted into Word. Those
documents contain REAL tables, so parsing them directly is exact; printing to
PDF and running OCR would throw that structure away and reintroduce errors.

A .docx is a zip containing word/document.xml, so zipfile + xml.etree is enough
(python-docx would pull in lxml, which the offline bundle does not need).

Two layouts are handled, both seen in practice:

  A. currency columns, one reference rate each -- NOT a two-way price:
         Tenor | USD  | EUR
         1M    | 3.85 | 2.40
  B. bid/offer columns for the currency named in a preceding title row:
         USD RATES (%)
         Tenor | BID  | OFFER
         1M    | 3.80 | 3.90

Settlement basis differs between these emails (money-market reference is T+0,
the other is T+2). That is recorded and reported, never silently mixed.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_TENOR_RE = re.compile(r"^\s*(O/?N|T/?N|S/?N|\d{1,2}\s*[DWMY])\s*$", re.I)
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_CCY_RE = re.compile(r"^[A-Z]{3}$")
_SETTLE_RE = re.compile(r"T\+\s*(\d)", re.I)


def _cell_text(tc) -> str:
    return " ".join("".join(t.text or "" for t in p.iter(W + "t")).strip()
                    for p in tc.iter(W + "p")).strip()


def read_tables(path: str) -> List[List[List[str]]]:
    """Every table in the document as a list of rows of cell strings."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    tables = []
    for tbl in root.iter(W + "tbl"):
        rows = []
        for tr in tbl.findall(W + "tr"):
            cells = []
            for tc in tr.findall(W + "tc"):
                # Skip only the continuation half of a VERTICALLY merged cell;
                # never de-duplicate on text -- two columns legitimately hold
                # the same number, and dropping one silently loses a currency.
                vm = tc.find(f"{W}tcPr/{W}vMerge")
                if vm is not None and vm.get(W + "val") in (None, "continue"):
                    continue
                cells.append(_cell_text(tc))
            rows.append(cells)
        tables.append(rows)
    return tables


def _norm_tenor(s: str) -> Optional[str]:
    from .parser import canonical_tenor
    m = _TENOR_RE.match(s or "")
    if not m:
        return None
    return canonical_tenor(m.group(1))


def parse_docx(path: str, default_currency: str = "",
               verbose: bool = True) -> Tuple[Dict, Dict]:
    """Return (surface, meta).

    surface: {ccy: {'bid': {tenor: rate}, 'offer': {tenor: rate}}} in decimals.
    meta:    {'settle': 'T+0'|'T+2'|'', 'sections': [...], 'reference_only': [ccy]}

    A single reference rate is stored ONLY under ``mid`` (never as bid == offer,
    which would imply a zero spread and manufacture arbitrage) and the currency
    is listed under ``reference_only``.
    """
    surface: Dict = {}
    meta: Dict = {"settle": "", "sections": [], "reference_only": [], "skipped": []}

    def put(ccy, tenor, bid, offer, mid=None):
        e = surface.setdefault(ccy.upper(), {"bid": {}, "offer": {}, "mid": {}})
        if bid is not None:
            e["bid"][tenor] = bid / 100.0
        if offer is not None:
            e["offer"][tenor] = offer / 100.0
        if mid is not None:
            # A single reference rate is NOT a two-way price. Storing it as
            # bid == offer would imply a zero spread and manufacture arbitrage
            # against any genuine two-way quote, so it lives on its own side.
            e["mid"][tenor] = mid / 100.0

    section = ""            # last non-table-ish title row, e.g. "USD RATES (%)"
    columns: List[str] = []  # currency per data column, or ['BID','OFFER']
    col_ccy = default_currency
    skip_block = False

    for table in read_tables(path):
        for row in table:
            cells = [c.strip() for c in row if c is not None]
            joined = " ".join(cells)
            if not joined.strip():
                columns = []          # blank row ends a sub-table
                continue

            m = _SETTLE_RE.search(joined)
            if m and not meta["settle"]:
                meta["settle"] = f"T+{m.group(1)}"

            first = cells[0] if cells else ""

            # header row: "Tenor | USD | EUR"  or  "Tenor | BID | OFFER"
            if first.lower().startswith("tenor"):
                rest = [c.upper() for c in cells[1:] if c]
                columns = rest
                # NOTE: do not clear skip_block here -- a skipped section (e.g.
                # USD SENIOR BOND) has its own "Tenor | ..." header, and
                # resetting would let its 3Y/5Y rows leak into the funding curve.
                if rest and all(_CCY_RE.match(c) for c in rest):
                    meta["sections"].append(f"{section or 'rates'}: {', '.join(rest)}")
                elif rest:
                    meta["sections"].append(f"{section or 'rates'}: {col_ccy} {'/'.join(rest)}")
                continue

            # a title row (single cell, no numbers) sets the context
            if len(cells) == 1 and not _NUM_RE.match(first):
                section = first
                mm = re.match(r"^([A-Z]{3})\b", first.upper())
                if mm:
                    col_ccy = mm.group(1)
                # non-money-market blocks are not funding quotes
                skip_block = bool(re.search(r"BOND|SENIOR|CD\b", first, re.I))
                if skip_block:
                    meta["skipped"].append(first)
                columns = []
                continue

            tenor = _norm_tenor(first)
            if tenor is None or not columns or skip_block:
                continue

            vals = []
            for c in cells[1:]:
                vals.append(float(c) if _NUM_RE.match(c) else None)

            if all(_CCY_RE.match(c) for c in columns):
                # layout A: one reference rate per currency column
                for ccy, v in zip(columns, vals):
                    if v is None:
                        continue
                    put(ccy, tenor, None, None, mid=v)   # one-sided reference
                    if ccy not in meta["reference_only"]:
                        meta["reference_only"].append(ccy)
            else:
                # layout B: BID / OFFER for the section's currency
                bid = offer = None
                for name, v in zip(columns, vals):
                    if "BID" in name:
                        bid = v
                    elif "OFFER" in name or "ASK" in name:
                        offer = v
                if bid is None and offer is None and vals:
                    if col_ccy not in meta["reference_only"]:
                        meta["reference_only"].append(col_ccy)
                    if col_ccy:
                        put(col_ccy, tenor, None, None, mid=vals[0])
                    continue
                if col_ccy:
                    put(col_ccy, tenor, bid, offer)

    if verbose:
        name = Path(path).name
        ccys = ", ".join(sorted(surface)) or "-"
        print(f"  {name}: {ccys}"
              + (f" | settlement {meta['settle']}" if meta["settle"] else "")
              + (f" | reference-only (not two-way): {', '.join(meta['reference_only'])}"
                 if meta["reference_only"] else "")
              + (f" | skipped: {', '.join(meta['skipped'])}" if meta["skipped"] else ""))
    return surface, meta


def surface_from_docx(path: str, default_currency: str = "",
                      verbose: bool = True) -> Dict:
    return parse_docx(path, default_currency, verbose)[0]
