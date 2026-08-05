"""FX ticker resolution built for an air-gapped machine.

Design constraints (moving code between an online and an offline box is slow):

  * NEVER hard-fail on an unknown ticker. Try many candidate formats, then fall
    back to triangulation from the USD legs.
  * NEVER silently swallow a missing quote -- every pair/tenor reports whether
    it resolved, which ticker won, and what was tried.
  * Everything is fixable ON THE OFFLINE BOX without a code change or a new
    bundle: edit ``fx_tickers.json`` (plain JSON, any text editor) and rerun.

OBSERVED on the terminal (hover), and nothing beyond it is assumed:

  * cross pairs use their PLAIN pair name for spot and for the ordinary tenors
    (EURCNH stays EURCNH);
  * ONLY the 12M point is special, using a 2-letter-code form:
        EURCNH -> CGEU12M   EURHKD -> HDEU12M   EURCHF -> SFEU12M
        HKDCNH -> CGHD12M   CHFHKD -> HDSF1Y    (1Y, not 12M)

Those five are stored verbatim. The 2-letter-code form is NOT extrapolated to
other tenors -- for 1M/3M/6M the plain pair name is tried first, and the code
form is kept only as a last-resort fallback that costs nothing if wrong.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

OVERRIDES_FILE = "fx_tickers.json"

# Bloomberg 2-letter FX codes. EU/SF/HD/CG are confirmed from observed tickers;
# the rest are the usual codes and are only ever used as CANDIDATES, so a wrong
# guess costs nothing (it simply does not resolve and we triangulate instead).
BBG_CCY_CODE = {
    "EUR": "EU", "CHF": "SF", "HKD": "HD", "CNH": "CG",   # confirmed
    "GBP": "BP", "JPY": "JY", "AUD": "AD", "CAD": "CD",   # conventional
    "NZD": "ND", "SGD": "SD", "CNY": "CC", "SEK": "SK",
    "NOK": "NK", "DKK": "DK", "TWD": "NT", "KRW": "KW",
}

# Alternative spellings of the same tenor, tried in order.
TENOR_ALIASES = {
    "1Y": ["12M", "1Y"],
    "12M": ["12M", "1Y"],
    "6M": ["6M"], "3M": ["3M"], "2M": ["2M"], "1M": ["1M"],
    "2W": ["2W"], "1W": ["1W"], "3W": ["3W"],
    "O/N": ["ON"], "T/N": ["TN"],
}

# CONFIRMED cross SPOT tickers (plain pair name).
CONFIRMED_CROSS_SPOT = {
    "EURCHF": "EURCHF Curncy",
    "EURHKD": "EURHKD Curncy",
    "EURCNH": "EURCNH Curncy",
    "CHFHKD": "CHFHKD Curncy",
    "HKDCNH": "HKDCNH Curncy",
    # CHFCNH has no hover ticker; candidates below (incl. the slash form the
    # Help Desk suggested) will settle it.
}

# CONFIRMED cross FORWARD tickers, observed on the terminal.
# NOTE these 12M forms return forward POINTS, not outrights -- bloomberg.py
# classifies and converts them. The slash form (EUR/CNH 12M Curncy) returns an
# outright and is tried first; these stay as reliable alternates.
CONFIRMED_CROSS_FWD = {
    ("EURCNH", "1Y"): "CGEU12M Curncy",
    ("EURHKD", "1Y"): "HDEU12M Curncy",
    ("EURCHF", "1Y"): "SFEU12M Curncy",
    ("HKDCNH", "1Y"): "CGHD12M Curncy",
    ("CHFHKD", "1Y"): "HDSF1Y Curncy",
}

# VERIFIED live (2026-08), kept so the resolver needs no probing for these:
VERIFIED_CROSS = {
    "EURCNH|3M": "EUR/CNH 3M Curncy",   # outright (EURCNH3M/CGEU3M give points)
    "CHFCNH|3M": "CHF/CNH 3M Curncy",   # outright; only form that worked
    "CHFCNH|SPOT": "CHFCNH Curncy",
}


def code_of(ccy: str) -> Optional[str]:
    return BBG_CCY_CODE.get(ccy.upper())


def load_overrides(path: str = OVERRIDES_FILE) -> Dict[str, str]:
    """Confirmed defaults, overlaid with whatever is in the JSON file.

    The file is hand-editable on the offline machine -- fixing a ticker never
    requires touching code or rebuilding the bundle.
    """
    out = defaults()
    p = Path(path)
    if p.exists():
        try:
            out.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"  ! could not read {path} ({e}); using built-in defaults")
    return out


def save_overrides(mapping: Dict[str, str], path: str = OVERRIDES_FILE) -> None:
    Path(path).write_text(json.dumps(mapping, indent=2, sort_keys=True),
                          encoding="utf-8")


def key(pair: str, tenor: Optional[str] = None) -> str:
    return f"{pair}|{tenor}" if tenor else f"{pair}|SPOT"


def defaults() -> Dict[str, str]:
    out = {key(p): sec for p, sec in CONFIRMED_CROSS_SPOT.items()}
    out.update({key(p, t): sec for (p, t), sec in CONFIRMED_CROSS_FWD.items()})
    out.update(VERIFIED_CROSS)
    return out


def candidates(pair: str, tenor: Optional[str] = None) -> List[str]:
    """Candidate tickers, most likely first. Never raises; always returns some."""
    base, quote = pair[:3], pair[3:]
    out: List[str] = []

    def add(s):
        if s not in out:
            out.append(s)

    if tenor is None:
        if pair in CONFIRMED_CROSS_SPOT:
            add(CONFIRMED_CROSS_SPOT[pair])
        add(f"{pair} Curncy")
        add(f"{base}/{quote} Curncy")
        return out

    if (pair, tenor) in CONFIRMED_CROSS_FWD:
        add(CONFIRMED_CROSS_FWD[(pair, tenor)])   # observed verbatim

    cq, cb = code_of(quote), code_of(base)

    for tv in TENOR_ALIASES.get(tenor, [tenor]):
        # VERIFIED: the slash form is the only one that worked for BOTH EURCNH
        # and CHFCNH, and it returns an OUTRIGHT, so it leads.
        add(f"{base}/{quote} {tv} Curncy")    # EUR/CNH 3M, CHF/CNH 3M
        # These return forward POINTS (handled by bloomberg.quote_kind).
        add(f"{pair}{tv} Curncy")             # EURCNH3M  -> points
        if cq and cb:
            add(f"{cq}{cb}{tv} Curncy")       # CGEU3M    -> points
        add(f"{pair}+{tv} Curncy")            # (errored for crosses; harmless)
        add(f"{pair} {tv} Curncy")
    return out


def resolve(client, pairs: List[str], tenors: List[str],
            overrides: Optional[Dict[str, str]] = None,
            fields=("PX_BID", "PX_ASK"), verbose: bool = True) -> Dict[str, str]:
    """Probe candidates and keep whatever returns a price.

    Everything is requested in ONE batch. Unresolved entries are reported with
    the candidates that were tried, so nothing disappears quietly.
    """
    found: Dict[str, str] = dict(overrides or {})
    probes: Dict[str, List[str]] = {}
    for pair in pairs:
        if key(pair) not in found:
            probes[key(pair)] = candidates(pair)
        for t in tenors:
            if key(pair, t) not in found:
                probes[key(pair, t)] = candidates(pair, t)
    if not probes:
        if verbose:
            print("  all tickers already known (defaults/overrides)")
        return found

    all_secs = sorted({s for lst in probes.values() for s in lst})
    if verbose:
        print(f"  probing {len(all_secs)} candidate ticker(s) "
              f"for {len(probes)} pair-tenor(s) ...")
    try:
        data = client.reference(all_secs, list(fields))
    except Exception as e:                      # never let a probe break the run
        print(f"  ! probe request failed ({e}); everything will triangulate")
        return found

    unresolved = []
    for k, cands in probes.items():
        for sec in cands:
            rec = data.get(sec) or {}
            if rec.get("__error__"):
                continue
            if rec.get("PX_BID") is not None or rec.get("PX_ASK") is not None:
                found[k] = sec
                if verbose:
                    print(f"    OK  {k:16} -> {sec}")
                break
        else:
            unresolved.append((k, cands))

    if unresolved and verbose:
        print(f"\n  {len(unresolved)} unresolved (will triangulate from USD legs):")
        for k, cands in unresolved:
            print(f"    --  {k:16} tried: {', '.join(c.replace(' Curncy','') for c in cands)}")
    return found
