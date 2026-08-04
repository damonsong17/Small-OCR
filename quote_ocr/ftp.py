"""Generate an FTP quote surface that is arbitrage-free across currencies.

Starting from our funding cost (the merged channel surface) plus a margin, the
published FTP surface must satisfy: nobody can borrow currency A from us, FX
swap it, and lend currency B back to us at a profit.

For each pair (A base, B quote) and tenor, with F/S from the FX market:

    synthetic B borrow  = implied_quote_rate(F/S, ftp_offer[A])   # borrow A, swap
    require:  ftp_bid[B]  <=  synthetic B borrow
    synthetic A borrow  = implied_base_rate (F/S, ftp_offer[B])   # borrow B, swap
    require:  ftp_bid[A]  <=  synthetic A borrow

Violations are removed by TIGHTENING (lowering) the offending bid -- we pay less
on deposits rather than making our loans uncompetitive. Set mode='offer' to
raise the borrow side instead. Bids only ever decrease, so the loop converges.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .arb import fwd_over_spot, implied_base_rate, implied_quote_rate
from .implied import basis_of
from .pricing import TENOR_DAYS


@dataclass
class FtpAdjustment:
    ccy: str
    tenor: str
    side: str
    before: float
    after: float
    reason: str

    def as_row(self) -> dict:
        return {"ccy": self.ccy, "tenor": self.tenor, "side": self.side,
                "before_pct": round(self.before * 100, 6),
                "after_pct": round(self.after * 100, 6),
                "moved_bps": round((self.after - self.before) * 1e4, 2),
                "reason": self.reason}


def build_ftp(
    surface: Dict,
    fx: Dict,
    pairs: List[str],
    tenors: List[str],
    margin_bps: float = 0.0,
    bid_margin_bps: Optional[float] = None,
    offer_margin_bps: Optional[float] = None,
    mode: str = "bid",
    pip: float = 10000.0,
    max_iter: int = 25,
):
    """Return (ftp_surface, adjustments).

    ftp_offer = our borrow cost + offer margin   (what we charge to lend out)
    ftp_bid   = our lend rate   - bid margin     (what we pay on deposits)
    then iteratively enforced to be cross-currency arbitrage free.
    """
    bm = (bid_margin_bps if bid_margin_bps is not None else margin_bps) / 1e4
    om = (offer_margin_bps if offer_margin_bps is not None else margin_bps) / 1e4

    ftp: Dict = {}
    for ccy, sides in surface.items():
        e = ftp.setdefault(ccy, {"bid": {}, "offer": {}})
        for t, v in sides.get("bid", {}).items():
            e["bid"][t] = v - bm
        for t, v in sides.get("offer", {}).items():
            e["offer"][t] = v + om

    adjustments: List[FtpAdjustment] = []
    for _ in range(max_iter):
        changed = False
        for pair in pairs:
            base, quote = pair[:3], pair[3:]
            bb, bq = basis_of(base), basis_of(quote)
            for tenor in tenors:
                fp = fx.get(pair, {}).get(tenor)
                if fp is None or fp.spot is None or fp.points is None:
                    continue
                act = getattr(fp, "act", None) or TENOR_DAYS.get(tenor)
                if not act:
                    continue
                fos = fwd_over_spot(fp.spot, fp.points, pip)

                # borrow BASE from us -> swap -> lend QUOTE back to us
                a_off = _get(ftp, base, "offer", tenor)
                b_bid = _get(ftp, quote, "bid", tenor)
                if a_off is not None and b_bid is not None:
                    cap = implied_quote_rate(fos, a_off, act, bb, bq)
                    if b_bid > cap:
                        changed |= _apply(ftp, adjustments, quote, base, tenor,
                                          b_bid, cap, a_off, mode, act)

                # borrow QUOTE from us -> swap -> lend BASE back to us
                b_off = _get(ftp, quote, "offer", tenor)
                a_bid = _get(ftp, base, "bid", tenor)
                if b_off is not None and a_bid is not None:
                    cap = implied_base_rate(fos, b_off, act, bb, bq)
                    if a_bid > cap:
                        changed |= _apply(ftp, adjustments, base, quote, tenor,
                                          a_bid, cap, b_off, mode, act)
        if not changed:
            break
    return ftp, adjustments


def _apply(ftp, adjustments, victim_ccy, via_ccy, tenor, before, cap,
           other_offer, mode, act) -> bool:
    """Tighten the surface so the round trip no longer profits."""
    reason = (f"borrow {via_ccy}@{other_offer*100:.4f} -> FX swap -> "
              f"lend {victim_ccy} (act={act})")
    if mode == "offer":
        # raise the borrow side of the OTHER currency instead
        cur = ftp[via_ccy]["offer"].get(tenor)
        need = cur + (before - cap)
        ftp[via_ccy]["offer"][tenor] = need
        adjustments.append(FtpAdjustment(via_ccy, tenor, "offer", cur, need, reason))
        return True
    ftp[victim_ccy]["bid"][tenor] = cap
    adjustments.append(FtpAdjustment(victim_ccy, tenor, "bid", before, cap, reason))
    return True


def _get(surface, ccy, side, tenor):
    return surface.get(ccy, {}).get(side, {}).get(tenor)
