# FXFA implied yield, CIP, and what "arbitrage" really means

Answers to the core questions, with a worked USDCNH example. This is the
conceptual basis for `quote_ocr/arb.py`.

## 1. Is the Help-Desk formula flipped?

Yes. The literal Help-Desk line `Forward = Spot × (term DF / base DF)` gives the
wrong sign. The correct, no-arbitrage relation (derive by investing 1 USD two
ways and equating) is:

```
F = S × DF_base / DF_term          (base=USD, term=CNH for USDCNH)
  = S × (1 + r_term·t) / (1 + r_base·t)          [DF = 1/(1+r·t)]
```

**Worked check** (USD 4.30%, CNH 1.50%, S = 7.1850, t = 1):
- correct: F = 7.1850 × (1/1.043)/(1/1.015) = 7.1850 × 0.9732 = **6.992** → forward **discount** ✅ (matches the market: USDCNH forwards trade below spot because USD rates > CNH rates)
- flipped: 7.1850 × 1.0276 = 7.383 → forward *premium* ❌

So `arb.py` uses the correct orientation, anchored to the observable forward
discount, not the phrasing. (The help desk almost certainly just mislabeled
base/term verbally.)

## 2 & 3. How FXFA computes an "implied yield"

FXFA for USDCNH takes **three** things:
1. **spot** (traded market price),
2. **forward** outright/points for the tenor (traded market price),
3. **one** currency's yield curve that *you select* on the page (say the CNH curve).

It then **solves for the OTHER currency's yield** (USD) that makes CIP hold
*exactly* given (1)+(2)+(3):

```
DF_USD = (F / S) × DF_CNH   →   r_USD_implied backed out
```

So the **USD implied yield is a derived number, not an independently observed
USD deposit rate.** It is "the USD yield consistent with the traded FX swap and
the CNH curve you chose." Which leg is *implied* vs *given* is your choice — fix
CNH, imply USD, or vice-versa.

**Therefore FXFA's own numbers always satisfy CIP** — the implied yield is
*defined* to make them satisfy it. There is no CIP violation *inside* FXFA.

## 4. If "the quotes never satisfy CIP", what does it imply? Arb or not?

The violation is **not** among FXFA's internal numbers. It is between:

- **A = FX-swap-implied** USD yield (from FXFA, using spot+forward+CNH curve), and
- **B = the actual USD cash-market yield** you can independently transact (SOFR/OIS, or a real USD deposit).

```
cross-currency basis  =  A − B
```

- basis = 0 → CIP holds → no discrepancy.
- basis ≠ 0 → the FX swap implies a *different* USD yield than the USD cash market.

**Worked example**: if the *traded* 1Y forward is 6.95 (deeper discount than the
4.30%/1.50% CIP forward of 6.992), back out A:
`DF_USD = (6.95/7.185)×(1/1.015) = 0.95300 → A = 4.93%`. Against B = 4.30% USD
OIS, **basis = +63 bps** — i.e. synthesising USD via "borrow CNH + FX swap"
costs 4.93% vs 4.30% direct.

**Is that basis a riskless arbitrage? No.** In the frictionless textbook it is
(borrow cheap leg, lend rich leg, hedge FX, lock the 63 bps). In reality it is a
**risk/cost premium**, because harvesting it consumes:
- **balance sheet** (leverage ratio, capital/RWA),
- **funding & liquidity** (you must fund and roll the position; term funding can
  vanish — a big deal for offshore CNH),
- **counterparty/credit, collateral/XVA, regulatory (LCR/NSFR)** capacity.

The basis is the market *price of those frictions*. It is an opportunity **only
for whoever's cost of bearing them is below the basis** — different institutions,
different answer. That is why every flagged opportunity carries a **risk_type**:
you net the gross bps against *your* cost of that risk.

## 5. Post-2008 "CIP no longer holds" — does arbitrage always exist?

No — not *riskless* arbitrage. A persistent non-zero basis means the *textbook*
CIP equality is violated, but the trade isn't free (frictions above). Better
read as a **funding risk premium** than a free lunch. So a non-zero basis ≠
guaranteed money; it is *your* opportunity iff your marginal cost of balance
sheet/funding for that risk type is below it.

## What this means for the engine — three distinct things

| # | what | is it "real" arb? | engine |
|---|---|---|---|
| **A** | market cross-currency basis (FX-implied vs market OIS) | a market-wide **risk premium** — monitor as context; yours only if your cost < basis | feed OIS as a "channel" (phase 2) |
| **B** | a **specific channel** (AFS, internal funding) priced off the FX-swap market or off another channel | **yes, potentially real** net of costs — a counterparty is off-market and you transact both legs | `scan_surface_noarb` on the channel |
| **C** | **your own FTP quotes** let a client round-trip you | a hard no-arb you fully control — must be **eliminated** | `scan_surface_noarb` on your FTP surface |

`arb.py` computes the FX-swap-implied cross rate and compares it to a quoted
rate; the reported **bps is the gross edge**. To decide if an opportunity is
actionable, subtract your cost for its **risk_type**. The pilot targets **B**
(AFS mispricing) and **C** (FTP consistency); **A** (market basis vs OIS) is the
phase-2 add once OIS tickers are wired.
