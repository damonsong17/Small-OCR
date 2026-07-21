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

    def reference(self, securities: List[str], fields: List[str]) -> Dict[str, Dict[str, float]]:
        import blpapi

        svc = self._session.getService("//blp/refdata")
        req = svc.createRequest("ReferenceDataRequest")
        for s in securities:
            req.getElement("securities").appendValue(s)
        for f in fields:
            req.getElement("fields").appendValue(f)
        self._session.sendRequest(req)

        out: Dict[str, Dict[str, float]] = {}
        while True:
            ev = self._session.nextEvent(5000)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                arr = msg.getElement("securityData")
                for i in range(arr.numValues()):
                    sd = arr.getValueAsElement(i)
                    sec = sd.getElementAsString("security")
                    fd = sd.getElement("fieldData")
                    out[sec] = {
                        f: (fd.getElementAsFloat(f) if fd.hasElement(f) else None)
                        for f in fields
                    }
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


def _fwd_key(pair: str) -> str:
    """Bloomberg keys FX forward tickers off the non-USD leg (CNH, EUR, ...)."""
    base, quote = pair[:3], pair[3:]
    return quote if base == "USD" else base


def build_fx_market(client, pair: str, tenors: List[str], pip: float = 10000.0) -> Dict[str, FxPoint]:
    """Pull spot + forward outright (bid/ask) and derive points, per tenor."""
    fields = FX_TICKERS["fields"]
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

    out = {}
    for t, sec in per_tenor.items():
        fd = ref.get(sec) or {}
        f_bid, f_ask = fd.get("PX_BID"), fd.get("PX_ASK")
        fwd_mid = _mid(f_bid, f_ask)
        points = (fwd_mid - spot) * pip if (fwd_mid is not None and spot is not None) else None
        out[t] = FxPoint(tenor=t, spot=spot, points=points,
                         spot_bid=s_bid, spot_ask=s_ask, fwd_bid=f_bid, fwd_ask=f_ask)
    return out


def _mid(bid, ask):
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return bid if bid is not None else ask


def _quote(bid, ask):
    return {"PX_BID": bid, "PX_ASK": ask}


def demo_mock_fx() -> MockBloomberg:
    """Canned USDCNH + EURUSD spot + forward OUTRIGHTS (bid/ask) for offline arb.

    Uses the Help-Desk ticker convention: spot '<PAIR> Curncy', outrights
    '<non-USD leg>+<tenor> Curncy' (e.g. CNH+1M, EUR+3M), fields PX_BID/PX_ASK.
    """
    return MockBloomberg(data={
        # USDCNH: forward discount (USD rate > CNH rate) -> outright < spot
        "USDCNH Curncy": _quote(7.1840, 7.1860),
        "CNH+1M Curncy": _quote(7.1725, 7.1735),
        "CNH+3M Curncy": _quote(7.1495, 7.1505),
        "CNH+6M Curncy": _quote(7.1155, 7.1165),
        "CNH+12M Curncy": _quote(7.0520, 7.0540),
        # EURUSD: forward premium (USD rate > EUR rate) -> outright > spot
        "EURUSD Curncy": _quote(1.08495, 1.08505),
        "EUR+1M Curncy": _quote(1.08585, 1.08595),
        "EUR+3M Curncy": _quote(1.08755, 1.08765),
        "EUR+6M Curncy": _quote(1.08995, 1.09005),
        "EUR+12M Curncy": _quote(1.09445, 1.09455),
    })
