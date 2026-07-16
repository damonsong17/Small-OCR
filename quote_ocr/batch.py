"""Batch-ingest a folder of dated quote images into CSV / SQLite / Excel.

Layout expected:
    inbox/<SOURCE>/<SOURCE>_<YYYYMMDD>.png    (subfolders optional)

Filenames drive the metadata: ``AFS_20260716.png`` -> source=AFS, date=2026-07-16.
Processing is incremental -- a file whose SHA-1 already appears in the store's
manifest is skipped, so re-running only picks up new or changed images.

Outputs are written into separate directories:
    <out>/csv/<SOURCE>_<DATE>.csv
    <out>/xlsx/<SOURCE>_<DATE>.xlsx
    <out>/quotes.db          (full history + manifest)
"""
from __future__ import annotations

import re
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


def _write_xlsx(rows, path: str) -> bool:
    try:
        from to_excel import build  # optional (needs openpyxl)
    except Exception:
        return False
    build(rows).save(path)
    return True


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

    pipeline = QuotePipeline(config or Config())
    summary = {"processed": 0, "skipped": 0, "unnamed": 0, "quotes": 0}

    inbox_path = Path(inbox)
    if not inbox_path.exists():
        print(f"  ! inbox folder does not exist: {inbox}")
        return {"processed": 0, "skipped": 0, "unnamed": 0, "quotes": 0}

    files = [
        p for p in sorted(inbox_path.rglob("*"))
        if p.suffix.lower() in SUPPORTED_SUFFIXES
    ]
    if verbose:
        print(f"  found {len(files)} image file(s) under {inbox}")
    if not files:
        print(f"  ! no images ({', '.join(sorted(SUPPORTED_SUFFIXES))}) found under {inbox}")

    with QuoteStore(str(out_path / "quotes.db")) as store:
        for path in files:
            parsed = parse_filename(path.stem)
            if not parsed:
                summary["unnamed"] += 1
                if verbose:
                    print(f"  ! skip (name not SOURCE_YYYYMMDD): {path.name}")
                continue
            source, date = parsed
            sha1 = sha1_of(str(path))

            if not force and store.is_processed(str(path), sha1):
                summary["skipped"] += 1
                continue

            quotes = pipeline.run_file(str(path), supplier=source)
            for q in quotes:
                q.date = date        # authoritative date from the filename
                q.supplier = source  # the quotation source
            store.replace_quotes(source, date, quotes)
            store.record_file(str(path), source, date, sha1, len(quotes))

            stem = f"{source}_{date}"
            to_csv(quotes, str(csv_dir / f"{stem}.csv"))
            if make_xlsx:
                _write_xlsx([q.as_row() for q in quotes], str(xlsx_dir / f"{stem}.xlsx"))

            summary["processed"] += 1
            summary["quotes"] += len(quotes)
            if verbose:
                print(f"  + {path.name}: {len(quotes)} quotes -> {stem}.csv")

    if summary["processed"] == 0 and summary["unnamed"] > 0:
        print("  ! nothing processed: filenames must be SOURCE_YYYYMMDD, "
              "e.g. rename 'AFS.png' -> 'AFS_20260716.png'")
    return summary
