"""Small OCR pipeline: quote-sheet images -> structured bid/offer records.

Uses PP-OCRv5 (via RapidOCR) for text detection + recognition, then a
rule-based parser to extract currency, tenor, bid/offer, date and supplier.
"""
from .config import Config
from .models import FIELDNAMES, Quote
from .pipeline import QuotePipeline

__all__ = ["Config", "Quote", "FIELDNAMES", "QuotePipeline"]
__version__ = "0.1.0"
