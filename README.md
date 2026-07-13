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
| `currency` | currency or pair | `USD/CNY` |
| `tenor` | tenor bucket | `O/N`, `1W`, `1M`, `3M`, `1Y` |
| `bid` | bid / left price | `6.1234` |
| `offer` | offer / right price | `6.1240` |
| `source_file`, `page`, `confidence`, `raw` | provenance & auditing | |

Fields that aren't present are left blank rather than dropped.

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
python examples/make_sample.py                       # writes examples/sample_fx_quote.png
python run.py examples/sample_fx_quote.png -o out.csv
```
