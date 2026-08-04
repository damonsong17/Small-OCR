"""Framework: combine OCR'd funding quotes with Bloomberg FX market data.

First implementation is covered-interest parity (CIP) for a pair like USDCNH:
given spot + market forward points (Bloomberg) and the two currencies' funding
rates (from the OCR quote store), compute the CIP-fair forward points and the
pickup vs the market. This is the skeleton the arb/quoting engine plugs into.

NOTE (pending confirmation): which side of the OCR bid/offer to use for
borrow vs lend, and whether the per-tenor Bloomberg ticker returns points or an
outright, are marked TODO -- they set the exact numbers, not the structure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# FALLBACK ONLY -- nominal day counts, used when Bloomberg SETTLE_DT is not
# available. The real `act` is the ACTUAL number of days between the spot and
# forward settlement dates; it changes with the trade date (weekend/holiday
# rolls) and is generally NOT 30/60/90. Example: trade 2026-08-04 -> spot
# 2026-08-06, 1M forward settles 2026-09-08 => act = 33, not 30 (a 10% error).
# Week tenors are the stable ones; month tenors always need SETTLE_DT.
TENOR_DAYS = {
    "O/N": 1, "T/N": 1, "1W": 7, "2W": 14, "3W": 21,
    "1M": 30, "2M": 60, "3M": 91, "4M": 121, "6M": 182, "9M": 273, "1Y": 365,
}


@dataclass
class PricingRow:
    tenor: str
    spot: Optional[float]
    r_base: Optional[float]      # base ccy funding (e.g. USD), decimal
    r_quote: Optional[float]     # quote ccy funding (e.g. CNH), decimal
    market_points: Optional[float]
    fair_points: Optional[float]
    pickup_points: Optional[float]
    note: str = ""

    def as_row(self) -> dict:
        return {
            "tenor": self.tenor,
            "spot": self.spot,
            "r_base": self.r_base,
            "r_quote": self.r_quote,
            "market_points": self.market_points,
            "fair_points": self.fair_points,
            "pickup_points": self.pickup_points,
            "note": self.note,
        }


def cip_forward_points(spot, r_base, r_quote, days, pip=10000.0, basis=360.0) -> float:
    """CIP-fair forward points for QUOTE-per-BASE (e.g. CNH per USD)."""
    t = days / basis
    fwd = spot * (1.0 + r_quote * t) / (1.0 + r_base * t)
    return (fwd - spot) * pip


def compare(
    fx_market: Dict[str, "object"],   # tenor -> FxPoint (spot, points)
    base_rates: Dict[str, float],     # tenor -> decimal rate (base ccy, e.g. USD)
    quote_rates: Dict[str, float],    # tenor -> decimal rate (quote ccy, e.g. CNH)
    pip: float = 10000.0,
    basis: float = 360.0,
) -> List[PricingRow]:
    rows: List[PricingRow] = []
    for tenor, fp in fx_market.items():
        days = TENOR_DAYS.get(tenor)
        rb, rq, spot = base_rates.get(tenor), quote_rates.get(tenor), fp.spot
        fair = pickup = None
        note = ""
        if None in (days, rb, rq, spot):
            note = "missing input"
        else:
            fair = cip_forward_points(spot, rb, rq, days, pip, basis)
            if fp.points is not None:
                pickup = fp.points - fair  # TODO: confirm ticker = points
                note = "market rich vs CIP" if pickup > 0 else "market cheap vs CIP"
        rows.append(PricingRow(tenor, spot, rb, rq, fp.points, fair, pickup, note))
    return rows


# --- pull funding rates out of the quote store --------------------------------
def rates_from_store(store, date: str, currency: str, side: str = "mid") -> Dict[str, float]:
    """tenor -> decimal funding rate for a currency on a date (percent/100)."""
    out: Dict[str, float] = {}
    for r in store.by_currency(date, currency):
        bid, offer = _f(r["bid"]), _f(r["offer"])
        val = {"bid": bid, "offer": offer}.get(side)
        if side == "mid":
            val = (bid + offer) / 2 if (bid is not None and offer is not None) else (bid or offer)
        if val is not None:
            out[r["tenor"]] = val / 100.0
    return out


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
