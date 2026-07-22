#!/usr/bin/env python3
"""Package the repo + offline bundle into a single zip for the air-gapped box.

Run on the NETWORKED builder machine (same OS + Python version as the target),
after installing deps:

    pip install -r requirements.txt
    python scripts/pack.py

Produces  Small-OCR_offline.zip  containing the source + the bundle folder
(wheels + model weights + lock), and EXCLUDING .venv, .git, caches, and local
data. Copy that one zip to the target, unzip, run scripts/install_bundle.py.

Pass --no-build to skip rebuilding the bundle (reuse an existing bundle\).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "Small-OCR_offline.zip"

# Directory names / suffixes never copied to the offline machine.
EXCLUDE_DIRS = {".venv", ".venv_test", "venv", ".git", "__pycache__",
                ".idea", ".vscode", "data", "output"}
EXCLUDE_SUFFIX = {".pyc", ".db", ".zip"}


def _skip(rel: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if rel.suffix in EXCLUDE_SUFFIX:
        return True
    return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--no-build", action="store_true",
                   help="Reuse existing bundle\\ instead of rebuilding it.")
    args = p.parse_args(argv)

    if not args.no_build:
        print("Building offline bundle ...")
        subprocess.check_call([sys.executable, str(ROOT / "scripts" / "make_bundle.py")])

    if not (ROOT / "bundle" / "wheelhouse").exists():
        sys.exit("No bundle\\wheelhouse found. Run without --no-build, or run "
                 "scripts/make_bundle.py first.")

    if OUT.exists():
        OUT.unlink()

    n = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ROOT.rglob("*"):
            if not f.is_file() or f == OUT:
                continue
            rel = f.relative_to(ROOT)
            if _skip(rel):
                continue
            z.write(f, Path("Small-OCR") / rel)
            n += 1

    print(f"\nWrote {OUT}  ({n} files, {OUT.stat().st_size // (1024*1024)} MB)")
    print("Copy it to the offline machine, unzip, then:")
    print("  cd Small-OCR")
    print("  python -m venv .venv && .venv\\Scripts\\activate")
    print("  python scripts\\install_bundle.py")


if __name__ == "__main__":
    main()
