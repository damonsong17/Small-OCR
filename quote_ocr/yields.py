"""Where the KNOWN-leg currency yield comes from -- and why it matters.

An FX-swap implied yield is only meaningful relative to the rate you feed in as
the known leg. The same spot/forward produces a different implied yield, with a
different MEANING, depending on that source:

  * ``channel``   -- the tradeable bid/offer from a quote source (AFS, internal
                     funding). Implied yield = "the USD funding I actually
                     synthesise by borrowing CNH from this counterparty and FX
                     swapping". This is the number that decides whether a
                     CHANNEL arbitrage is real for us.
  * ``benchmark`` -- the fixing printed on the same sheet (SOFR, EURIBOR,
                     CNH HIBOR, HKD HIBOR). Already captured by the OCR into
                     ``benchmark_rate``. Closest to what FXFA shows when you
                     select that curve.
  * ``market``    -- a live Bloomberg curve (OIS / IBOR). Implied yield minus
                     the same currency's market rate = the CROSS-CURRENCY BASIS,
                     i.e. the market inefficiency, not a tradeable edge for us.

Mixing them silently is the classic way to produce a confident, wrong number, so
the source is always explicit and recorded.

The market tickers below are NOT verified -- they are candidates, probed the
same way as the FX tickers, and anything confirmed can be pinned in
``rate_tickers.json`` on the offline machine without a code change.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

RATE_OVERRIDES_FILE = "rate_tickers.json"

SOURCES = ("channel", "benchmark", "market")

# UNVERIFIED candidate tickers per currency+tenor. Probe with rate_check.py and
# pin whatever works; nothing here is assumed to be correct.
MARKET_RATE_CANDIDATES = {
    "USD": ["USOSFR{T} Curncy", "SOFRRATE Index", "US0003M Index"],
    "EUR": ["EUSWE{T} Curncy", "ESTRON Index", "EUR003M Index"],
    "CHF": ["SFSNT{T} Curncy", "SSARON Index", "SF0003M Index"],
    "CNH": ["CNHHIBOR{T} Index", "HIHD{T} Index", "CNH{T} Index"],
    "HKD": ["HIHD{T} Index", "HKDHIBOR{T} Index", "HD0003M Index"],
}

# Tenor token used inside the ticker templates.
RATE_TENOR = {"1M": "1M", "2M": "2M", "3M": "3M", "6M": "6M",
              "1Y": "1Y", "12M": "1Y", "1W": "1W", "2W": "2W"}


def key(ccy: str, tenor: str) -> str:
    return f"{ccy.upper()}|{tenor}"


def load_overrides(path: str = RATE_OVERRIDES_FILE) -> Dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ! could not read {path} ({e})")
        return {}


def save_overrides(mapping: Dict[str, str], path: str = RATE_OVERRIDES_FILE) -> None:
    Path(path).write_text(json.dumps(mapping, indent=2, sort_keys=True),
                          encoding="utf-8")


def candidates(ccy: str, tenor: str) -> List[str]:
    t = RATE_TENOR.get(tenor, tenor)
    return [c.format(T=t) for c in MARKET_RATE_CANDIDATES.get(ccy.upper(), [])]


def from_channel(surface: Dict, ccy: str, side: str = "mid") -> Dict[str, float]:
    """Tradeable funding rates from a quote surface (decimals)."""
    e = surface.get(ccy.upper(), {})
    bid, offer = e.get("bid", {}), e.get("offer", {})
    out: Dict[str, float] = {}
    for t in set(bid) | set(offer):
        b, o = bid.get(t), offer.get(t)
        if side == "bid":
            v = b
        elif side == "offer":
            v = o
        else:
            v = (b + o) / 2 if (b is not None and o is not None) else (b if b is not None else o)
        if v is not None:
            out[t] = v
    return out


def from_benchmark(store, date: str, ccy: str,
                   exclude_segments: Optional[set] = None) -> Dict[str, float]:
    """Benchmark fixings printed on the sheet (SOFR / EURIBOR / HIBOR)."""
    from .sources import UNTRADEABLE_SEGMENTS
    excl = UNTRADEABLE_SEGMENTS if exclude_segments is None else exclude_segments
    out: Dict[str, float] = {}
    for r in store.by_currency(date, ccy):
        if (r["segment"] or "").strip().lower() in excl:
            continue
        raw = r["benchmark_rate"]
        try:
            v = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        out[r["tenor"]] = v / 100.0
    return out


def from_market(client, ccy: str, tenors: List[str],
                overrides: Optional[Dict[str, str]] = None,
                verbose: bool = True) -> Dict[str, float]:
    """Live Bloomberg curve, trying candidates and reporting what was used."""
    ov = overrides or {}
    per_tenor: Dict[str, List[str]] = {}
    for t in tenors:
        k = key(ccy, t)
        per_tenor[t] = [ov[k]] if k in ov else candidates(ccy, t)
    secs = sorted({s for lst in per_tenor.values() for s in lst})
    if not secs:
        if verbose:
            print(f"  ! no market rate ticker candidates for {ccy}")
        return {}
    try:
        ref = client.reference(secs, ["PX_LAST", "PX_BID", "PX_ASK"])
    except Exception as e:
        print(f"  ! market rate request failed for {ccy}: {e}")
        return {}

    out: Dict[str, float] = {}
    for t, cands in per_tenor.items():
        for s in cands:
            rec = ref.get(s) or {}
            if rec.get("__error__"):
                continue
            v = rec.get("PX_LAST")
            if v is None:
                b, a = rec.get("PX_BID"), rec.get("PX_ASK")
                v = (b + a) / 2 if (b is not None and a is not None) else (b or a)
            if v is not None:
                out[t] = v / 100.0
                if verbose:
                    print(f"    rate {ccy} {t:4} <- {s} = {v}")
                break
        else:
            if verbose:
                print(f"    rate {ccy} {t:4} UNRESOLVED, tried: "
                      f"{', '.join(cands) or '(none)'}")
    return out


def describe(source: str) -> str:
    return {
        "channel": "tradeable channel quotes (decides real channel arbitrage)",
        "benchmark": "sheet benchmark fixings (closest to FXFA's selected curve)",
        "market": "live Bloomberg curve (difference vs implied = xccy basis)",
    }.get(source, source)
