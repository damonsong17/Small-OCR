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

# How to build securities for a pair. {pair} -> e.g. USDCNH.
# Verify on the terminal: spot ticker, and the per-tenor forward-points ticker.
FX_TICKERS = {
    "spot": "{pair} Curncy",              # PX_LAST = spot
    "points": "{pair}{tenor} Curncy",     # PX_LAST = outright or points (VERIFY)
    "field": "PX_LAST",
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
    spot: Optional[float]
    points: Optional[float]  # raw value of the per-tenor ticker (verify meaning)


def build_fx_market(client, pair: str, tenors: List[str]) -> Dict[str, FxPoint]:
    """Pull spot + per-tenor forward values for a pair into a tenor->FxPoint map."""
    field_name = FX_TICKERS["field"]
    spot_sec = FX_TICKERS["spot"].format(pair=pair)
    secs = [spot_sec]
    per_tenor = {}
    for t in tenors:
        code = TENOR_CODE.get(t, t)
        sec = FX_TICKERS["points"].format(pair=pair, tenor=code)
        per_tenor[t] = sec
        secs.append(sec)

    ref = client.reference(secs, [field_name])
    spot = (ref.get(spot_sec) or {}).get(field_name)
    out = {}
    for t, sec in per_tenor.items():
        out[t] = FxPoint(tenor=t, spot=spot, points=(ref.get(sec) or {}).get(field_name))
    return out


def demo_mock_usdcnh() -> MockBloomberg:
    """Canned USDCNH spot + 1M/3M/6M/1Y forward points for offline testing."""
    return MockBloomberg(data={
        "USDCNH Curncy": {"PX_LAST": 7.1850},
        "USDCNH1M Curncy": {"PX_LAST": -120.0},
        "USDCNH3M Curncy": {"PX_LAST": -350.0},
        "USDCNH6M Curncy": {"PX_LAST": -690.0},
        "USDCNH12M Curncy": {"PX_LAST": -1320.0},
    })


def demo_mock_fx() -> MockBloomberg:
    """Canned multi-pair FX (USDCNH + EURUSD) for offline arb testing."""
    return MockBloomberg(data={
        "USDCNH Curncy": {"PX_LAST": 7.1850},
        "USDCNH1M Curncy": {"PX_LAST": -120.0},
        "USDCNH3M Curncy": {"PX_LAST": -350.0},
        "USDCNH6M Curncy": {"PX_LAST": -690.0},
        "USDCNH12M Curncy": {"PX_LAST": -1320.0},
        "EURUSD Curncy": {"PX_LAST": 1.0850},
        "EURUSD1M Curncy": {"PX_LAST": 9.0},
        "EURUSD3M Curncy": {"PX_LAST": 26.0},
        "EURUSD6M Curncy": {"PX_LAST": 50.0},
        "EURUSD12M Curncy": {"PX_LAST": 95.0},
    })
