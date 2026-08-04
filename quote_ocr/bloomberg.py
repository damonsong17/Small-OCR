"""Bloomberg Desktop API (blpapi) client for FX market data + an offline mock.

The real client talks to a Bloomberg **Professional terminal running on the same
machine** (Desktop API, host localhost:8194) -- no Bloomberg Anywhere needed.
The mock returns canned USDCNH data so the whole pricing framework can be
developed and tested with no terminal and no network.

IMPORTANT: the exact tickers/fields for FX forward points differ by desk
convention -- verify them against FRD / FXFA on your terminal and adjust
``FX_TICKERS`` below. This module is structured so that is the only change
needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Ticker/field conventions, confirmed with the Bloomberg Help Desk
# (2026-07-21, ref# H#1330770666) and the official blpapi examples
# (github.com/msitt/blpapi-python). See BLPAPI_REFERENCE.md.
#
#   - FX is two-sided: use PX_BID / PX_ASK (not PX_LAST).
#   - spot     : "USDCNH Curncy"
#   - outright : forward outright ticker uses the non-USD leg + '+' + tenor,
#                e.g. "CNH+1M Curncy" (USDCNH), "EUR+1M Curncy" (EURUSD).
#   - points   : forward points ticker, same but no '+', e.g. "CNH1M Curncy".
#   Excel equivalents: =BFXFORWARD("USDCNH","1M","BidOutright"/"AskOutright"
#   /"BidPoints"/"AskPoints"). Those toolkit analytics are NOT callable from
#   blpapi -- pull the tickers and compute CIP yourself.
#
# VERIFY the exact roots on your terminal (FWCV/FRD) and adjust here.
FX_TICKERS = {
    "spot": "{pair} Curncy",
    "outright": "{fwd}+{tenor} Curncy",
    "points": "{fwd}{tenor} Curncy",
    "fields": ["PX_BID", "PX_ASK"],
}

# Bloomberg tenor codes for the common buckets.
TENOR_CODE = {
    "O/N": "ON", "T/N": "TN", "1W": "1W", "2W": "2W",
    "1M": "1M", "2M": "2M", "3M": "3M", "6M": "6M", "9M": "9M", "1Y": "12M",
}


def _element_value(field_data, name):
    """Read a field without assuming its type (float / string / date)."""
    if field_data is None or not field_data.hasElement(name):
        return None
    el = field_data.getElement(name)
    try:
        return el.getValue()          # native Python type
    except Exception:
        try:
            return el.toString().strip()
        except Exception:
            return None


class BloombergClient:
    """Thin wrapper over blpapi ReferenceDataRequest (Desktop API)."""

    def __init__(self, host: str = "localhost", port: int = 8194):
        self.host, self.port = host, port
        self._session = None

    def connect(self):
        import blpapi  # imported lazily; only needed for the real client

        opts = blpapi.SessionOptions()
        opts.setServerHost(self.host)
        opts.setServerPort(self.port)
        self._session = blpapi.Session(opts)
        if not self._session.start():
            raise ConnectionError(
                "Could not start blpapi session -- is the Bloomberg terminal "
                "running and logged in on this machine?"
            )
        if not self._session.openService("//blp/refdata"):
            raise ConnectionError("Could not open //blp/refdata service.")
        return self

    def reference(self, securities: List[str], fields: List[str]) -> Dict[str, Dict]:
        """Snapshot reference data. Values keep their native type (float/str/date).

        Any per-security problem is reported back under the '__error__' key and
        unavailable fields under '__fieldErrors__', so a None is explainable
        (bad ticker vs no permission vs field not applicable) instead of silent.
        """
        import blpapi

        svc = self._session.getService("//blp/refdata")
        req = svc.createRequest("ReferenceDataRequest")
        for s in securities:
            req.getElement("securities").appendValue(s)
        for f in fields:
            req.getElement("fields").appendValue(f)
        self._session.sendRequest(req)

        out: Dict[str, Dict] = {}
        while True:
            ev = self._session.nextEvent(5000)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                arr = msg.getElement("securityData")
                for i in range(arr.numValues()):
                    sd = arr.getValueAsElement(i)
                    sec = sd.getElementAsString("security")
                    rec: Dict = {}
                    if sd.hasElement("securityError"):
                        rec["__error__"] = sd.getElement("securityError").toString().strip()
                    if sd.hasElement("fieldExceptions"):
                        fe = sd.getElement("fieldExceptions")
                        errs = [fe.getValueAsElement(k).toString().strip()
                                for k in range(fe.numValues())]
                        if errs:
                            rec["__fieldErrors__"] = errs
                    fd = sd.getElement("fieldData") if sd.hasElement("fieldData") else None
                    for f in fields:
                        rec[f] = _element_value(fd, f)
                    out[sec] = rec
            if ev.eventType() == blpapi.Event.RESPONSE:
                break
        return out

    def close(self):
        if self._session is not None:
            self._session.stop()
            self._session = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()


@dataclass
class MockBloomberg:
    """Offline stand-in returning canned reference data."""

    data: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def connect(self):
        return self

    def reference(self, securities, fields):
        return {s: {f: self.data.get(s, {}).get(f) for f in fields} for s in securities}

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


@dataclass
class FxPoint:
    tenor: str
    spot: Optional[float]        # spot mid
    points: Optional[float]      # forward points mid (outright - spot) * pip
    spot_bid: Optional[float] = None
    spot_ask: Optional[float] = None
    fwd_bid: Optional[float] = None   # forward outright bid
    fwd_ask: Optional[float] = None   # forward outright ask
    # Settlement dates and the ACTUAL day count between them. `act` varies by
    # tenor AND by trade date (holiday/weekend rolls), so it must come from
    # Bloomberg's SETTLE_DT -- never a nominal 30/90/180.
    spot_settle: Optional[object] = None
    fwd_settle: Optional[object] = None
    act: Optional[int] = None


# Market quoting convention: the currency earlier in this list is the BASE.
# (EUR/USD, USD/CHF, USD/CNH, ... ) Extend as new currencies are added.
QUOTE_ORDER = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "CNH", "CNY",
               "HKD", "SGD", "JPY"]


def order_pair(a: str, b: str) -> str:
    """Return the market-convention pair string for two currencies."""
    def rank(c):
        return QUOTE_ORDER.index(c) if c in QUOTE_ORDER else len(QUOTE_ORDER)
    return (a + b) if rank(a) <= rank(b) else (b + a)


def all_pairs(currencies: List[str]) -> List[str]:
    """Every unique pair from a currency list, in market-convention order."""
    cs = list(dict.fromkeys(currencies))
    out = []
    for i, a in enumerate(cs):
        for b in cs[i + 1:]:
            out.append(order_pair(a, b))
    return out


def _fwd_key(pair: str) -> str:
    """Root used for FX forward tickers.

    For USD pairs Bloomberg keys forwards off the non-USD leg (CNH+1M, EUR+1M).
    For crosses (no USD leg) the full pair is used (EURCHF+1M) -- VERIFY on the
    terminal; if a cross returns no data, that is the ticker root to adjust.
    """
    base, quote = pair[:3], pair[3:]
    if base == "USD":
        return quote
    if quote == "USD":
        return base
    return pair


def build_fx_market(client, pair: str, tenors: List[str], pip: float = 10000.0,
                    with_settle: bool = True) -> Dict[str, FxPoint]:
    """Pull spot + forward outright (bid/ask), derive points, and (by default)
    the SETTLE_DT of each leg so the ACTUAL day count `act` is available."""
    fields = list(FX_TICKERS["fields"])
    if with_settle:
        fields.append("SETTLE_DT")
    spot_sec = FX_TICKERS["spot"].format(pair=pair)
    fwd = _fwd_key(pair)
    secs = [spot_sec]
    per_tenor = {}
    for t in tenors:
        code = TENOR_CODE.get(t, t)
        sec = FX_TICKERS["outright"].format(fwd=fwd, tenor=code)
        per_tenor[t] = sec
        secs.append(sec)

    ref = client.reference(secs, fields)
    sd = ref.get(spot_sec) or {}
    s_bid, s_ask = sd.get("PX_BID"), sd.get("PX_ASK")
    spot = _mid(s_bid, s_ask)
    spot_settle = sd.get("SETTLE_DT") if with_settle else None

    out = {}
    for t, sec in per_tenor.items():
        fd = ref.get(sec) or {}
        f_bid, f_ask = fd.get("PX_BID"), fd.get("PX_ASK")
        fwd_mid = _mid(f_bid, f_ask)
        points = (fwd_mid - spot) * pip if (fwd_mid is not None and spot is not None) else None
        fwd_settle = fd.get("SETTLE_DT") if with_settle else None
        out[t] = FxPoint(tenor=t, spot=spot, points=points,
                         spot_bid=s_bid, spot_ask=s_ask, fwd_bid=f_bid, fwd_ask=f_ask,
                         spot_settle=spot_settle, fwd_settle=fwd_settle,
                         act=act_days(spot_settle, fwd_settle))
    return out


def _mid(bid, ask):
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return bid if bid is not None else ask


def settle_dates(client, pair: str, tenors: List[str]) -> Dict[str, object]:
    """SETTLE_DT for spot and each forward tenor (the blpapi equivalent of
    Excel's BDP("CNH1M BGN Curncy","SETTLE_DT") -- same field mnemonic).

    Returns {'spot': date, '<tenor>': date, ...}. Note the *points* ticker
    (no '+') is used here, matching the Help Desk's BDP example.
    """
    fwd = _fwd_key(pair)
    spot_sec = FX_TICKERS["spot"].format(pair=pair)
    secs = {"spot": spot_sec}
    for t in tenors:
        secs[t] = FX_TICKERS["points"].format(fwd=fwd, tenor=TENOR_CODE.get(t, t))
    ref = client.reference(list(secs.values()), ["SETTLE_DT"])
    return {k: (ref.get(v) or {}).get("SETTLE_DT") for k, v in secs.items()}


def act_days(spot_settle, fwd_settle) -> Optional[int]:
    """Actual days between the spot and forward settlement dates."""
    if spot_settle is None or fwd_settle is None:
        return None
    return (fwd_settle - spot_settle).days


def _quote(bid, ask, settle=None):
    d = {"PX_BID": bid, "PX_ASK": ask}
    if settle is not None:
        d["SETTLE_DT"] = settle
    return d


def demo_mock_fx() -> MockBloomberg:
    """Canned USDCNH + EURUSD spot + forward OUTRIGHTS (bid/ask) for offline arb.

    Uses the Help-Desk ticker convention: spot '<PAIR> Curncy', outrights
    '<non-USD leg>+<tenor> Curncy' (e.g. CNH+1M, EUR+3M), fields PX_BID/PX_ASK.
    """
    from datetime import date
    # Trade date 2026-08-04 -> spot 2026-08-06; forward settles roll with the
    # calendar, so act is 33/95/186/368 -- not 30/90/180/360.
    sp = date(2026, 8, 6)
    s1, s3, s6, s12 = (date(2026, 9, 8), date(2026, 11, 9),
                       date(2027, 2, 8), date(2027, 8, 9))
    return MockBloomberg(data={
        # USDCNH: forward discount (USD rate > CNH rate) -> outright < spot
        "USDCNH Curncy": _quote(7.1840, 7.1860, sp),
        "CNH+1M Curncy": _quote(7.1725, 7.1735, s1),
        "CNH+3M Curncy": _quote(7.1495, 7.1505, s3),
        "CNH+6M Curncy": _quote(7.1155, 7.1165, s6),
        "CNH+12M Curncy": _quote(7.0520, 7.0540, s12),
        # EURUSD: forward premium (USD rate > EUR rate) -> outright > spot
        "EURUSD Curncy": _quote(1.08495, 1.08505, sp),
        "EUR+1M Curncy": _quote(1.08585, 1.08595, s1),
        "EUR+3M Curncy": _quote(1.08755, 1.08765, s3),
        "EUR+6M Curncy": _quote(1.08995, 1.09005, s6),
        "EUR+12M Curncy": _quote(1.09445, 1.09455, s12),
    })
