"""Structured output record for a single quote line."""
from __future__ import annotations

from dataclasses import dataclass

# Column order for CSV / JSON output. This IS the structured schema.
FIELDNAMES = [
    "date",           # quote sheet date (page-level), e.g. 2026-07-13
    "supplier",       # broker / counterparty (may be blank)
    "segment",        # market/desk grouping, e.g. Chinese, Korean, ISLAMIC (may be blank)
    "currency",       # e.g. USD, EUR, CNH, HKD, or USD/CNY
    "benchmark",      # reference index for the row, e.g. SOFR, EURIBOR, CNH HIBOR
    "benchmark_rate", # the index value, when present
    "tenor",          # e.g. O/N, 1W, 2W, 1S, 6S, 1Y
    "bid",            # bid price
    "offer",          # offer (ask) price
    "source_file",    # originating image or pdf file name
    "page",           # 1-based page index (relevant for multi-page PDFs)
    "confidence",     # min OCR confidence of the tokens used for this row
    "raw",            # raw reconstructed row text (for auditing / debugging)
]


@dataclass
class Quote:
    date: str = ""
    supplier: str = ""
    segment: str = ""
    currency: str = ""
    benchmark: str = ""
    benchmark_rate: str = ""
    tenor: str = ""
    bid: str = ""
    offer: str = ""
    source_file: str = ""
    page: int = 1
    confidence: float = 0.0
    raw: str = ""

    def as_row(self) -> dict:
        return {name: getattr(self, name) for name in FIELDNAMES}
