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
    tenor: str
    pnl_bps: float
    risk_type: str
    channel: str
    detail: str

    def as_row(self) -> dict:
        return {
            "kind": self.kind, "pair": self.pair, "tenor": self.tenor,
            "pnl_bps": self.pnl_bps, "risk_type": self.risk_type,
            "channel": self.channel, "detail": self.detail,
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


def classify_risk(pair: str, tenor: str) -> str:
    base, quote = pair[:3], pair[3:]
    days = TENOR_DAYS.get(tenor, 30)
    if base in OFFSHORE or quote in OFFSHORE:
        return "liquidity (offshore ccy funding)"
    if days >= 90:
        return "liquidity (term funding) + basis"
    return "execution / rollover"


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
) -> List[Opportunity]:
    """Find borrow-A / swap / lend-B round trips that profit above threshold."""
    opps: List[Opportunity] = []
    for pair in pairs:
        base, quote = pair[:3], pair[3:]
        bb, bq = basis_of(base), basis_of(quote)
        for tenor in tenors:
            fp = fx.get(pair, {}).get(tenor)
            if fp is None or fp.spot is None or fp.points is None:
                continue
            # ACTUAL settle-to-settle days; fall back to the nominal table only
            # if SETTLE_DT was unavailable.
            act = getattr(fp, "act", None) or TENOR_DAYS.get(tenor)
            if not act:
                continue
            fos = fwd_over_spot(fp.spot, fp.points, pip)

            b_off = _g(surface, base, "offer", tenor)
            b_bid = _g(surface, base, "bid", tenor)
            q_off = _g(surface, quote, "offer", tenor)
            q_bid = _g(surface, quote, "bid", tenor)

            # borrow BASE @offer -> synth QUOTE borrow; lend QUOTE @bid
            if b_off is not None and q_bid is not None:
                synth = implied_quote_rate(fos, b_off, act, bb, bq)
                edge = (q_bid - synth) * 1e4
                if edge > threshold_bps:
                    opps.append(Opportunity(
                        kind, pair, tenor, round(edge, 2), classify_risk(pair, tenor), channel,
                        f"borrow {base}@{b_off*100:.3f} -> FX swap -> lend {quote}@{q_bid*100:.3f} "
                        f"(synth {quote} borrow {synth*100:.3f}, act={act})"))

            # borrow QUOTE @offer -> synth BASE borrow; lend BASE @bid
            if q_off is not None and b_bid is not None:
                synth = implied_base_rate(fos, q_off, act, bb, bq)
                edge = (b_bid - synth) * 1e4
                if edge > threshold_bps:
                    opps.append(Opportunity(
                        kind, pair, tenor, round(edge, 2), classify_risk(pair, tenor), channel,
                        f"borrow {quote}@{q_off*100:.3f} -> FX swap -> lend {base}@{b_bid*100:.3f} "
                        f"(synth {base} borrow {synth*100:.3f}, act={act})"))
    return opps


def _g(surface, ccy, side, tenor):
    return surface.get(ccy, {}).get(side, {}).get(tenor)


def surface_from_store(store, date: str, currencies: List[str]) -> Dict:
    """Build a bid/offer funding surface from the OCR quote store."""
    from .pricing import rates_from_store
    surf = {}
    for c in currencies:
        surf[c] = {
            "bid": rates_from_store(store, date, c, "bid"),
            "offer": rates_from_store(store, date, c, "offer"),
        }
    return surf
