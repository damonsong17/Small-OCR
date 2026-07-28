"""Offline treasury dashboard — Excel in, monitored metrics out.

Runs entirely on the desk's own machine: the Python standard library plus
``openpyxl`` (already a dependency of the OCR pipeline). No web framework, no
CDN, no map tile server, no telemetry.

    from treasury import Book, load_sources
    book = load_sources("data/treasury")   # a folder of .xlsx / .csv
    book.metric("lcr")

See ``TREASURY_DASHBOARD.md``.
"""
from __future__ import annotations

from .book import Book, load_sources
from .config import Settings

__all__ = ["Book", "load_sources", "Settings"]
