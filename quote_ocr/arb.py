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
    borrow_channel: str = ""   # which source we borrow from
    lend_channel: str = ""     # which source we lend to
    # True when a leg came from a one-sided reference rate widened to a
    # two-way price: the edge is indicative, not a firm executable trade.
    indicative: bool = False

    def legs(self) -> str:
        b = f"{self.borrow_ccy} {self.borrow_tenor}"
        l = f"{self.lend_ccy} {self.lend_tenor}"
        if self.borrow_channel:
            b += f" @{self.borrow_channel}"
        if self.lend_channel:
            l += f" @{self.lend_channel}"
        if not self.fx_tenor:
            return f"borrow {b} -> lend {l}"
        return f"borrow {b} -> FX swap {self.fx_tenor} -> lend {l}"

    def as_row(self) -> dict:
        return {
            "kind": self.kind, "pair": self.pair, "tenor": self.tenor,
            "pnl_bps": self.pnl_bps, "risk_type": self.risk_type,
            "channel": self.channel, "detail": self.detail,
            "borrow_ccy": self.borrow_ccy, "borrow_tenor": self.borrow_tenor,
            "fx_tenor": self.fx_tenor, "lend_ccy": self.lend_ccy,
            "lend_tenor": self.lend_tenor, "mismatch": self.mismatch,
            "borrow_channel": self.borrow_channel,
            "lend_channel": self.lend_channel,
            "indicative": self.indicative,
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
    indicative: Optional[set] = None,
) -> List[Opportunity]:
    """Find borrow-A / FX-swap / lend-B round trips that profit above threshold.

    With ``allow_mismatch`` the lending leg may mature on a different date from
    the FX swap. Those are real opportunities but carry gap/rollover risk, so
    they are labelled [TENOR MISMATCH] and every leg's tenor is reported.

    ``indicative`` is the set of ``(ccy, side, tenor)`` keys that were derived
    from a one-sided reference rate (see ``sources.apply_reference_sides``); any
    route touching one is labelled [INDICATIVE] instead of read as firm.
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
                        ind = (_is_ind(indicative, base, "offer", t_fx)
                               or _is_ind(indicative, quote, "bid", t_lend))
                        opps.append(Opportunity(
                            kind, pair, t_fx, round(edge, 2),
                            classify_risk(pair, t_fx, mism), channel,
                            f"borrow {base} {t_fx}@{b_off*100:.3f} -> FX swap {t_fx} "
                            f"-> lend {quote} {t_lend}@{q_bid*100:.3f} "
                            f"(synth {quote} borrow {synth_q*100:.3f}, act={act})"
                            + ("  [TENOR MISMATCH]" if mism else "")
                            + ("  [INDICATIVE: one-sided reference rate]" if ind else ""),
                            borrow_ccy=base, borrow_tenor=t_fx, fx_tenor=t_fx,
                            lend_ccy=quote, lend_tenor=t_lend, mismatch=mism,
                            indicative=ind))

                # borrow QUOTE @offer -> FX swap -> lend BASE @bid
                b_bid = _g(surface, base, "bid", t_lend)
                if synth_b is not None and b_bid is not None:
                    edge = (b_bid - synth_b) * 1e4
                    if edge > threshold_bps:
                        ind = (_is_ind(indicative, quote, "offer", t_fx)
                               or _is_ind(indicative, base, "bid", t_lend))
                        opps.append(Opportunity(
                            kind, pair, t_fx, round(edge, 2),
                            classify_risk(pair, t_fx, mism), channel,
                            f"borrow {quote} {t_fx}@{q_off*100:.3f} -> FX swap {t_fx} "
                            f"-> lend {base} {t_lend}@{b_bid*100:.3f} "
                            f"(synth {base} borrow {synth_b*100:.3f}, act={act})"
                            + ("  [TENOR MISMATCH]" if mism else "")
                            + ("  [INDICATIVE: one-sided reference rate]" if ind else ""),
                            borrow_ccy=quote, borrow_tenor=t_fx, fx_tenor=t_fx,
                            lend_ccy=base, lend_tenor=t_lend, mismatch=mism,
                            indicative=ind))
    return opps


def _g(surface, ccy, side, tenor):
    from .pricing import lookup_tenor
    return lookup_tenor(surface.get(ccy, {}).get(side, {}), tenor)


def _is_ind(indicative, ccy, side, tenor) -> bool:
    """Was this side invented from a one-sided reference rate?

    Checks every spelling of the tenor (1Y == 12M) so the flag cannot be lost
    to a naming difference -- an unflagged indicative edge would read as firm.
    """
    if not indicative:
        return False
    from .pricing import tenor_variants
    return any((ccy.upper(), side, t) in indicative for t in tenor_variants(tenor))


# --- cross-channel arbitrage --------------------------------------------------
def scan_across_channels(
    channels: Dict[str, Dict],      # {channel name: surface}
    fx: Dict,
    pairs: List[str],
    tenors: List[str],
    threshold_bps: float = 0.5,
    pip: float = 10000.0,
    allow_mismatch: bool = True,
    indicative: Optional[Dict[str, set]] = None,   # {channel: {(ccy,side,tenor)}}
) -> List[Opportunity]:
    """Arbitrage BETWEEN sources: borrow from one channel, lend to another.

    Merging channels into a best-of surface hides exactly this -- if AFS offers
    USD at 4.10 and the internal desk at 3.90, the merged surface keeps 3.90 and
    the "borrow internal, lend AFS" trade disappears. So every channel is kept
    separate and each ordered pair of channels is checked.

    Two shapes are found:
      * same currency, no FX at all: borrow ccy @A.offer, lend ccy @B.bid;
      * cross currency: borrow @A.offer, FX swap, lend the other ccy @B.bid.

    A one-sided reference rate lives under 'mid' and is never executable on its
    own; if the caller widened it into a two-way price it must pass the derived
    keys in ``indicative`` so those routes are labelled, not read as firm.
    """
    opps: List[Opportunity] = []
    names = list(channels)
    ind_of = indicative or {}

    # 1) same-currency funding arbitrage across channels (no FX leg)
    ccys = {c for s in channels.values() for c in s}
    for ccy in sorted(ccys):
        for a in names:
            for b in names:
                if a == b:
                    continue
                for t_b in tenors:
                    off = _g(channels[a], ccy, "offer", t_b)
                    if off is None:
                        continue
                    lend_tenors = tenors if allow_mismatch else [t_b]
                    for t_l in lend_tenors:
                        bid = _g(channels[b], ccy, "bid", t_l)
                        if bid is None:
                            continue
                        edge = (bid - off) * 1e4
                        if edge <= threshold_bps:
                            continue
                        mism = t_l != t_b
                        risk = ("gap/rollover (tenor mismatch) + " if mism else "") \
                            + "counterparty / funding line"
                        ind = (_is_ind(ind_of.get(a), ccy, "offer", t_b)
                               or _is_ind(ind_of.get(b), ccy, "bid", t_l))
                        opps.append(Opportunity(
                            "cross_channel_same_ccy", ccy, t_b, round(edge, 2),
                            risk, f"{a}->{b}",
                            f"borrow {ccy} {t_b}@{off*100:.3f} from {a} -> "
                            f"lend {ccy} {t_l}@{bid*100:.3f} to {b}"
                            + ("  [TENOR MISMATCH]" if mism else "")
                            + ("  [INDICATIVE: one-sided reference rate]" if ind else ""),
                            borrow_ccy=ccy, borrow_tenor=t_b, fx_tenor="",
                            lend_ccy=ccy, lend_tenor=t_l, mismatch=mism,
                            borrow_channel=a, lend_channel=b, indicative=ind))

    # 2) cross-currency across channels, via the FX swap
    for a in names:
        for b in names:
            if a == b:
                continue
            merged = {}
            for ccy, sides in channels[a].items():
                merged.setdefault(ccy, {"bid": {}, "offer": {}})["offer"] = \
                    dict(sides.get("offer", {}))
            for ccy, sides in channels[b].items():
                merged.setdefault(ccy, {"bid": {}, "offer": {}})["bid"] = \
                    dict(sides.get("bid", {}))
            # Only the offer side comes from a, only the bid side from b, so the
            # indicative keys follow the same split.
            ind_ab = {k for k in ind_of.get(a, ()) if k[1] == "offer"} \
                | {k for k in ind_of.get(b, ()) if k[1] == "bid"}
            for o in scan_surface_noarb(merged, fx, pairs, tenors,
                                        channel=f"{a}->{b}",
                                        kind="cross_channel_fx",
                                        threshold_bps=threshold_bps, pip=pip,
                                        allow_mismatch=allow_mismatch,
                                        indicative=ind_ab):
                o.borrow_channel, o.lend_channel = a, b
                o.detail = o.detail.replace("borrow ", f"borrow[{a}] ", 1) \
                                   .replace("-> lend ", f"-> lend[{b}] ", 1)
                opps.append(o)
    return opps


def surface_from_store(store, date: str, currencies: List[str]) -> Dict:
    """Build a bid/offer funding surface from the OCR quote store.

    Delegates to sources.surface_from_store so untradeable segments (Korean /
    Taiwanese / Indian / ISLAMIC) are excluded here too -- this used to read
    every segment, which let those quotes into scan.py's arbitrage search.
    """
    from .sources import surface_from_store as _load
    return _load(store, date, currencies)
