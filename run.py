#!/usr/bin/env python3
"""CLI for the quote OCR pipeline.

Examples
--------
    # single image -> CSV
    python run.py samples/quote.png -o out.csv

    # a whole folder -> JSON, tag the supplier, use the more accurate model
    python run.py samples/ -o out.json --supplier "ACME BROKERS" --model server

    # a PDF (needs: pip install pymupdf)
    python run.py samples/quotes.pdf -o out.csv
"""
from __future__ import annotations

import argparse
import sys

from quote_ocr import Config, QuotePipeline
from quote_ocr.loader import collect_inputs
from quote_ocr.writer import write


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="OCR quote sheets (images/PDF) into structured bid/offer rows."
    )
    p.add_argument("inputs", nargs="+", help="Image/PDF files or folders.")
    p.add_argument("-o", "--output", default="quotes.csv", help="Output file path.")
    p.add_argument(
        "-f", "--format", choices=["csv", "json"], default=None,
        help="Output format (default: inferred from -o extension).",
    )
    p.add_argument("--supplier", default="", help="Supplier/broker name to tag rows.")
    p.add_argument(
        "--ocr-version", default="PP-OCRv5",
        choices=["PP-OCRv4", "PP-OCRv5", "PP-OCRv6"],
        help="PP-OCR model family (default: PP-OCRv5).",
    )
    p.add_argument(
        "--model", default="mobile", choices=["mobile", "server"],
        help="mobile = fast (default); server = more accurate.",
    )
    p.add_argument(
        "--engine", default="onnxruntime", choices=["onnxruntime", "openvino"],
        help="Inference backend. Use openvino to accelerate on Intel hardware.",
    )
    p.add_argument(
        "--lang", default="ch", choices=["ch", "en"],
        help="Recognition language pack. 'ch' also handles English (default).",
    )
    p.add_argument("--quiet", action="store_true", help="Suppress the summary table.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    files = collect_inputs(args.inputs)
    if not files:
        print("No supported input files found.", file=sys.stderr)
        return 1

    fmt = args.format or ("json" if args.output.lower().endswith(".json") else "csv")

    config = Config(
        ocr_version=args.ocr_version,
        model_type=args.model,
        engine=args.engine,
        det_lang="ch" if args.lang == "ch" else "en",
        rec_lang=args.lang,
        supplier=args.supplier,
    )

    print(f"Loading {config.ocr_version} ({config.model_type}, {config.engine}) ...")
    pipeline = QuotePipeline(config)

    quotes = []
    for path in files:
        rows = pipeline.run_file(path, supplier=args.supplier)
        quotes.extend(rows)
        print(f"  {path}: {len(rows)} quote(s)")

    write(quotes, args.output, fmt=fmt)
    print(f"\nWrote {len(quotes)} quote(s) to {args.output} ({fmt}).")

    if not args.quiet and quotes:
        _preview(quotes)
    return 0


def _preview(quotes, limit: int = 12) -> None:
    cols = ["date", "supplier", "currency", "tenor", "bid", "offer"]
    widths = {c: len(c) for c in cols}
    for q in quotes[:limit]:
        row = q.as_row()
        for c in cols:
            widths[c] = max(widths[c], len(str(row[c])))
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print("\n" + header)
    print("  ".join("-" * widths[c] for c in cols))
    for q in quotes[:limit]:
        row = q.as_row()
        print("  ".join(str(row[c]).ljust(widths[c]) for c in cols))
    if len(quotes) > limit:
        print(f"... ({len(quotes) - limit} more)")


if __name__ == "__main__":
    raise SystemExit(main())
