"""Structured output record for a single quote line."""
from __future__ import annotations

from dataclasses import dataclass

# Column order for CSV / JSON output. This IS the structured schema.
#
# The first ten columns are kept in their original order for backward
# compatibility with earlier outputs; the richer matrix fields (segment,
# benchmark, benchmark_rate) are appended at the end so positional consumers of
# the original columns keep working. Any field may be blank for a given row.
FIELDNAMES = [
    "date",           # quote sheet date (page-level), e.g. 2026-07-13
    "supplier",       # broker / counterparty (may be blank)
    "currency",       # e.g. USD, EUR, CNH, HKD, or USD/CNY
    "tenor",          # e.g. O/N, 1W, 2W, 1S, 6S, 1Y
    "bid",            # bid price (blank for offer-only sheets)
    "offer",          # offer (ask) price (blank for bid-only sheets)
    "source_file",    # originating image or pdf file name
    "page",           # 1-based page index (relevant for multi-page PDFs)
    "confidence",     # min OCR confidence of the tokens used for this row
    "raw",            # raw reconstructed row text (for auditing / debugging)
    # --- appended, additive fields ---
    "segment",        # market/desk grouping, e.g. Chinese, Korean, ISLAMIC
    "benchmark",      # reference index for the row, e.g. SOFR, EURIBOR, CNH HIBOR
    "benchmark_rate", # the index value, when present
    "mid",            # a SINGLE reference rate, when the source quotes one
                      # number instead of a two-way price. Kept apart from
                      # bid/offer so it can never be read as an executable price.
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
    mid: str = ""
    source_file: str = ""
    page: int = 1
    confidence: float = 0.0
    raw: str = ""

    def as_row(self) -> dict:
        return {name: getattr(self, name) for name in FIELDNAMES}
