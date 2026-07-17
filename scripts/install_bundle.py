#!/usr/bin/env python3
"""Install the OFFLINE bundle on the air-gapped target machine.

Run inside a fresh virtual environment on the target (no network needed):

    python -m venv .venv
    .venv\\Scripts\\activate          # Windows
    python scripts/install_bundle.py

It installs every wheel from bundle/wheelhouse with no index, then copies the
RapidOCR model weights into the installed package so first run needs no network.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "bundle"
WHEELS = BUNDLE / "wheelhouse"
MODELS = BUNDLE / "models"
LOCK = BUNDLE / "requirements.lock.txt"


def run(cmd):
    print(">", " ".join(cmd))
    subprocess.check_call(cmd)


def main():
    if not WHEELS.exists() or not LOCK.exists():
        sys.exit(f"Bundle not found. Expected {WHEELS} and {LOCK}. "
                 f"Copy the 'bundle' folder from make_bundle.py first.")

    pip = [sys.executable, "-m", "pip"]
    run(pip + ["install", "--no-index", "--find-links", str(WHEELS), "-r", str(LOCK)])

    # optionally blpapi if its wheel was bundled
    if any(WHEELS.glob("blpapi*")):
        run(pip + ["install", "--no-index", "--find-links", str(WHEELS), "blpapi"])

    # stage model weights into the installed rapidocr package
    try:
        import rapidocr
        dst = Path(rapidocr.__file__).parent / "models"
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in MODELS.glob("*.onnx"):
            shutil.copy2(f, dst / f.name)
            n += 1
        print(f"copied {n} model file(s) into {dst}")
    except Exception as e:  # pragma: no cover
        print(f"  ! could not stage models: {e}")

    print("\nVerifying imports ...")
    subprocess.check_call([sys.executable, "-c",
                           "import rapidocr, onnxruntime, fitz, openpyxl; "
                           "print('core OK')"])
    print("Done. Try:  python ingest.py data --out data\\output --enhance")


if __name__ == "__main__":
    main()
