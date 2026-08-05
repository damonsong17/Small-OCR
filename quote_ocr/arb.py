"""Cross-currency arbitrage detection + FTP no-arb checks.

Given a funding surface (per currency: bid/offer rates by tenor) and FX market
data (spot + forward points per pair), detect where the two are inconsistent
via covered interest parity (CIP), i.e. where a currency's funding synthesised
through an FX swap beats a directly quoted rate. Each opportunity carries a
P&L in **bps** and a **risk type**.

The same engine, run on our own published FTP surface, enforces the
cross-currency no-arb constraint the desk requires: nobody may borrow one
currency from us, FX-swap it, and lend another back to us at a profit.

Rate convention (configurable, pending final confirmation of the AFS columns):
  offer = the rate to BORROW that currency,  bid = the rate to LEND/place it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .implied import basis_of
from .pricing import TENOR_DAYS

# Currencies whose term funding carries offshore-liquidity risk.
OFFSHORE = {"CNH", "HKD", "TWD", "INR", "KRW", "IDR", "PHP"}


@dataclass
class Opportunity:
    kind: str          # 'cross_ccy_arb' (channel) | 'ftp_self_arb' | 'cip_basis'
    pair: str
    tenor: str         # the FX swap tenor (the trade's horizon)
    pnl_bps: float
    risk_type: str
    channel: str
    detail: str
    # Each leg's tenor, stated explicitly so a maturity mismatch is visible.
    borrow_ccy: str = ""
    borrow_tenor: str = ""
    fx_tenor: str = ""
    lend_ccy: str = ""
    lend_tenor: str = ""
    mismatch: bool = False

    def legs(self) -> str:
        return (f"borrow {self.borrow_ccy} {self.borrow_tenor} -> "
                f"FX swap {self.fx_tenor} -> lend {self.lend_ccy} {self.lend_tenor}")

    def as_row(self) -> dict:
        return {
            "kind": self.kind, "pair": self.pair, "tenor": self.tenor,
            "pnl_bps": self.pnl_bps, "risk_type": self.risk_type,
            "channel": self.channel, "detail": self.detail,
            "borrow_ccy": self.borrow_ccy, "borrow_tenor": self.borrow_tenor,
            "fx_tenor": self.fx_tenor, "lend_ccy": self.lend_ccy,
            "lend_tenor": self.lend_tenor, "mismatch": self.mismatch,
            "legs": self.legs(),
        }


# --- CIP helpers (QUOTE per BASE, e.g. USDCNH = CNH per USD) -------------------
# Each leg accrues on its OWN day-count basis, and `act` is the ACTUAL number of
# days between the spot and forward settlement dates (from Bloomberg SETTLE_DT).
def fwd_over_spot(spot: float, points: float, pip: float = 10000.0) -> float:
    return (spot + points / pip) / spot


def implied_quote_rate(fos: float, r_base: float, act: int,
                       basis_base: int = 360, basis_quote: int = 360) -> float:
    """Synthetic QUOTE-ccy rate from borrowing BASE and FX-swapping it."""
    return (fos * (1.0 + r_base * act / basis_base) - 1.0) * basis_quote / act


def implied_base_rate(fos: float, r_quote: float, act: int,
                      basis_base: int = 360, basis_quote: int = 360) -> float:
    """Synthetic BASE-ccy rate from borrowing QUOTE and FX-swapping it."""
    return ((1.0 + r_quote * act / basis_quote) / fos - 1.0) * basis_base / act


def classify_risk(pair: str, tenor: str, mismatch: bool = False) -> str:
    base, quote = pair[:3], pair[3:]
    days = TENOR_DAYS.get(tenor, 30)
    if base in OFFSHORE or quote in OFFSHORE:
        risk = "liquidity (offshore ccy funding)"
    elif days >= 90:
        risk = "liquidity (term funding) + basis"
    else:
        risk = "execution / rollover"
    if mismatch:
        # A maturity mismatch is NOT a closed arbitrage: the gap must be rolled
        # or reinvested at an unknown future rate.
        risk = "gap/rollover (tenor mismatch) + " + risk
    return risk


# --- surface: ccy -> {'bid': {tenor: rate}, 'offer': {tenor: rate}} -----------
def scan_surface_noarb(
    surface: Dict[str, Dict[str, Dict[str, float]]],
    fx: Dict[str, Dict[str, "object"]],
    pairs: List[str],
    tenors: List[str],
    channel: str = "",
    kind: str = "cross_ccy_arb",
    threshold_bps: float = 0.5,
    pip: float = 10000.0,
    basis: float = 360.0,
    allow_mismatch: bool = True,
) -> List[Opportunity]:
    """Find borrow-A / FX-swap / lend-B round trips that profit above threshold.

    With ``allow_mismatch`` the lending leg may mature on a different date from
    the FX swap. Those are real opportunities but carry gap/rollover risk, so
    they are labelled [TENOR MISMATCH] and every leg's tenor is reported.
    """
    opps: List[Opportunity] = []
    for pair in pairs:
        base, quote = pair[:3], pair[3:]
        bb, bq = basis_of(base), basis_of(quote)
        # The FX swap tenor sets the horizon: you borrow a currency for that
        # period and swap it. The LENDING leg may run to a different maturity --
        # that is allowed, but it is a gap position, not a closed arbitrage, so
        # every leg's tenor is reported and the mismatch is flagged.
        for t_fx in tenors:
            fp = fx.get(pair, {}).get(t_fx)
            if fp is None or fp.spot is None or fp.points is None:
                continue
            # ACTUAL settle-to-settle days; nominal table only as fallback.
            act = getattr(fp, "act", None) or TENOR_DAYS.get(t_fx)
            if not act:
                continue
            fos = fwd_over_spot(fp.spot, fp.points, pip)

            b_off = _g(surface, base, "offer", t_fx)
            q_off = _g(surface, quote, "offer", t_fx)
            synth_q = (implied_quote_rate(fos, b_off, act, bb, bq)
                       if b_off is not None else None)
            synth_b = (implied_base_rate(fos, q_off, act, bb, bq)
                       if q_off is not None else None)

            lend_tenors = tenors if allow_mismatch else [t_fx]
            for t_lend in lend_tenors:
                mism = t_lend != t_fx

                # borrow BASE @offer -> FX swap -> lend QUOTE @bid
                q_bid = _g(surface, quote, "bid", t_lend)
                if synth_q is not None and q_bid is not None:
                    edge = (q_bid - synth_q) * 1e4
                    if edge > threshold_bps:
                        opps.append(Opportunity(
                            kind, pair, t_fx, round(edge, 2),
                            classify_risk(pair, t_fx, mism), channel,
                            f"borrow {base} {t_fx}@{b_off*100:.3f} -> FX swap {t_fx} "
                            f"-> lend {quote} {t_lend}@{q_bid*100:.3f} "
                            f"(synth {quote} borrow {synth_q*100:.3f}, act={act})"
                            + ("  [TENOR MISMATCH]" if mism else ""),
                            borrow_ccy=base, borrow_tenor=t_fx, fx_tenor=t_fx,
                            lend_ccy=quote, lend_tenor=t_lend, mismatch=mism))

                # borrow QUOTE @offer -> FX swap -> lend BASE @bid
                b_bid = _g(surface, base, "bid", t_lend)
                if synth_b is not None and b_bid is not None:
                    edge = (b_bid - synth_b) * 1e4
                    if edge > threshold_bps:
                        opps.append(Opportunity(
                            kind, pair, t_fx, round(edge, 2),
                            classify_risk(pair, t_fx, mism), channel,
                            f"borrow {quote} {t_fx}@{q_off*100:.3f} -> FX swap {t_fx} "
                            f"-> lend {base} {t_lend}@{b_bid*100:.3f} "
                            f"(synth {base} borrow {synth_b*100:.3f}, act={act})"
                            + ("  [TENOR MISMATCH]" if mism else ""),
                            borrow_ccy=quote, borrow_tenor=t_fx, fx_tenor=t_fx,
                            lend_ccy=base, lend_tenor=t_lend, mismatch=mism))
    return opps


def _g(surface, ccy, side, tenor):
    from .pricing import lookup_tenor
    return lookup_tenor(surface.get(ccy, {}).get(side, {}), tenor)


def surface_from_store(store, date: str, currencies: List[str]) -> Dict:
    """Build a bid/offer funding surface from the OCR quote store.

    Delegates to sources.surface_from_store so untradeable segments (Korean /
    Taiwanese / Indian / ISLAMIC) are excluded here too -- this used to read
    every segment, which let those quotes into scan.py's arbitrage search.
    """
    from .sources import surface_from_store as _load
    return _load(store, date, currencies)
