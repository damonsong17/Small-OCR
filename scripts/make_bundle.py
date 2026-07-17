#!/usr/bin/env python3
"""Build an OFFLINE install bundle on a NETWORKED machine.

Run this on an internet-connected machine that has the **same OS and Python
version** as the air-gapped target (both Windows, both e.g. Python 3.11). It:

  1. freezes the current (working) environment to requirements.lock.txt,
  2. downloads every wheel (incl. transitive deps) into bundle/wheelhouse,
  3. optionally downloads the Bloomberg blpapi wheel,
  4. copies the cached RapidOCR model weights into bundle/models,

so the target machine can install with no network. Copy the whole `bundle/`
folder (plus this repo) to the target via USB.

    python scripts/make_bundle.py

Then on the target machine:  python scripts/install_bundle.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "bundle"
WHEELS = BUNDLE / "wheelhouse"
MODELS = BUNDLE / "models"
LOCK = BUNDLE / "requirements.lock.txt"

BLOOMBERG_INDEX = "https://blpapi.bloomberg.com/repository/releases/python/simple/"


def run(cmd):
    print(">", " ".join(cmd))
    subprocess.check_call(cmd)


def main():
    WHEELS.mkdir(parents=True, exist_ok=True)
    MODELS.mkdir(parents=True, exist_ok=True)
    pip = [sys.executable, "-m", "pip"]

    print(f"\nPython {sys.version.split()[0]} on {sys.platform} "
          f"-- the target machine must match this.\n")

    # 1) lock the working environment
    with open(LOCK, "w") as f:
        subprocess.check_call(pip + ["freeze"], stdout=f)
    print(f"wrote {LOCK}")

    # 2) download all wheels for the lock
    run(pip + ["download", "-r", str(LOCK), "-d", str(WHEELS)])

    # 3) Bloomberg blpapi (best effort; needs Bloomberg's index reachable)
    try:
        run(pip + ["download", "blpapi", "--index-url", BLOOMBERG_INDEX,
                   "-d", str(WHEELS)])
    except subprocess.CalledProcessError:
        print("  ! blpapi download failed (Bloomberg index not reachable here). "
              "Download it on a machine that can reach Bloomberg, or install it "
              "on the target from the terminal's API SDK. See DEPLOYMENT_OFFLINE.md")

    # 4) copy cached RapidOCR model weights (trigger a run first if empty)
    try:
        import rapidocr
        src = Path(rapidocr.__file__).parent / "models"
        onnx = list(src.glob("*.onnx")) if src.exists() else []
        if not onnx:
            print("  models not cached yet -- running OCR once to fetch them ...")
            _warm_models()
            onnx = list(src.glob("*.onnx"))
        for f in onnx:
            shutil.copy2(f, MODELS / f.name)
        print(f"copied {len(onnx)} model file(s) to {MODELS}")
    except Exception as e:  # pragma: no cover
        print(f"  ! could not stage models automatically: {e}")

    print(f"\nDone. Bundle at: {BUNDLE}")
    print("Copy the 'bundle' folder + this repo to the target, then run "
          "scripts/install_bundle.py there.")


def _warm_models():
    """Run one tiny OCR to force RapidOCR to download the model weights."""
    import numpy as np
    from quote_ocr import Config
    from quote_ocr.ocr_engine import OcrEngine
    img = (np.ones((60, 200, 3)) * 255).astype("uint8")
    OcrEngine(Config()).run(img)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
