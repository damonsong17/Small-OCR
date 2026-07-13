# Deployment Guide (Windows + VS Code, Intel i5-9500T / UHD 630)

This pipeline is intentionally lightweight: **PP-OCRv5** detection + recognition
models (a few tens of MB) run on CPU via ONNX Runtime, with an optional Intel
**OpenVINO** backend. No GPU VRAM is required; the UHD 630 iGPU is optional.

## 1. Install

```powershell
# From the repo root, in the VS Code integrated terminal:
python -m venv .venv
.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process RemoteSigned
python -m pip install --upgrade pip
pip install -r requirements.txt
```

On the **first run**, RapidOCR downloads the PP-OCRv5 ONNX weights automatically
(from ModelScope) and caches them inside the package. Later runs are offline.

> Behind a firewall that blocks ModelScope? Download the `ch_PP-OCRv5_det_mobile.onnx`
> and `ch_PP-OCRv5_rec_mobile.onnx` files manually and point to them via
> `Det.model_path` / `Rec.model_path` in `quote_ocr/ocr_engine.py`.

## 2. Run

```powershell
# image -> CSV
python run.py samples\quote.png -o quotes.csv

# folder of images -> JSON, tag supplier, higher-accuracy model
python run.py samples\ -o quotes.json --supplier "ACME BROKERS" --model server

# PDF -> CSV (pymupdf already installed via requirements.txt)
python run.py samples\quotes.pdf -o quotes.csv
```

### CLI options

| flag | default | notes |
|---|---|---|
| `-o, --output` | `quotes.csv` | format inferred from extension |
| `-f, --format` | inferred | `csv` or `json` |
| `--supplier` | *(auto)* | force a supplier tag on every row |
| `--ocr-version` | `PP-OCRv5` | `PP-OCRv4` / `PP-OCRv5` / `PP-OCRv6` |
| `--model` | `mobile` | `mobile` (fast) or `server` (more accurate) |
| `--layout` | `auto` | `auto` / `matrix` (wide grid) / `section` |
| `--engine` | `onnxruntime` | `openvino` to accelerate on Intel |
| `--lang` | `ch` | `ch` handles CN+EN; `en` for latin-only |

## 3. Intel acceleration (optional)

The i5-9500T CPU alone is plenty for the PP-OCRv5 *mobile* models (well under a
second per clean screenshot). If you want to try the UHD 630 / Intel-optimised
path:

```powershell
pip install openvino
python run.py samples\quote.png -o quotes.csv --engine openvino
```

Keep `--engine onnxruntime` (the default) if you want the most predictable,
zero-extra-dependency setup — on this hardware it is already fast.

## 4. Output schema

CSV/JSON columns: `date, supplier, segment, currency, benchmark, benchmark_rate,
tenor, bid, offer, source_file, page, confidence, raw`. `raw` is the
reconstructed row text, handy for spot-checks. Missing fields are left blank.

For a **matrix** sheet you get **one row per tenor × currency** — e.g. a single
`o/n` line across USD/EUR/CNH/HKD becomes four records.

## 5. How it works

```
image/PDF ─▶ loader ─▶ PP-OCRv5 (RapidOCR) ─▶ rows ─▶ layout parser ─▶ CSV/JSON
            (loader.py)   (ocr_engine.py)              (table_parser.py /   (writer.py)
                                                         parser.py)
```

1. **Load** — images directly; PDFs rendered to page images (PyMuPDF).
2. **OCR** — PP-OCRv5 returns text boxes + confidence.
3. **Rows** — boxes are clustered by vertical position to rebuild table rows.
4. **Parse** — the layout is auto-detected:
   - **matrix** (`table_parser.py`): reads the `BID`/`OFFER` header band to build
     a column model, then reads every cell by x-position — so all currencies in
     a row are captured and missing `-` cells stay blank. Emits one record per
     tenor × currency, with `segment` / `benchmark` / `benchmark_rate`.
   - **section** (`parser.py`): one currency block at a time, tracking the
     current currency from header rows.
   - `date` & `supplier` are page-level in both.

## 6. Tuning for your sheets

Everything adjustable lives in **`quote_ocr/config.py`**:

- **`currencies`** — add any codes your desk quotes (metals, EM, crosses).
- **`row_y_tolerance`** — raise if rows are being split, lower if adjacent
  rows merge.
- **`min_prices_for_quote`** — set to `2` to only emit rows that have both a
  bid and an offer.
- **`default_currency`** — fallback when a sheet has no currency header.

Tenor patterns and date/number regexes are at the top of **`quote_ocr/parser.py`**.

## 7. Extending

- **PDF** is already supported (install is in `requirements.txt`).
- **New fields** (e.g. `spot`, `valuation date`, `dealer`): add the field to
  `FIELDNAMES` + `Quote` in `quote_ocr/models.py`, then populate it in
  `QuoteParser.parse`.
- **Swap the OCR model**: change `ocr_version` / `model_type` in `Config`, or
  point `Det.model_path` / `Rec.model_path` at your own ONNX files.
- **Higher accuracy on messy scans**: try `--model server`; the parser is
  unchanged.

## 8. Sanity check (offline)

```powershell
# section layout -> 8 rows across USD/CNY and EUR/USD
python examples\make_sample.py
python run.py examples\sample_fx_quote.png -o out.csv

# matrix layout -> 35 rows (USD/EUR/CNH/HKD grid + Korean/Taiwanese/Indian/ISLAMIC)
python examples\make_matrix_sample.py
python run.py examples\sample_matrix_quote.png -o out.csv
```
