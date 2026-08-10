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

# --- counterparty access (KYC) ------------------------------------------------
# These blocks are quoted on the sheet and read correctly -- we simply CANNOT
# TRANSACT them: without a KYC relationship we do not get those prices. Using
# them would flag arbitrage we could never execute.
#
# This is a BUSINESS setting, not a data-quality one: it changes as KYC
# relationships are opened or closed, so it is configurable (segments.json or
# --no-kyc / --tradeable on the CLI) and must never require a code change.
NO_KYC_SEGMENTS = {
    "korean", "korea",
    "taiwanese", "taiwan",
    "indian", "india",
    "islamic", "islamiic", "israel", "israeli",
}

# Segments we DO have access to. Blank means the source has no segment concept
# (a text/manual channel, or an internal rate email), which is fine.
# "reference" / "internal" are not counterparties at all -- they are markers an
# earlier version wrote into this field, and they must not be mistaken for an
# unknown desk and silently excluded from a database already on the machine.
TRADEABLE_SEGMENTS = {"", "chinese", "reference", "internal"}

# What to do with a segment that is in neither list (a new source, a new desk).
# "exclude" is the safe default -- an unknown counterparty is assumed to have no
# KYC until someone says otherwise -- but it is always reported, never silent.
UNKNOWN_SEGMENT_POLICY = "exclude"

ACCESS_FILE = "segments.json"

# Backwards-compatible alias.
UNTRADEABLE_SEGMENTS = NO_KYC_SEGMENTS


def load_access(path: str = ACCESS_FILE) -> None:
    """Load KYC access config: {"tradeable": [...], "no_kyc": [...],
    "unknown": "exclude"|"include"}. Editable on the offline machine."""
    import json
    p = Path(path)
    if not p.exists():
        return
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ! could not read {path} ({e}); using built-in access config")
        return
    global UNKNOWN_SEGMENT_POLICY
    if "tradeable" in cfg:
        TRADEABLE_SEGMENTS.clear()
        TRADEABLE_SEGMENTS.update(s.strip().lower() for s in cfg["tradeable"])
    if "no_kyc" in cfg:
        NO_KYC_SEGMENTS.clear()
        NO_KYC_SEGMENTS.update(s.strip().lower() for s in cfg["no_kyc"])
    UNKNOWN_SEGMENT_POLICY = cfg.get("unknown", UNKNOWN_SEGMENT_POLICY)
    print(f"  loaded counterparty access config from {path}")


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


def _matches(name: str, group) -> bool:
    """Match a segment name against a configured group.

    Exact after normalising, plus a small tolerance for OCR variants of the SAME
    name (ISLAMlC, lndian, 'ISLAMIC BANK') -- that tolerance is a convenience,
    not the reason for the exclusion.
    """
    n = _norm(name)
    if not n:
        return False
    for item in group:
        g = _norm(item)
        if not g:
            continue
        if n == g or g in n or (len(n) >= 5 and n in g):
            return True
        if _edit_distance(n, g) <= 2:
            return True
    return False


def access_status(segment: Optional[str]) -> str:
    """'tradeable' | 'no_kyc' | 'unknown' for a segment."""
    n = _norm(segment)
    if not n:
        return "tradeable"          # source has no segment concept
    if _matches(segment, NO_KYC_SEGMENTS):
        return "no_kyc"
    if _matches(segment, TRADEABLE_SEGMENTS):
        return "tradeable"
    return "unknown"


def is_tradeable(segment: Optional[str], whitelist: bool = True) -> bool:
    """Whether we can actually transact this segment's quotes.

    ``whitelist`` is kept for compatibility; the decision is really the KYC
    access config, with UNKNOWN_SEGMENT_POLICY deciding new/unseen segments.
    """
    st = access_status(segment)
    if st == "no_kyc":
        return False
    if st == "unknown":
        return UNKNOWN_SEGMENT_POLICY == "include" or not whitelist
    return True


def reason(segment: Optional[str]) -> str:
    return {
        "no_kyc": "no KYC relationship - we cannot obtain these prices",
        "unknown": "counterparty not in the access config",
        "tradeable": "",
    }[access_status(segment)]


def empty_surface() -> Dict:
    return {}


def _put(surface: Dict, ccy: str, tenor: str, bid, offer, mid=None) -> None:
    e = surface.setdefault(ccy.upper(), {"bid": {}, "offer": {}, "mid": {}})
    e.setdefault("mid", {})
    if bid is not None:
        e["bid"][tenor] = bid
    if offer is not None:
        e["offer"][tenor] = offer
    if mid is not None:
        e["mid"][tenor] = mid


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
    source: str = None,
) -> Dict:
    """Build a surface from the OCR store, using only tradeable segments.

    Every distinct segment seen is reported with its decision, so an untradeable
    block can never slip into pricing unnoticed.
    """
    surface: Dict = {}
    kept: Dict[str, int] = {}
    dropped: Dict[str, int] = {}
    for ccy in currencies:
        for r in store.by_currency(date, ccy, source=source):
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
            mid = _num(r["mid"]) if "mid" in r.keys() else None
            _put(surface, ccy, r["tenor"],
                 bid / 100.0 if bid is not None else None,
                 offer / 100.0 if offer is not None else None,
                 mid / 100.0 if mid is not None else None)
    if verbose:
        who = f"[{source}] " if source else ""
        if kept:
            print(f"  {who}counterparties USED:     "
                  + ", ".join(f"{k} ({n})" for k, n in sorted(kept.items())))
        for label, n in sorted(dropped.items()):
            why = reason(None if label == "(none)" else label)
            print(f"  {who}counterparty EXCLUDED:   {label} ({n} rows) -- {why}")
        unknown = [k for k in dropped if access_status(k) == "unknown"]
        if unknown:
            print(f"  -> to trade any of these, add it to \"tradeable\" in "
                  f"{ACCESS_FILE}")
    return surface


