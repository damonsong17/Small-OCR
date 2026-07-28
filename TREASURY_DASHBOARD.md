# Treasury Dashboard — offline monitoring from your own spreadsheets

A local web dashboard for a small bank's treasury desk. Point it at a folder of
Excel files and it computes and monitors:

| Panel | What it answers |
|---|---|
| **Cash projection** | Will we be short, in which currency, on which day? |
| **Client balances** | Who funds us, how concentrated is it, who is over their limit? |
| **Client map** | Where does the book sit — with regulation, quota and live bid/offer per jurisdiction |
| **FX risk** | Net open position, sensitivity, VaR, limit headroom |
| **LCR** | HQLA against 30-day stressed net outflows |
| **NSFR** | Available against required stable funding |
| **P&L** | Net interest accrual, spread, FX revaluation |
| **Data sources** | What was loaded, how it was mapped, what was ignored |

It **computes the metrics from source data** — deals, securities, FX trades,
client static — rather than re-reading a finished LCR spreadsheet. If you also
drop in the report workbook you submitted, the panels reconcile computed against
reported and show the variance.

**It runs entirely offline.** Python standard library plus `openpyxl` (already a
dependency of the OCR pipeline). No web framework, no CDN, no map tile server,
no telemetry. The world map is a public-domain outline bundled as a file.

```powershell
python scripts\make_demo_data.py --out data\treasury    # a demo bank to look at
python dashboard.py --data data\treasury --open
```

---

## 1. What to feed it

Drop workbooks into one folder. Sheet and column names do not have to match
anything — the loader recognises the common spellings, expands abbreviations
(`Mat. Dt`, `Notional Amt`, `Cpty Type`), skips title rows and footnotes, and
tells you in the **Data sources** panel exactly what it made of each sheet.

| Dataset | What it is | Columns it looks for |
|---|---|---|
| `positions` | the deal blotter — one row per contract | counterparty, type, product, asset/liability, currency, amount, rate, value date, maturity, operational/insured/secured flags, collateral level, country |
| `securities` | investment and liquidity portfolio | ISIN, issuer, issuer type, currency, market value, HQLA level, encumbered, maturity, coupon, risk weight |
| `fx_trades` | spot, forwards, swaps | buy/sell currency and amount, value date, counterparty |
| `commitments` | committed but undrawn facilities | client, facility type, currency, limit, drawn, expiry |
| `clients` | counterparty static | name, type, sector, country, city (or lat/lon), rating, credit limit, RM |
| `cashflows` | anything contracts don't know | date, currency, amount, category, description |
| `fx_rates` | spot rates and volatilities | pair or currency + rate, date, annualised vol |
| `market_rates` | the funding curve | currency, tenor, bid, offer |
| `report_lines` | figures you already reported | report, as-of, line item, amount, factor, weighted |

Only `positions` is needed to get started; each panel says what it is missing.

**Conventions worth knowing**

