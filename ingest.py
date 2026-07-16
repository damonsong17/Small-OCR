#!/usr/bin/env python3
"""Batch-ingest a folder of dated quote images (production entry point).

    python ingest.py data/inbox --out data/output --model server --enhance

- Scans data/inbox for images named SOURCE_YYYYMMDD.<ext> (subfolders OK).
- Skips files already ingested (by content hash) -- only new/changed images are
  processed, so history is never rebuilt.
- Writes data/output/csv/, data/output/xlsx/, and data/output/quotes.db.
"""
from __future__ import annotations

import argparse
import sys

from quote_ocr import Config
from quote_ocr.batch import ingest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inbox", help="Folder of dated source images.")
    p.add_argument("--out", default="output", help="Output directory (default: output).")
    p.add_argument("--force", action="store_true",
                   help="Reprocess every file, ignoring the incremental manifest.")
    p.add_argument("--no-xlsx", action="store_true", help="Skip Excel export.")
    # OCR options (mirror run.py; sensible production defaults).
    p.add_argument("--model", default="mobile", choices=["mobile", "server"],
                   help="mobile (default) = fast, right for i5/UHD 630; server = "
                        "more accurate but ~10x slower on CPU.")
    p.add_argument("--engine", default="onnxruntime", choices=["onnxruntime", "openvino"])
    p.add_argument("--layout", default="auto", choices=["auto", "matrix", "section"])
    p.add_argument("--lang", default="ch", choices=["ch", "en"])
    p.add_argument("--enhance", action="store_true", default=True,
                   help="Preprocess + auto-upscale small images (default on).")
    p.add_argument("--no-enhance", dest="enhance", action="store_false")
    p.add_argument("--tenor-fix", default="15=1M,25=2M,35=3M,65=6M", metavar="MAP",
                   help="Tenor misread corrections (default handles s->digit slips).")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    tenor_fixups = {}
    for pair in args.tenor_fix.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            tenor_fixups[k.strip()] = v.strip()

    config = Config(
        model_type=args.model,
        engine=args.engine,
        layout=args.layout,
        det_lang="ch" if args.lang == "ch" else "en",
        rec_lang=args.lang,
        grayscale=args.enhance,
        sharpen=args.enhance,
        contrast=1.5 if args.enhance else 1.0,
        target_width=1800 if args.enhance else 0,
        tenor_fixups=tenor_fixups,
    )

    print(f"Ingesting {args.inbox} -> {args.out} "
          f"(PP-OCRv5 {args.model}, enhance={args.enhance}) ...")
    if args.model == "server":
        print("  note: server model is ~10x slower on CPU; expect minutes/image "
              "on an i5. Use --model mobile if it's too slow.")
    s = ingest(args.inbox, args.out, config, force=args.force,
               make_xlsx=not args.no_xlsx)
    print(f"\nDone. processed={s['processed']} skipped={s['skipped']} "
          f"unnamed={s['unnamed']} quotes={s['quotes']}")
    print(f"DB: {args.out}/quotes.db   CSV: {args.out}/csv/   XLSX: {args.out}/xlsx/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
