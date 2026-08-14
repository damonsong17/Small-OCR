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


# What the first column of a header row is called. Try the spellings actually
# seen before giving up -- a renamed column should not silently drop every row
# under it, and orphan_rows reports whatever this still misses.
_HEADER_LABELS = ("tenor", "period", "term", "maturity", "期限", "天期")


def _is_header_label(s: str) -> bool:
    low = (s or "").strip().lower()
    return any(low.startswith(h) for h in _HEADER_LABELS)


def _norm_tenor(s: str) -> Optional[str]:
    from .parser import canonical_tenor
    m = _TENOR_RE.match(s or "")
    if not m:
        return None
    return canonical_tenor(m.group(1))


def parse_docx(path: str, default_currency: str = "",
               verbose: bool = True, single_side: str = "offer") -> Tuple[Dict, Dict]:
    """Return (surface, meta).

    surface: {ccy: {'bid': {tenor}, 'offer': {tenor}, 'mid': {tenor}}}, decimals.
    meta:    {'settle', 'sections', 'one_sided': [ccy], 'single_side', 'skipped'}

    ``single_side`` says which side a table that quotes ONE number per currency
    actually is. The MM money-market email quotes the **offer** -- the rate we
    can borrow at -- so that is the default: it is a firm, executable side, and
    the absence of a bid correctly means we cannot place funds there.

    It is never stored as bid == offer, which would imply a zero spread and
    manufacture arbitrage against any genuine two-way quote. Pass
    ``single_side="mid"`` for a source that really does publish a mid; that
    lands on its own side and is treated as indicative downstream.
    """
    if single_side not in ("offer", "bid", "mid"):
        raise ValueError(f"single_side must be offer/bid/mid, got {single_side!r}")

    surface: Dict = {}
    meta: Dict = {"settle": "", "sections": [], "one_sided": [], "skipped": [],
                  "single_side": single_side, "other": [], "orphan_rows": []}
    # kept so existing callers/tests that read 'reference_only' still work
    meta["reference_only"] = meta["one_sided"]

    def put(ccy, tenor, bid, offer, one=None):
        e = surface.setdefault(ccy.upper(), {"bid": {}, "offer": {}, "mid": {}})
        if bid is not None:
            e["bid"][tenor] = bid / 100.0
        if offer is not None:
            e["offer"][tenor] = offer / 100.0
        if one is not None:
            e[single_side][tenor] = one / 100.0

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
            if _is_header_label(first):
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
            if tenor is None:
                continue
            if not columns:
                # A tenor row with no header above it: the "Tenor | USD | ..."
                # line was not recognised, so every row under it would vanish.
                # That is what a changed layout looks like -- count it and say so.
                meta["orphan_rows"].append(f"{tenor}: {joined[:60]}")
                continue

            vals = []
            for c in cells[1:]:
                vals.append(float(c) if _NUM_RE.match(c) else None)

            if skip_block:
                # Not a funding quote, so it stays OUT of the surface -- but it
                # is still real data from the email, and seeing it in the CSV is
                # how you check the parse. Keep it, labelled by its section.
                for v in vals:
                    if v is not None:
                        meta["other"].append({"section": section,
                                              "currency": col_ccy, "tenor": tenor,
                                              "rate": v / 100.0})
                        break
                continue

            if all(_CCY_RE.match(c) for c in columns):
                # layout A: ONE number per currency column -- single_side says
                # which side that number is (the MM email quotes the offer)
                for ccy, v in zip(columns, vals):
                    if v is None:
                        continue
                    put(ccy, tenor, None, None, one=v)
                    if ccy not in meta["one_sided"]:
                        meta["one_sided"].append(ccy)
            else:
                # layout B: BID / OFFER for the section's currency
                bid = offer = None
                for name, v in zip(columns, vals):
                    if "BID" in name:
                        bid = v
                    elif "OFFER" in name or "ASK" in name:
                        offer = v
                if bid is None and offer is None and vals:
                    if col_ccy not in meta["one_sided"]:
                        meta["one_sided"].append(col_ccy)
                    if col_ccy:
                        put(col_ccy, tenor, None, None, one=vals[0])
                    continue
                if col_ccy:
                    put(col_ccy, tenor, bid, offer)

    if verbose:
        name = Path(path).name
        ccys = ", ".join(sorted(surface)) or "-"
        print(f"  {name}: {ccys}"
              + (f" | settlement {meta['settle']}" if meta["settle"] else "")
              + (f" | one-sided ({single_side} only): {', '.join(meta['one_sided'])}"
                 if meta["one_sided"] else "")
              + (f" | skipped: {', '.join(meta['skipped'])}" if meta["skipped"] else ""))
        if meta["orphan_rows"]:
            print(f"  ! {len(meta['orphan_rows'])} row(s) had a tenor but no "
                  f"recognised header above them, so they were NOT read: "
                  + "; ".join(meta["orphan_rows"][:4])
                  + (" ..." if len(meta["orphan_rows"]) > 4 else ""))
    return surface, meta


def surface_from_docx(path: str, default_currency: str = "",
                      verbose: bool = True) -> Dict:
    return parse_docx(path, default_currency, verbose)[0]
