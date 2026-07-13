"""Structured output record for a single quote line."""
from __future__ import annotations

from dataclasses import dataclass

# Column order for CSV / JSON output. This IS the structured schema.
FIELDNAMES = [
    "date",         # quote sheet date (page-level), e.g. 2026-07-13
    "supplier",     # broker / counterparty (may be blank)
    "currency",     # e.g. USD/CNY or USD
    "tenor",        # e.g. O/N, 1W, 1M, 3M, 1Y
    "bid",          # left / bid price
    "offer",        # right / offer (ask) price
    "source_file",  # originating image or pdf file name
    "page",         # 1-based page index (relevant for multi-page PDFs)
    "confidence",   # min OCR confidence of the tokens used for this row
    "raw",          # raw reconstructed row text (for auditing / debugging)
]


@dataclass
class Quote:
    date: str = ""
    supplier: str = ""
    currency: str = ""
    tenor: str = ""
    bid: str = ""
    offer: str = ""
    source_file: str = ""
    page: int = 1
    confidence: float = 0.0
    raw: str = ""

    def as_row(self) -> dict:
        return {name: getattr(self, name) for name in FIELDNAMES}
