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


def _quotes_from_docx(path: str, single_side: str = "offer"):
    """Turn a .docx rate email into Quote rows for the store.

    ``single_side`` is which side a one-number-per-currency table is; the MM
    money-market email quotes the offer, so that is the default.
    """
    from .docx_reader import parse_docx
    from .models import Quote
    surf, meta = parse_docx(path, verbose=False, single_side=single_side)
    one_sided = {c.upper() for c in meta.get("one_sided", [])}
    side = meta.get("single_side", single_side)
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
                    + (f" {side}-only" if ccy in one_sided else "")))
    return out


def _quotes_from_text(path: str):
    """Turn a .txt / .csv / .md quote file into Quote rows for the store.

    Text channels (a broker's list, a chat scrape saved to a file, a PDF
    converted to Markdown) reach the database through the SAME schema as OCR and
    .docx sources, so a query does not need to know where a rate came from.
    """
    from .models import Quote
    from .sources import surface_from_text

    surf = surface_from_text(path)
    out = []

    def _pct(v):
        return "" if v is None else f"{v*100:.6f}".rstrip("0").rstrip(".")

    for ccy, sides in surf.items():
        for t in sorted(set(sides.get("bid", {})) | set(sides.get("offer", {}))
                        | set(sides.get("mid", {}))):
            out.append(Quote(
                currency=ccy, tenor=t,
                bid=_pct(sides.get("bid", {}).get(t)),
                offer=_pct(sides.get("offer", {}).get(t)),
                mid=_pct(sides.get("mid", {}).get(t)),
                segment="", source_file=Path(path).name, page=1,
                confidence=1.0, raw=f"text {Path(path).suffix.lstrip('.')}"))
    return out


# Text formats that carry quotes directly -- no OCR involved.
TEXT_SUFFIXES = {".txt", ".csv", ".md"}


def _under(path: Path, folder: Path) -> bool:
    try:
        return folder in path.resolve().parents
    except OSError:                                   # pragma: no cover
        return False


def _is_our_own_csv(path: Path) -> bool:
    """True for a CSV this pipeline wrote (matched on its header signature).

    Re-ingesting our own output silently degrades it: the long format has no
    benchmark or segment column, so a rich matrix extraction comes back as a
    flat bid/offer list and REPLACES the good rows for that source and date.
    """
    if path.suffix.lower() != ".csv":
        return False
    try:
        with open(path, encoding="utf-8-sig") as f:
            head = f.readline()
    except OSError:                                   # pragma: no cover
        return False
    cols = {c.strip().lower() for c in head.split(",")}
    return {"source_file", "confidence", "raw"} <= cols


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

    known = SUPPORTED_SUFFIXES | TEXT_SUFFIXES | {".docx"}
    everything = [p for p in sorted(inbox_path.rglob("*")) if p.is_file()]

    # The output folder is very often INSIDE the inbox ("ingest.py data --out
    # data\output"). Since .csv became an input type, our own long-format CSV
    # would be re-ingested as if it were a quote source -- and because it has no
    # benchmark or segment column, it would REPLACE a good extraction with a
    # flattened one. Never read anything we wrote.
    out_abs = out_path.resolve()
    from_output = [p for p in everything if _under(p, out_abs)]
    everything = [p for p in everything if not _under(p, out_abs)]
    ours = [p for p in everything if _is_our_own_csv(p)]
    everything = [p for p in everything if p not in ours]

    files = [p for p in everything if p.suffix.lower() in known]
    # A file we do not handle is REPORTED, never silently passed over: dropping
    # a quote source without a word is how a whole channel goes missing.
    ignored = [p for p in everything
               if p.suffix.lower() not in known and not p.name.startswith("~$")]
    if verbose:
        print(f"  found {len(files)} quote file(s) under {inbox}")
        if from_output:
            print(f"  (skipped {len(from_output)} file(s) inside the output "
                  f"folder {out_path} -- never re-reading what we wrote)")
        if ours:
            print(f"  (skipped {len(ours)} file(s) that are this pipeline's own "
                  f"output CSV: {', '.join(p.name for p in ours[:3])}"
                  + (" ..." if len(ours) > 3 else "") + ")")
        if ignored:
            exts = sorted({p.suffix.lower() or "(no extension)" for p in ignored})
            print(f"  ! ignored {len(ignored)} file(s) with unhandled type "
                  f"{', '.join(exts)}: {', '.join(p.name for p in ignored[:5])}"
                  + (" ..." if len(ignored) > 5 else ""))
            print(f"    handled types: {', '.join(sorted(known))}")
    if not files:
        print(f"  ! no quote files ({', '.join(sorted(known))}) found under {inbox}")

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
            elif path.suffix.lower() in TEXT_SUFFIXES:
                # Broker lists, chat scrapes, PDF-to-Markdown: already text.
                quotes = _quotes_from_text(str(path))
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
