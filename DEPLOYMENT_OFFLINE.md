# Offline / Air-Gapped Deployment (Windows internal machine)

Goal: install and run the whole pipeline on an internal Windows box **with no
network**, from `python` + shell only (no VS Code), and wire in the **Bloomberg
terminal** (Professional, not Anywhere) for FX market data.

The trick to avoid two-machine debugging: **build and verify the bundle on a
networked machine that has the SAME OS + Python version as the target**, so if
the offline install works there, it works on the internal box.

---

## 0. Prerequisite (read this first)

The networked "builder" machine and the air-gapped "target" machine must have
the **same Python version** (e.g. both Python 3.11, 64-bit Windows). Check with
`python --version` on both. Wheels are Python-version- and OS-specific.

## 1. On the NETWORKED machine — build the bundle

```powershell
git clone <repo>  &&  cd Small-OCR
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # installs + resolves everything
python run.py examples\make_sample.py    # optional: warms the OCR model cache
python examples\make_sample.py
python run.py examples\sample_fx_quote.png -o out.csv   # confirms it works online

python scripts\make_bundle.py            # creates .\bundle\  (wheels + models + lock)
```

`make_bundle.py` writes:
- `bundle\wheelhouse\` — every wheel (incl. transitive deps),
- `bundle\models\` — the RapidOCR `.onnx` weights (so no first-run download),
- `bundle\requirements.lock.txt` — exact pinned versions.

### Bloomberg blpapi
`make_bundle.py` also tries to download `blpapi` from Bloomberg's index. If that
machine can't reach Bloomberg, get the wheel where you can:
```powershell
pip download blpapi --index-url https://blpapi.bloomberg.com/repository/releases/python/simple/ -d bundle\wheelhouse
```
Modern `blpapi` wheels bundle the C++ SDK, so the single wheel is usually enough.

### Verify offline install BEFORE copying (this is what prevents back-and-forth)
On the **same networked machine**, in a throwaway venv, install from the bundle
with `--no-index` — this proves nothing is missing:
```powershell
python -m venv .venv_test
.venv_test\Scripts\Activate.ps1
pip install --no-index --find-links bundle\wheelhouse -r bundle\requirements.lock.txt
python -c "import rapidocr, onnxruntime, fitz, openpyxl; print('offline install OK')"
deactivate
```
If that succeeds, the target will succeed too.

## 2. Transfer

Copy the **repo folder** and the **`bundle\` folder** to the target via USB.

## 3. On the AIR-GAPPED target — install

```powershell
cd Small-OCR
python -m venv .venv
.venv\Scripts\Activate.ps1
python scripts\install_bundle.py         # installs from bundle, stages models, verifies
```

Run the OCR pipeline (no network needed):
```powershell
python ingest.py data --out data\output --enhance
```

## 4. Bloomberg (FXFA / USDCNH) on the target

The Desktop API needs the **terminal running and logged in on this machine**
(Bloomberg *Anywhere* is NOT required). Then:

```powershell
# offline framework test (mock market data, no terminal needed):
python price.py --db data\output\quotes.db --date 2026-07-16

# live: pull real USDCNH spot + forward points from the terminal:
python price.py --db data\output\quotes.db --date 2026-07-16 --live
```

⚠️ **Verify the tickers/fields** for FX forward points against `FRD`/`FXFA` on
your terminal, then adjust `FX_TICKERS` / `TENOR_CODE` in
`quote_ocr\bloomberg.py`. The defaults (`USDCNH Curncy`, `USDCNH1M Curncy`,
`PX_LAST`) are a starting point — the pipeline is structured so this mapping is
the only thing you change.

## 5. What price.py does (framework)

1. reads USD + CNH funding rates for a date from `quotes.db` (your OCR output),
2. pulls USDCNH spot + forward points from Bloomberg (mock or live),
3. computes the **CIP-fair forward points** and the **pickup vs market**,
   per tenor (see `quote_ocr\pricing.py`).

This is the skeleton for the arb/quoting engine. The exact bid/offer side to use
(borrow vs lend) and the points/outright convention are marked `TODO` in
`pricing.py`/`bloomberg.py` — they set the numbers, not the structure.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `pip install` wants the network | You skipped `--no-index`; use `install_bundle.py`. |
| A wheel is "not a supported wheel on this platform" | Builder and target Python/OS differ — rebuild the bundle on a matching machine. |
| First OCR run tries to download models | Models weren't staged — copy `bundle\models\*.onnx` into `…\site-packages\rapidocr\models\`. |
| blpapi `Could not start session` | Terminal not running/logged in on this machine, or DAPI disabled. |
| blpapi import error | The wheel's C++ SDK DLLs aren't found — install the matching Bloomberg C++ SDK, or use a bundled-SDK blpapi wheel. |
