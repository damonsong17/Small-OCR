"""Batch-ingest a folder of dated quote images into CSV / SQLite / Excel.

Layout expected:
    inbox/<SOURCE>/<SOURCE>_<YYYYMMDD>.png    (subfolders optional)

Filenames drive the metadata: ``AFS_20260716.png`` -> source=AFS, date=2026-07-16.
Processing is incremental -- a file whose SHA-1 already appears in the store's
manifest is skipped, so re-running only picks up new or changed images.

Outputs are written into separate directories:
    <out>/csv/<SOURCE>/<SOURCE>_<DATE>.csv
    <out>/xlsx/<SOURCE>/<SOURCE>_<DATE>.xlsx
    <out>/quotes.db          (full history + manifest)
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from .config import Config
from .loader import SUPPORTED_SUFFIXES
from .pipeline import QuotePipeline
from .store import QuoteStore, sha1_of
from .writer import to_csv

# SOURCE_YYYYMMDD or SOURCE-YYYY-MM-DD (source may contain underscores).
_FN_RE = re.compile(r"^(?P<source>.+)[_-](?P<date>\d{4}-?\d{2}-?\d{2})$")


def parse_filename(stem: str):
    """'AFS_20260716' -> ('AFS', '2026-07-16'); None if it doesn't match."""
    m = _FN_RE.match(stem)
    if not m:
        return None
    d = m.group("date").replace("-", "")
    if len(d) != 8:
        return None
    return m.group("source"), f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _quotes_from_docx(path: str):
    """Turn a .docx rate email into Quote rows for the store."""
    from .docx_reader import parse_docx
    from .models import Quote
    surf, meta = parse_docx(path, verbose=False)
    ref_only = {c.upper() for c in meta.get("reference_only", [])}
    out = []
    def _pct(v):
        return "" if v is None else f"{v*100:.6f}".rstrip("0").rstrip(".")

    for ccy, sides in surf.items():
        # A one-sided reference rate has no bid/offer, so it must be carried by
        # its own 'mid' tenors -- leaving them out would drop the whole internal
        # money-market email from the database.
        tenors = (set(sides.get("bid", {})) | set(sides.get("offer", {}))
                  | set(sides.get("mid", {})))
        for t in tenors:
            out.append(Quote(
                currency=ccy, tenor=t,
                bid=_pct(sides.get("bid", {}).get(t)),
                offer=_pct(sides.get("offer", {}).get(t)),
                mid=_pct(sides.get("mid", {}).get(t)),
                # 'segment' is the COUNTERPARTY group and drives the KYC access
                # check, so the reference-rate marker must not live there -- an
                # internal email has no counterparty segment, and writing one
                # would get the whole channel excluded as an unknown desk.
                # The populated 'mid' column already says it is one-sided.
                segment="",
                benchmark=meta.get("settle", ""),
                source_file=Path(path).name, page=1, confidence=1.0,
                raw=f"docx {meta.get('settle','')}"
                    + (" reference-only" if ccy in ref_only else "")))
    return out


def _xlsx_builder():
    """Return to_excel.build if Excel export is available, else (None, reason)."""
    try:
        from to_excel import build  # imports openpyxl at module top
        return build, ""
    except ImportError as e:
        return None, f"{e} (run: pip install openpyxl)"
    except Exception as e:  # pragma: no cover
        return None, str(e)


def ingest(
    inbox: str,
    out: str,
    config: Optional[Config] = None,
    force: bool = False,
    make_xlsx: bool = True,
    verbose: bool = True,
) -> dict:
    out_path = Path(out)
    csv_dir = out_path / "csv"
    xlsx_dir = out_path / "xlsx"
    csv_dir.mkdir(parents=True, exist_ok=True)
    if make_xlsx:
        xlsx_dir.mkdir(parents=True, exist_ok=True)

    # Built on first image, not up front: a folder holding only .docx rate
    # emails needs no OCR engine at all, and eager construction would download
    # (or fail to find) the models for nothing -- fatal on the offline machine.
    _pipeline = []

    def pipeline():
        if not _pipeline:
            _pipeline.append(QuotePipeline(config or Config()))
        return _pipeline[0]

    summary = {"processed": 0, "skipped": 0, "unnamed": 0, "quotes": 0}

    xlsx_build = None
    if make_xlsx:
        xlsx_build, why = _xlsx_builder()
        if xlsx_build is None:
            print(f"  ! Excel export unavailable, skipping .xlsx (CSV/DB still "
                  f"written): {why}")
            make_xlsx = False

    inbox_path = Path(inbox)
    if not inbox_path.exists():
        print(f"  ! inbox folder does not exist: {inbox}")
        return {"processed": 0, "skipped": 0, "unnamed": 0, "quotes": 0}

    files = [
        p for p in sorted(inbox_path.rglob("*"))
        if p.suffix.lower() in SUPPORTED_SUFFIXES or p.suffix.lower() == ".docx"
    ]
    if verbose:
        print(f"  found {len(files)} image file(s) under {inbox}")
    if not files:
        print(f"  ! no images ({', '.join(sorted(SUPPORTED_SUFFIXES))}) found under {inbox}")

    n = len(files)
    with QuoteStore(str(out_path / "quotes.db")) as store:
        for i, path in enumerate(files, 1):
            parsed = parse_filename(path.stem)
            if not parsed:
                summary["unnamed"] += 1
                if verbose:
                    print(f"  [{i}/{n}] ! skip (name not SOURCE_YYYYMMDD): {path.name}")
                continue
            source, date = parsed
            sha1 = sha1_of(str(path))

            if not force and store.is_processed(str(path), sha1):
                summary["skipped"] += 1
                if verbose:
                    print(f"  [{i}/{n}] = skip (already ingested): {path.name}")
                continue

            if verbose:
                print(f"  [{i}/{n}] processing {path.name} ... ", end="", flush=True)
            t0 = time.time()
            if path.suffix.lower() == ".docx":
                # Internal rate emails: real tables, parsed exactly (no OCR).
                quotes = _quotes_from_docx(str(path))
            else:
                quotes = pipeline().run_file(str(path), supplier=source)
            for q in quotes:
                q.date = date        # authoritative date from the filename
                q.supplier = source  # the quotation source
            store.replace_quotes(source, date, quotes)
            store.record_file(str(path), source, date, sha1, len(quotes))

            # One folder per source, so AFS / MM / MP stay separated on disk
            # exactly as they are separated in the store.
            stem = f"{source}_{date}"
            src_csv = csv_dir / source
            src_csv.mkdir(parents=True, exist_ok=True)
            to_csv(quotes, str(src_csv / f"{stem}.csv"))
            if make_xlsx and xlsx_build is not None:
                src_xlsx = xlsx_dir / source
                src_xlsx.mkdir(parents=True, exist_ok=True)
                xlsx_build([q.as_row() for q in quotes]).save(
                    str(src_xlsx / f"{stem}.xlsx")
                )

            summary["processed"] += 1
            summary["quotes"] += len(quotes)
            if verbose:
                print(f"{len(quotes)} quotes in {time.time() - t0:.1f}s -> {stem}.csv",
                      flush=True)

    if summary["processed"] == 0 and summary["unnamed"] > 0:
        print("  ! nothing processed: filenames must be SOURCE_YYYYMMDD, "
              "e.g. rename 'AFS.png' -> 'AFS_20260716.png'")
    return summary