- **Asset or liability.** Give an `A/L` column if you have one. Otherwise it is
  inferred from the product (a deposit funds you, a loan doesn't). A negative
  amount with no product also reads as a liability.
- **Rates.** A plain number in a rate column is read as *percent* — `4.35` means
  4.35%. If the cell is percent-formatted in Excel, that formatting is honoured.
  If your export already writes decimals, set `"rate_unit": "fraction"` in
  `settings.json`.
- **No maturity date** means callable on demand. Those balances stay out of the
  contractual cash ladder and appear as the "callable on demand" figure instead.
- **Currency conversion** uses the `fx_rates` sheet. A pair `EURUSD` with rate
  1.085 means *1 EUR = 1.085 USD*. Anything with no rate is excluded from base
  currency totals and reported as a warning, never silently zeroed.

### When a sheet is misread

Pin it. Put `mapping.json` in the data folder — it always wins over automatic
classification, and it is data, so nothing needs recompiling:

```json
{
  "sources": [
    {
      "match": {"file": "*deposits*.xlsx", "sheet": "Sheet1"},
      "dataset": "positions",
      "columns": {"amount": "Balance EUR", "maturity_date": "Repay"},
      "constants": {"product": "Time deposit", "side": "Liability"}
    },
    {"match": {"file": "notes*.xlsx"}, "skip": true}
  ]
}
```

`columns` overrides individual fields; `constants` supplies what the sheet
simply doesn't have; `skip` ignores a workbook entirely.

---

## 2. Settings

`settings.json` in the data folder. Every value is optional.

```json
{
  "entity": "Meridian Bank — Treasury",
  "base_currency": "USD",
  "as_of": "2026-07-24",
  "capital_base": 250000000,
  "nop_limit_pct_per_currency": 0.10,
  "nop_limit_pct_aggregate": 0.20,
  "concentration_top_n": 10,
  "cash_daily_days": 30,
  "lcr_table": "lcr_basel",
  "nsfr_table": "nsfr_basel",
  "lcr_warn": 1.10,
  "var_confidence": 0.99,
  "default_fx_volatility": 0.08,
  "quotes_db": "data/output/quotes.db"
}
```

`capital_base` is what turns FX limits on — without it the NOP is shown but not
measured against anything. `as_of` defaults to the latest date in the data.

---

## 3. Regulatory factors are data, not code

LCR run-off rates, NSFR ASF/RSF factors and HQLA haircuts live in
`treasury/regs/*.json` as **first-match rule tables**:

```json
{"id": "wholesale_nonfi",
 "label": "Non-operational deposits — corporate, sovereign, PSE",
 "factor": 0.4,
 "when": {"counterparty_type": ["CORPORATE", "SOVEREIGN", "PSE"]}}
```

Every weighted line in the LCR and NSFR tables names the rule that produced it,
so a reviewer can trace any figure back to a line in a JSON file. Anything no
rule matches takes the table's conservative default and is labelled
`Unmatched` — those rows are the ones to review first.

To run your regulator's overlay: copy `lcr_basel.json`, edit the factors, put it
in a folder of your own, and set `"regs_dir"` and `"lcr_table"` in
`settings.json`. The shipped tables are Basel defaults; HKMA, FINMA and EBA all
differ in places, and the file is where those differences belong.

Condition grammar: exact value, list membership, `{"lte": 30}` / `gte` / `lt` /
`gt`, `{"not": [...]}`, `null` for "must be empty".

---

## 4. The client map

Clients are placed by explicit lat/lon, then by city
(`treasury/data/places.json`), then by country centroid. Financial centres too
small to have a polygon at map scale — Hong Kong, Singapore, Luxembourg,
Bahrain — are in `treasury/data/jurisdictions.json`. Both files are plain JSON
you can add rows to.

Clicking a country opens its panel, built from **overlays**:

- **Exposure** — clients, funding taken, lending placed, undrawn, live deals
- **Quota and limits** — country exposure against a limit you set, with any
  counterparty breaches there
- **Regulation** — regulator, desk notes, the things a dealer needs before
  quoting
- **Current bid / offer** — the money-market curve for that jurisdiction's
  currency, from your rates sheet or the OCR pipeline's `quotes.db`
- **Clients here** — who they are and how much of their line is used

Regulation and quota text comes from `country_overlays.json`. A template ships
in `treasury/data/`; **put your own copy in your data folder and it wins, key by
key**. The shipped file has regulator names filled in and everything else left
for your team — it is a starting structure, not a compliance source.

Adding a new interaction is one function:

```python
# treasury/overlays.py
@overlay("settlement", title="Settlement calendar", order=45)
def settlement(book, iso2, context):
    if iso2 not in HOLIDAYS:
        return None
    return section_list(HOLIDAYS[iso2])
```

Return `None` and the section is skipped for that country.

---

## 5. Adding a metric

A metric is one function that returns data. The front end renders panels
generically, so there is no HTML, no JavaScript and no route to touch:

```python
# treasury/metrics/duration.py
from . import metric, spec

@metric("duration", title="Portfolio duration", group="Market risk",
        needs=("securities",), order=35)
def duration(book, **params):
    rows = book.rows("securities")
    total = sum(r["base_amount"] or 0 for r in rows)
    weighted = sum((r["residual_days"] / 365) * (r["base_amount"] or 0) for r in rows)
    years = weighted / total if total else None
    return {
        "hero": spec.hero("Weighted average life", years, "number"),
        "kpis": [spec.kpi("Portfolio", total, "money")],
        "charts": [spec.bar("By currency", labels,
                            [spec.series("Market value", values)], format="money")],
        "tables": [spec.table("Holdings", columns, rows)],
        "notes": ["Simple average life, not modified duration."],
    }
```

Import it from `treasury/metrics/__init__.py` and it appears in the navigation,
on the overview, and in the snapshot history. `needs` names the datasets it
requires; if they're absent the panel says so instead of failing.

Available building blocks in `treasury.metrics.spec`: `hero`, `kpi`,
`status_for`, `bar` (grouped or stacked), `hbar`, `line`, `waterfall`,
`reference`, `table`, `col`, `series`. Formats: `money`, `money_ccy`, `ratio`,
`pct`, `rate`, `bps`, `price`, `days`, `number`, `date`, `text`.

---

## 6. Trend history

The dashboard keeps a small `treasury.db` beside your data with the headline
number from every panel and the FX rates behind it. That gives the ratios a
history line, and gives P&L a prior mark for FX revaluation.

```powershell
python dashboard.py --data data\treasury --snapshot     # store today's numbers
```

Press **Snapshot** in the UI, or run it from a scheduled task at close of
business. FX revaluation needs at least two snapshots before it reports
anything, and says so until then.

---

## 7. Command line

```powershell
python dashboard.py --data data\treasury --open       # serve on 127.0.0.1:8787
python dashboard.py --data data\treasury --check      # what was understood, then exit
python dashboard.py --data data\treasury --snapshot   # store headline history
python dashboard.py --data data\treasury --export board.json
```

| flag | meaning |
|---|---|
| `--data` | folder of `.xlsx` / `.csv` source files |
| `--port`, `--host` | default `8787` on `127.0.0.1` |
| `--base-currency`, `--as-of` | override `settings.json` |
| `--quotes-db` | the OCR pipeline's `quotes.db`, for live bid/offer on the map |

`--check` is the one to run first on a new data set: it prints every sheet, what
it was read as, and which metrics that makes available.

**Sharing it with the desk.** The default binding is localhost only. To let the
other three people reach it, `--host 0.0.0.0` — but there is **no
authentication**, so only do that on a trusted internal network, and remember
the page shows the whole balance sheet.

---

## 8. Using it with the OCR pipeline

The quote-sheet OCR pipeline in this repo writes `quotes.db`. Point the
dashboard at it and the money-market curve loads automatically — the map's
bid/offer overlay then shows the actual broker quotes you scanned that morning:

```powershell
python ingest.py data\inbox --out data\output           # OCR -> quotes.db
python dashboard.py --data data\treasury --quotes-db data\output\quotes.db --open
```

---

## 9. Offline install

Nothing extra is needed beyond the existing bundle — the dashboard adds no
dependencies past `openpyxl`, which `requirements.txt` already pins. Follow
[DEPLOYMENT_OFFLINE.md](DEPLOYMENT_OFFLINE.md), then:

```powershell
python dashboard.py --data D:\treasury\data --open
```

Confirm there is no network dependency by pulling the cable and reloading: the
map, the fonts and the charts all come from `treasury/static/`.

---

## 10. Tests

```powershell
python -m unittest discover tests -v
```

The LCR and NSFR tests use books small enough to check on paper — if a factor in
`treasury/regs/` changes, the arithmetic in `tests/test_treasury.py` is what
should fail.

---

## What it does not do

Worth knowing before the numbers reach a committee:

- **Interest is simple accrual**, settled with principal. No amortisation, no
  coupon schedules for contracts longer than a year, no fair-value moves on the
  securities book.
- **VaR is parametric and uncorrelated** — position × σ × z × √(t/252). The
  diversified figure assumes independence, the undiversified sum assumes perfect
  correlation, and the truth is between. No historical simulation.
- **Derivatives beyond FX are not modelled.** Add them as positions with an
  explicit side if they matter to the desk.
- **The cash ladder is contractual**, not behavioural: it shows what happens if
  nothing rolls. Demand balances sit outside it as a separate figure.
- **The regulatory tables are Basel defaults**, not your regulator's rulebook,
  and the jurisdiction notes are a blank template. Both are yours to own.
