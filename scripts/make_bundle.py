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


def _is_blpapi(req_line: str) -> bool:
    return req_line.strip().lower().startswith("blpapi")


def main():
    WHEELS.mkdir(parents=True, exist_ok=True)
    MODELS.mkdir(parents=True, exist_ok=True)
    pip = [sys.executable, "-m", "pip"]

    print(f"\nPython {sys.version.split()[0]} on {sys.platform} "
          f"-- the target machine must match this.\n")

    # 1) lock the working environment (full list, used for the offline install)
    with open(LOCK, "w") as f:
        subprocess.check_call(pip + ["freeze"], stdout=f)
    print(f"wrote {LOCK}")

    # 2) BUILD a wheel for every dependency EXCEPT blpapi (blpapi lives on
    #    Bloomberg's private index, not PyPI, so `pip wheel` from PyPI would
    #    fail on it). Everything else is built here (turns source-only packages
    #    like antlr4-python3-runtime into wheels) so the target never compiles.
    lines = LOCK.read_text().splitlines()
    build_reqs = [ln for ln in lines if ln.strip() and not _is_blpapi(ln)]
    build_file = BUNDLE / "_build_reqs.txt"
    build_file.write_text("\n".join(build_reqs) + "\n")
    run(pip + ["wheel", "-r", str(build_file), "-w", str(WHEELS)])
    #    Include pip + the build backend so a fresh venv can bootstrap offline
    #    (Python 3.12+ venvs don't ship setuptools).
    run(pip + ["wheel", "pip", "setuptools", "wheel", "-w", str(WHEELS)])

    # 3) blpapi: from Bloomberg's index (skip if a wheel is already staged).
    if not any(WHEELS.glob("blpapi*")):
        try:
            run(pip + ["download", "blpapi", "--index-url", BLOOMBERG_INDEX,
                       "-d", str(WHEELS)])
        except subprocess.CalledProcessError:
            pass
    if not any(WHEELS.glob("blpapi*")):
        # Can't get blpapi here -> drop it from the lock so the OFFLINE install
        # of everything else still succeeds; the Bloomberg step is added later.
        kept = [ln for ln in lines if not _is_blpapi(ln)]
        LOCK.write_text("\n".join(kept) + "\n")
        print("  ! blpapi wheel unavailable -> removed from lock. Add it on the "
              "target from the Bloomberg API SDK before using the pricing step.")
    else:
        print("  blpapi wheel present in bundle.")

    # 4) stage RapidOCR model weights. Always warm the model ingest.py uses by
    #    default (PP-OCRv5 mobile) so the offline box never tries to download,
    #    then copy every cached .onnx (covers server too if present).
    try:
        import rapidocr
        src = Path(rapidocr.__file__).parent / "models"
        print("  ensuring the default (mobile) OCR weights are cached ...")
        _warm_models()
        onnx = list(src.glob("*.onnx")) if src.exists() else []
        for f in onnx:
            shutil.copy2(f, MODELS / f.name)
        names = ", ".join(f.name for f in onnx)
        print(f"copied {len(onnx)} model file(s) to {MODELS}: {names}")
        if not any("mobile" in f.name.lower() for f in onnx):
            print("  ! WARNING: no 'mobile' model staged -- ingest.py defaults to "
                  "mobile and will try to download it offline. Run one "
                  "`python ingest.py ... --model mobile` online first, then rebuild.")
    except Exception as e:  # pragma: no cover
        print(f"  ! could not stage models automatically: {e}")

    _preflight()
    print(f"\nDone. Bundle at: {BUNDLE}")
    print("Copy the 'bundle' folder + this repo to the target, then run "
          "scripts/install_bundle.py there.")


def _preflight():
    """Fail loudly here (with internet) rather than on the offline box."""
    print("\n--- preflight ---")
    ok = True
    sdists = [f.name for f in WHEELS.glob("*") if f.suffix not in (".whl",)]
    if sdists:
        ok = False
        print(f"  FAIL: non-wheel files in wheelhouse (offline box would compile): {sdists}")
    else:
        print(f"  OK: wheelhouse is all wheels ({len(list(WHEELS.glob('*.whl')))}).")

    models = list(MODELS.glob("*.onnx"))
    if any("mobile" in m.name.lower() for m in models):
        print(f"  OK: mobile OCR weights staged ({len(models)} files).")
    else:
        ok = False
        print("  FAIL: mobile OCR weights not staged (ingest.py defaults to mobile).")

    if any(WHEELS.glob("blpapi*")):
        print("  OK: blpapi wheel present.")
    else:
        print("  note: no blpapi wheel (only needed for the Bloomberg step).")

    print(f"  Python for this bundle: {sys.version.split()[0]} / {sys.platform} "
          f"-- the target MUST match.")
    print("  " + ("PREFLIGHT PASSED" if ok else ">>> PREFLIGHT FAILED - fix before copying <<<"))


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
