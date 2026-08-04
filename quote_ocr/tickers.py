"""FX ticker resolution: confirmed conventions + empirical discovery.

Bloomberg's cross-currency forward naming is not publicly documented and is not
uniform (e.g. CHFHKD works for most tenors but 1Y shows as HDSF1Y; CHFCNH has no
hover ticker at all yet BDP("CHF/CNH 3M Curncy",...) reportedly works). Rather
than hardcode guesses, we:

  1. use the CONFIRMED convention for USD pairs,
  2. try a list of CANDIDATE formats for crosses and record which one actually
     returns data (``resolve``), persisting the result to a JSON overrides file,
  3. fall back to triangulation from the USD legs when nothing resolves.

Anything in the overrides file wins, so a ticker you verify by hand (such as
HDSF1Y) can simply be pinned there.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

OVERRIDES_FILE = "fx_tickers.json"

# Candidate formats for a CROSS forward outright, tried in order.
# {pair}=EURCHF, {base}=EUR, {quote}=CHF, {tenor}=3M
CROSS_FORWARD_CANDIDATES = [
    "{pair}{tenor} Curncy",        # EURCHF3M Curncy
    "{pair}+{tenor} Curncy",       # EURCHF+3M Curncy
    "{base}/{quote} {tenor} Curncy",   # CHF/CNH 3M Curncy  (per Bloomberg Help Desk)
    "{pair} {tenor} Curncy",       # EURCHF 3M Curncy
]

CROSS_SPOT_CANDIDATES = [
    "{pair} Curncy",               # EURCHF Curncy
    "{base}/{quote} Curncy",       # CHF/CNH Curncy
]


def load_overrides(path: str = OVERRIDES_FILE) -> Dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_overrides(mapping: Dict[str, str], path: str = OVERRIDES_FILE) -> None:
    Path(path).write_text(json.dumps(mapping, indent=2, sort_keys=True),
                          encoding="utf-8")


def key(pair: str, tenor: Optional[str] = None) -> str:
    return f"{pair}|{tenor}" if tenor else f"{pair}|SPOT"


def candidates(pair: str, tenor: Optional[str] = None) -> List[str]:
    base, quote = pair[:3], pair[3:]
    fmts = CROSS_FORWARD_CANDIDATES if tenor else CROSS_SPOT_CANDIDATES
    return [f.format(pair=pair, base=base, quote=quote, tenor=tenor) for f in fmts]


def resolve(client, pairs: List[str], tenors: List[str],
            overrides: Optional[Dict[str, str]] = None,
            fields=("PX_BID", "PX_ASK"), verbose: bool = True) -> Dict[str, str]:
    """Probe candidate tickers for each cross pair/tenor; return what works.

    Only securities that come back with an actual price are accepted. The
    returned mapping is keyed 'PAIR|TENOR' (and 'PAIR|SPOT').
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
        return found

    all_secs = sorted({s for lst in probes.values() for s in lst})
    data = client.reference(all_secs, list(fields))

    for k, cands in probes.items():
        for sec in cands:
            rec = data.get(sec) or {}
            if rec.get("__error__"):
                continue
            if rec.get("PX_BID") is not None or rec.get("PX_ASK") is not None:
                found[k] = sec
                if verbose:
                    print(f"  resolved {k:16} -> {sec}")
                break
        else:
            if verbose:
                print(f"  unresolved {k:16} (will triangulate)")
    return found
