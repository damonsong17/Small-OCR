"""Load funding surfaces from any quote source into one common shape.

A *surface* is ``{ccy: {'bid': {tenor: rate}, 'offer': {tenor: rate}}}`` with
rates as decimals (3.85% -> 0.0385). Sources supported today:

  * the OCR SQLite store (``quotes.db``),
  * a plain text / CSV file (for a channel that arrives as text, or manual entry).

Segments that are quoted but NOT tradeable for us (we cannot obtain those
prices) are excluded by default -- see ``UNTRADEABLE_SEGMENTS``.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# AFS quotes these blocks but we cannot transact them -> never use for pricing
# or arbitrage. Matched case-insensitively; OCR typo variants included.
UNTRADEABLE_SEGMENTS = {
    "korean", "korea",
    "taiwanese", "taiwan",
    "indian", "india",
    "islamic", "islamiic", "israel", "israeli",
}


def is_tradeable(segment: Optional[str]) -> bool:
    return (segment or "").strip().lower() not in UNTRADEABLE_SEGMENTS


def empty_surface() -> Dict:
    return {}


def _put(surface: Dict, ccy: str, tenor: str, bid, offer) -> None:
    e = surface.setdefault(ccy.upper(), {"bid": {}, "offer": {}})
    if bid is not None:
        e["bid"][tenor] = bid
    if offer is not None:
        e["offer"][tenor] = offer


def _num(x) -> Optional[float]:
    try:
        v = float(str(x).strip())
    except (TypeError, ValueError):
        return None
    return v


def surface_from_store(
    store, date: str, currencies: Iterable[str],
    exclude_segments: Optional[set] = None,
) -> Dict:
    """Build a surface from the OCR store, skipping untradeable segments."""
    excl = UNTRADEABLE_SEGMENTS if exclude_segments is None else exclude_segments
    surface: Dict = {}
    skipped = 0
    for ccy in currencies:
        for r in store.by_currency(date, ccy):
            seg = (r["segment"] or "").strip().lower()
            if seg in excl:
                skipped += 1
                continue
            bid, offer = _num(r["bid"]), _num(r["offer"])
            _put(surface, ccy, r["tenor"],
                 bid / 100.0 if bid is not None else None,
                 offer / 100.0 if offer is not None else None)
    if skipped:
        print(f"  (excluded {skipped} rows from untradeable segments)")
    return surface


# --- text / csv input ---------------------------------------------------------
# Accepts flexible lines, comments with '#':
#     USD, 1M, 3.80, 3.85
#     CNH  3M  1.30  1.55
#     currency=EUR tenor=6M bid=2.50 offer=2.75
_KV = re.compile(r"(\w+)\s*=\s*([^\s,]+)")


def surface_from_text(path: str, exclude_segments: Optional[set] = None) -> Dict:
    """Parse a .txt/.csv quote file into a surface.

    Columns (in order) are: currency, tenor, bid, offer[, segment].
    A header line naming those columns is honoured if present.
    """
    excl = UNTRADEABLE_SEGMENTS if exclude_segments is None else exclude_segments
    surface: Dict = {}
    text = Path(path).read_text(encoding="utf-8-sig")

    rows = []
    header = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if _KV.search(line) and "=" in line:
            d = {k.lower(): v for k, v in _KV.findall(line)}
            rows.append(d)
            continue
        parts = [p.strip() for p in (line.split(",") if "," in line else line.split())]
        if header is None and _looks_like_header(parts):
            header = [p.lower() for p in parts]
            continue
        if header:
            d = dict(zip(header, parts))
            # extra trailing column beyond the header is treated as the segment
            if len(parts) > len(header) and "segment" not in d:
                d["segment"] = parts[len(header)]
            rows.append(d)
        elif len(parts) >= 4:
            rows.append({"currency": parts[0], "tenor": parts[1],
                         "bid": parts[2], "offer": parts[3],
                         "segment": parts[4] if len(parts) > 4 else ""})

    skipped = 0
    for d in rows:
        if (d.get("segment", "") or "").strip().lower() in excl:
            skipped += 1
            continue
        ccy, tenor = d.get("currency"), d.get("tenor")
        if not ccy or not tenor:
            continue
        bid, offer = _num(d.get("bid")), _num(d.get("offer"))
        _put(surface, ccy, tenor.upper(),
             bid / 100.0 if bid is not None else None,
             offer / 100.0 if offer is not None else None)
    if skipped:
        print(f"  (excluded {skipped} rows from untradeable segments)")
    return surface


def _looks_like_header(parts: List[str]) -> bool:
    low = {p.lower() for p in parts}
    return "currency" in low or "ccy" in low


def merge_surfaces(*surfaces: Dict) -> Dict:
    """Combine channels: best borrow (lowest offer), best lend (highest bid)."""
    out: Dict = {}
    for s in surfaces:
        for ccy, sides in s.items():
            e = out.setdefault(ccy, {"bid": {}, "offer": {}})
            for t, v in sides.get("bid", {}).items():
                e["bid"][t] = max(e["bid"].get(t, float("-inf")), v)
            for t, v in sides.get("offer", {}).items():
                e["offer"][t] = min(e["offer"].get(t, float("inf")), v)
    return out
