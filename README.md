# Small-OCR — Quote Sheet OCR Pipeline

Turn **quote-sheet images (JPG/PNG/…) and PDFs** into **structured bid/offer
rows** (CSV or JSON). Built on **PP-OCRv5** (via [RapidOCR](https://github.com/RapidAI/RapidOCR),
ONNX) so it runs fully locally on a modest CPU / Intel iGPU — no data leaves the
machine.

It is designed to be **one stage in a larger pipeline**: point it at an image,
get back clean records you can feed into your trade-opportunity logic.

## Extracted fields

| field | meaning | example |
|---|---|---|
| `date` | quote-sheet date | `2026-07-13` |
| `supplier` | broker / counterparty (best-effort, may be blank) | `ACME BROKERS LTD` |
| `segment` | market / desk grouping | `Chinese`, `Korean`, `ISLAMIC` |
| `currency` | currency or pair | `USD`, `CNH`, `USD/CNY` |
| `benchmark` | reference index for the row | `SOFR`, `EURIBOR`, `CNH HIBOR` |
| `benchmark_rate` | the index value, when present | `1.42848` |
| `tenor` | tenor bucket (months normalised to `M`) | `O/N`, `1W`, `2W`, `1M`, `6M`, `1Y` |
| `bid` | bid price | `1.30` |
| `offer` | offer price | `1.50` |
| `source_file`, `page`, `confidence`, `raw` | provenance & auditing | |

Fields that aren't present are left blank rather than dropped.

## Two layouts (auto-detected)

- **matrix** — wide grids where one tenor row carries many currencies side by
  side (`Tenor | SOFR BID OFFER | EURIBOR BID OFFER | CNH HIBOR BID OFFER | …`),
  optionally with stacked sub-tables. Parsed **column-by-column using
  x-positions**, so every currency is captured and missing cells (`-`) stay
  blank without shifting the row. This is emitted as **one record per
  tenor × currency**.
- **section** — simpler sheets with one currency block at a time.

`--layout auto` (default) picks matrix when the sheet has `BID`/`OFFER` column
headers, otherwise section — and if the matrix parser finds nothing it falls
back to section, so unusual structures still produce output.

**Handles varied inputs**, all into the same schema:
- **single-sided** quotes — offer-only or bid-only sheets (the missing side is
  left blank, not guessed);
- **narrow / few-column** sheets from other sources (e.g. a single
  `Tenor | Bid | Offer`), not just wide grids;
- **missing cells** (`-`) and multi-word benchmarks (`CNH HIBOR`);
- **tenor normalisation** — sources that write months as `1s`/`6s` are output as
  `1M`/`6M` (configurable via `Config.month_units`) to match FTP / Bloomberg.

**Output stays compatible:** the first ten CSV columns are exactly the original
`date, supplier, currency, tenor, bid, offer, source_file, page, confidence,
raw`; `segment`, `benchmark`, `benchmark_rate` are appended at the end, so
existing consumers keep working regardless of the input table type.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows (PowerShell: .venv\Scripts\Activate.ps1)
pip install -r requirements.txt

# single image -> CSV (PP-OCRv5 weights download automatically on first run)
python run.py path\to\quote.png -o quotes.csv

# a whole folder -> JSON, tagged with a supplier
python run.py samples\ -o quotes.json --supplier "ACME BROKERS"

# a PDF (pymupdf is in requirements.txt, so this just works)
python run.py quotes.pdf -o quotes.csv
```

## Use it as a library (pipeline stage)

```python
from quote_ocr import QuotePipeline, Config

pipe = QuotePipeline(Config(ocr_version="PP-OCRv5", model_type="mobile"))
quotes = pipe.run_file("quote.png")          # -> list[Quote]
for q in quotes:
    print(q.currency, q.tenor, q.bid, q.offer)
```

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for Windows/VS Code setup, Intel OpenVINO
acceleration, tuning, and how to extend the field extraction.

## Try it offline

```bash
# section-style sheet
python examples/make_sample.py                       # writes examples/sample_fx_quote.png
python run.py examples/sample_fx_quote.png -o out.csv

# wide matrix sheet (multi-currency grid + stacked sub-table)
python examples/make_matrix_sample.py                # writes examples/sample_matrix_quote.png
python run.py examples/sample_matrix_quote.png -o out.csv
```
