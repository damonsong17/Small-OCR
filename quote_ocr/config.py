"""Configuration for the quote OCR pipeline.

Everything tunable lives here so you can adapt the pipeline to your own quote
sheets without touching the parsing logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


# Currencies we recognise in headers / rows. Extend freely for your desk.
DEFAULT_CURRENCIES: List[str] = [
    "USD", "EUR", "JPY", "GBP", "CHF", "AUD", "NZD", "CAD",
    "CNY", "CNH", "HKD", "SGD", "TWD", "KRW", "INR", "THB",
    "SEK", "NOK", "DKK", "ZAR", "MXN", "BRL", "RUB", "TRY",
    "PLN", "HUF", "CZK", "ILS", "AED", "SAR", "XAU", "XAG",
]


@dataclass
class Config:
    # --- OCR model selection (RapidOCR / PP-OCR family) ---
    ocr_version: str = "PP-OCRv5"          # PP-OCRv5 (requested) | PP-OCRv4 | PP-OCRv6
    model_type: str = "mobile"             # mobile (fast, iGPU-friendly) | server (more accurate)
    engine: str = "onnxruntime"            # onnxruntime | openvino (Intel-accelerated)
    det_lang: str = "ch"                   # ch covers CN+EN; en for latin-only
    rec_lang: str = "ch"                   # ch covers CN+EN
    text_score: float = 0.5                # drop OCR results below this confidence

    # --- Table row reconstruction ---
    # Two text boxes belong to the same row if their vertical centres are within
    # this fraction of the median text height.
    row_y_tolerance: float = 0.6

    # --- Field extraction ---
    currencies: List[str] = field(default_factory=lambda: list(DEFAULT_CURRENCIES))
    supplier: str = ""                     # override; blank => best-effort auto-detect
    default_currency: str = ""             # fall back when a row has no currency context

    # A data row must contain a tenor AND at least this many price numbers to be
    # emitted as a quote.
    min_prices_for_quote: int = 1