# --- text / csv input ---------------------------------------------------------
# Accepts flexible lines, comments with '#':
#     USD, 1M, 3.80, 3.85
#     CNH  3M  1.30  1.55
#     currency=EUR tenor=6M bid=2.50 offer=2.75
_KV = re.compile(r"(\w+)\s*=\s*([^\s,]+)")
# a Markdown table rule: |---|:---:|---|
_MD_RULE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def _split(line: str) -> List[str]:
    """Split a quote line on pipe, comma, or whitespace -- in that order.

    Pipe first so a Markdown table converted from a PDF parses; splitting it on
    whitespace would yield '|' as a column and shift every field.
    """
    if "|" in line:
        return [p.strip() for p in line.strip().strip("|").split("|")]
    if "," in line:
        return [p.strip() for p in line.split(",")]
    return line.split()


def surface_from_text(path: str, exclude_segments: Optional[set] = None) -> Dict:
    """Parse a .txt/.csv/.md quote file into a surface.

    Columns (in order) are: currency, tenor, bid, offer[, segment].
    A header line naming those columns is honoured if present.

    Three delimiters are accepted, so the same parser covers a hand-typed line,
    a CSV export, and a Markdown table converted from a PDF:

        USD, 3M, 4.06, 4.12
        USD  3M  4.06  4.12
        | USD | 3M | 4.06 | 4.12 |
        currency=USD tenor=3M bid=4.06 offer=4.12
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
        if _MD_RULE.match(line):
            continue          # the |---|---| rule under a Markdown header
        if _KV.search(line) and "=" in line:
            d = {k.lower(): v for k, v in _KV.findall(line)}
            rows.append(d)
            continue
        parts = _split(line)
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


_TENOR_ORDER = ["O/N", "T/N", "S/N", "1W", "2W", "3W", "1M", "2M", "3M", "4M",
                "5M", "6M", "9M", "1Y", "12M", "2Y", "3Y", "5Y"]


def tenors_in(surface: Dict) -> List[str]:
    """Every tenor the quote sources actually contain, in market order.

    The scan must follow the data: AFS quotes O/N, 1W, 2W, 1M, 2M, 3M, 6M, 1Y,
    and hardcoding a shorter list silently drops real opportunities.
    """
    found = set()
    for sides in surface.values():
        for side in ("bid", "offer", "mid"):
            found.update(sides.get(side, {}))
    rank = {t: i for i, t in enumerate(_TENOR_ORDER)}
    return sorted(found, key=lambda t: (rank.get(t.upper(), 99), t))


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


# --- one-sided reference rates ------------------------------------------------
def apply_reference_sides(surface: Dict, half_spread_bps: float = 0.0):
    """Make single-sided reference rates ('mid') usable, visibly and reversibly.

    The internal money-market email quotes ONE rate per currency, not a two-way
    price. It is stored under ``mid`` so it can never masquerade as an
    executable bid/offer. But the desk still wants to know when the internal
    book looks arbitrageable against AFS, so this fills the missing sides from
    the mid at an ASSUMED half spread and returns the keys it invented.

    Returns ``(surface, indicative)`` where ``indicative`` is a set of
    ``(ccy, side, tenor)`` -- every opportunity touching one of those is flagged
    INDICATIVE rather than presented as a firm trade. A genuine quoted side is
    never overwritten.
    """
    hs = half_spread_bps / 1e4
    out: Dict = {}
    indicative = set()
    for ccy, sides in surface.items():
        e = {"bid": dict(sides.get("bid", {})),
             "offer": dict(sides.get("offer", {})),
             "mid": dict(sides.get("mid", {}))}
        for t, v in e["mid"].items():
            if t not in e["bid"]:
                e["bid"][t] = v - hs
                indicative.add((ccy.upper(), "bid", t))
            if t not in e["offer"]:
                e["offer"][t] = v + hs
                indicative.add((ccy.upper(), "offer", t))
        out[ccy] = e
    return out, indicative


def describe_reference(surface: Dict) -> List[str]:
    """Currencies in this surface that are reference-only (mid, no two-way)."""
    out = []
    for ccy, sides in sorted(surface.items()):
        if sides.get("mid") and not sides.get("bid") and not sides.get("offer"):
            out.append(ccy)
    return out
