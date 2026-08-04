"""FX-swap implied yields, matching Bloomberg FXFA conventions.

Formula (verified against FXFA for USDCNH = ACT/360 and USDHKD = ACT/365).
For a pair BASE/QUOTE (e.g. USDCNH: base USD, quote CNH), implying the BASE
currency's yield from the QUOTE currency's known rate:

    r_base_bid = [ (S_ask / F_ask) * (1 + r_quote * act/basis_quote) - 1 ] * basis_base/act
    r_base_ask = [ (S_bid / F_bid) * (1 + r_quote * act/basis_quote) - 1 ] * basis_base/act

Two details that make it match Bloomberg:
  * the KNOWN leg accrues on ITS OWN basis (CNH ACT/360, HKD ACT/365),
    while the IMPLIED leg is expressed on ITS OWN basis (USD ACT/360);
  * ``act`` is the ACTUAL number of days between the spot settlement date and
    the forward settlement date -- take both from Bloomberg's SETTLE_DT, never
    from a nominal 30/90/180 day count.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Money-market day-count basis per currency.
# CNH=360 and HKD=365 are confirmed against FXFA; the rest follow the standard
# market convention -- VERIFY any new currency before relying on it.
DAY_BASIS = {
    "USD": 360, "EUR": 360, "CHF": 360, "JPY": 360, "CNH": 360, "CNY": 360,
    "SEK": 360, "NOK": 360, "DKK": 360,
    "GBP": 365, "HKD": 365, "SGD": 365, "TWD": 365, "AUD": 365, "NZD": 365,
    "CAD": 365, "ZAR": 365, "THB": 365, "MYR": 365,
}

DEFAULT_BASIS = 360


def basis_of(ccy: str) -> int:
    return DAY_BASIS.get(ccy.upper(), DEFAULT_BASIS)


@dataclass
class ImpliedYield:
    pair: str
    tenor: str
    implied_ccy: str
    known_ccy: str
    act: int
    bid: Optional[float]   # decimal (0.0432 = 4.32%)
    ask: Optional[float]

    def as_row(self) -> dict:
        return {
            "pair": self.pair, "tenor": self.tenor,
            "implied_ccy": self.implied_ccy, "known_ccy": self.known_ccy,
            "act": self.act,
            "implied_bid": self.bid, "implied_ask": self.ask,
        }


def implied_base_yield(
    pair: str, tenor: str, act: int,
    spot_bid: float, spot_ask: float, fwd_bid: float, fwd_ask: float,
    r_quote: float,
) -> ImpliedYield:
    """Imply the BASE currency's yield (e.g. USD in USDCNH) from the QUOTE rate."""
    base, quote = pair[:3], pair[3:]
    bq, bb = basis_of(quote), basis_of(base)
    grow = 1.0 + r_quote * act / bq
    bid = ((spot_ask / fwd_ask) * grow - 1.0) * bb / act
    ask = ((spot_bid / fwd_bid) * grow - 1.0) * bb / act
    return ImpliedYield(pair, tenor, base, quote, act, bid, ask)


def implied_quote_yield(
    pair: str, tenor: str, act: int,
    spot_bid: float, spot_ask: float, fwd_bid: float, fwd_ask: float,
    r_base: float,
) -> ImpliedYield:
    """Imply the QUOTE currency's yield (e.g. CNH in USDCNH) from the BASE rate."""
    base, quote = pair[:3], pair[3:]
    bb, bq = basis_of(base), basis_of(quote)
    grow = 1.0 + r_base * act / bb
    bid = ((fwd_bid / spot_bid) * grow - 1.0) * bq / act
    ask = ((fwd_ask / spot_ask) * grow - 1.0) * bq / act
    return ImpliedYield(pair, tenor, quote, base, act, bid, ask)
