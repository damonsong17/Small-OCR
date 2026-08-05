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

# Blocks we cannot transact -> never use for pricing or arbitrage.
UNTRADEABLE_SEGMENTS = {
    "korean", "korea",
    "taiwanese", "taiwan",
    "indian", "india",
    "islamic", "islamiic", "israel", "israeli",
}

# Segments we DO trade. Blank means "source has no segment concept" (a text
# channel), which is fine. This whitelist is the primary gate: an unrecognised
# or OCR-garbled segment is excluded by default rather than silently trusted.
TRADEABLE_SEGMENTS = {"", "chinese"}


def _norm(segment: Optional[str]) -> str:
    return "".join(ch for ch in (segment or "").lower() if ch.isalpha())


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def looks_untradeable(segment: Optional[str]) -> bool:
    """Fuzzy match against the untradeable names.

    OCR mangles these labels (ISLAMlC, lndian, Taiwanes, 'ISLAMIC BANK'), so an
    exact blacklist leaks. Substring, truncation and small typos all count.
    """
    n = _norm(segment)
    if not n:
        return False
    for bad in UNTRADEABLE_SEGMENTS:
        if bad in n or (len(n) >= 5 and n in bad):
            return True
        if _edit_distance(n, bad) <= 2:
            return True
    return False


def is_tradeable(segment: Optional[str], whitelist: bool = True) -> bool:
    """Whether a segment's quotes may be used.

    With ``whitelist`` (default) only known-good segments pass -- fail-safe
    against OCR noise. Otherwise fall back to fuzzy blacklisting.
    """
    if looks_untradeable(segment):
        return False
    if whitelist:
        n = _norm(segment)
        return n in {_norm(s) for s in TRADEABLE_SEGMENTS}
    return True


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
    whitelist: bool = True,
    verbose: bool = True,
) -> Dict:
    """Build a surface from the OCR store, using only tradeable segments.

    Every distinct segment seen is reported with its decision, so an untradeable
    block can never slip into pricing unnoticed.
    """
    surface: Dict = {}
    kept: Dict[str, int] = {}
    dropped: Dict[str, int] = {}
    for ccy in currencies:
        for r in store.by_currency(date, ccy):
            seg = (r["segment"] or "").strip()
            if exclude_segments is not None:
                use = _norm(seg) not in {_norm(s) for s in exclude_segments}
            else:
                use = is_tradeable(seg, whitelist=whitelist)
            label = seg or "(none)"
            if not use:
                dropped[label] = dropped.get(label, 0) + 1
                continue
            kept[label] = kept.get(label, 0) + 1
            bid, offer = _num(r["bid"]), _num(r["offer"])
            _put(surface, ccy, r["tenor"],
                 bid / 100.0 if bid is not None else None,
                 offer / 100.0 if offer is not None else None)
    if verbose:
        if kept:
            print("  segments USED:    "
                  + ", ".join(f"{k} ({n})" for k, n in sorted(kept.items())))
        if dropped:
            print("  segments EXCLUDED: "
                  + ", ".join(f"{k} ({n})" for k, n in sorted(dropped.items())))
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
        if not is_tradeable(d.get("segment", ""), whitelist=False):
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


def surface_from_lines(lines: Iterable[str], label: str = "manual") -> Dict:
    """Parse quote lines given directly (CLI, clipboard, a chat scrape).

    Same flexible grammar as the files: "USD,3M,4.00,4.10", "CNH 1M 1.30 1.55",
    or "currency=EUR tenor=6M bid=2.50 offer=2.75". Unparseable lines are
    reported, never dropped silently.
    """
    import tempfile
    text = "\n".join(lines)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(text)
        tmp = f.name
    try:
        surf = surface_from_text(tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)
    n = sum(len(v.get("bid", {})) + len(v.get("offer", {})) for v in surf.values())
    print(f"  {label}: parsed {n} quote value(s) for {', '.join(sorted(surf)) or '-'}")
    return surf


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
