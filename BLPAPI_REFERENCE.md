# blpapi + FX/CIP — references and what is verified

This documents the evidence behind `quote_ocr/bloomberg.py`, `pricing.py`, and
`arb.py`, so the Bloomberg usage is grounded, not guessed. Bloomberg's API docs
are closed; where a claim can't be pinned to a public source it is marked
**VERIFY on terminal**.

## Sources

1. **Bloomberg Help Desk**, live chat 2026-07-21, ref# **H#1330770666**
   (specialist Luca DiRaimo) — the authoritative, desk-specific answer.
2. **Official blpapi Python SDK** — [github.com/msitt/blpapi-python](https://github.com/msitt/blpapi-python)
   (Bloomberg's maintained examples), e.g.
   [SimpleRefDataOverrideExample.py](https://github.com/msitt/blpapi-python/blob/master/examples/SimpleRefDataOverrideExample.py).
3. **BIS**, *Covered interest parity lost: understanding the cross-currency basis*
   ([bis.org/publ/qtrpdf/r_qt1609e](https://www.bis.org/publ/qtrpdf/r_qt1609e.htm)) and
   *CIP, FX swaps, cross-currency swaps and the factors that move the basis*.
4. Quant SE, on Bloomberg implied yield / basis (shared): Q
   [76962](https://quant.stackexchange.com/questions/76962),
   [82430](https://quant.stackexchange.com/questions/82430) (ICVS92 basis),
   [80090](https://quant.stackexchange.com/questions/80090) (fit BBG forward),
   [74011](https://quant.stackexchange.com/questions/74011) (xccy = 2 IRS).

## 1. The blpapi call pattern (verified vs official SDK)

Our `BloombergClient` (in `bloomberg.py`) uses exactly the documented pattern:

```python
session.start(); session.openService("//blp/refdata")
svc = session.getService("//blp/refdata")
req = svc.createRequest("ReferenceDataRequest")
req.getElement("securities").appendValue("USDCNH Curncy")   # == req.append("securities", ...)
req.getElement("fields").appendValue("PX_BID")
session.sendRequest(req)
# loop events; on each msg read securityData -> fieldData -> field values
```

This matches source (2). `ReferenceDataRequest` = snapshot; `MarketDataRequest`
(subscription) = streaming — Help Desk (1) confirmed either works for FX.

## 2. FX fields & tickers (from Help Desk, source 1)

- **Two-sided FX uses `PX_BID` / `PX_ASK`** (we switched off `PX_LAST`).
- **spot**: `USDCNH Curncy`.
- **forward outright**: non-USD leg + `+` + tenor → `CNH+1M Curncy` (USDCNH),
  `EUR+1M Curncy` (EURUSD).
- **forward points**: same, no `+` → `CNH1M Curncy`.
- Excel equivalents: `=BFXFORWARD("USDCNH","1M","BidOutright"|"AskOutright"|"BidPoints"|"AskPoints")`.
- **`BFXFORWARD` / `BFXIMPLIEDFORWARD` are NOT callable from blpapi** — you pull
  the tickers and compute CIP yourself. This is *why* our engine computes the
  implied yield rather than calling an analytic. (Help Desk, source 1.)

These roots are encoded in `FX_TICKERS`; **VERIFY the exact roots** (FWCV/FRD/FXFA)
on your terminal and adjust that one dict if they differ.

## 3. CIP / implied yield — the formula, reconciled

For a pair quoted QUOTE per BASE (USDCNH = CNH per USD; base=USD, quote=CNH),
no-arbitrage gives (BIS, source 3; standard CIP):

```
F/S = (1 + r_quote·t) / (1 + r_base·t)  =  DF_base / DF_term      (DF = 1/(1+r·t))
```

Derivation (invest 1 USD two ways and equate): `F = S·(1+r_quote·t)/(1+r_base·t)`.
Sanity: r_USD > r_CNH ⇒ F < S ⇒ USDCNH forward **discount** (negative points) —
matches the market and our mock. So the implied QUOTE rate from an FX swap is
`r_quote = (F/S·(1+r_base·t) − 1)/t` (`arb.implied_quote_rate`).

> Note on the Help-Desk one-liner "Forward = Spot × (term DF / base DF)": taken
> literally with DF = 1/(1+r·t) that inverts the sign. The correct, market-
> consistent orientation is **base DF / term DF** (above). We anchor to the
> derivation + the observed forward discount, not the phrasing.

## 4. Why naive CIP ≠ Bloomberg FXFA (the point of the SE posts)

Since 2008 **CIP does not hold**: there is a persistent **cross-currency basis**
(BIS, source 3). FXFA's "implied yield" is the rate that makes CIP hold *given
the market forward and the other leg's discount curve* — so it already **bakes
in the basis**. Consequently:

- A flat single-rate CIP (what `arb.py` computes) gives the **textbook** implied
  yield; the **gap vs the direct market OIS rate is the basis** — and that gap is
  exactly the inefficiency/arbitrage signal we want to flag.
- To reproduce FXFA's number to the decimal you must use the **same discount
  curves (OIS + basis), day count, and interpolation** Bloomberg uses (sources
  3, 4). That is a phase-2 refinement; for detection, the basis itself is the
  output, not an error.

## 5. Status

| item | status |
|---|---|
| blpapi ReferenceDataRequest pattern | ✅ matches official SDK |
| `PX_BID`/`PX_ASK` for FX | ✅ Help Desk |
| spot / outright(+) / points ticker forms | ✅ Help Desk; **roots VERIFY on terminal** |
| compute CIP in Python (no BFXFORWARD) | ✅ Help Desk |
| CIP formula & sign | ✅ BIS + derivation + market sign |
| matching FXFA implied yield exactly | ⏳ needs same OIS/basis curve (phase 2) |
