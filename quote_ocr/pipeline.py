"""End-to-end pipeline: image / PDF file -> structured Quote records."""
from __future__ import annotations

import os
from typing import List, Optional

from .config import Config
from .loader import load_pages
from .models import Quote
from .ocr_engine import OcrEngine
from .parser import QuoteParser


class QuotePipeline:
    """Wires the OCR engine and parser together.

    The engine (PP-OCRv5) is loaded once and reused across files, so process a
    whole folder with a single QuotePipeline instance.
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.engine = OcrEngine(self.config)
        self.parser = QuoteParser(self.config)

    def run_file(self, path: str, supplier: str = "") -> List[Quote]:
        source = os.path.basename(path)
        quotes: List[Quote] = []
        for page_no, image in load_pages(path):
            items = self.engine.run(image)
            quotes.extend(
                self.parser.parse(
                    items, source_file=source, page=page_no, supplier=supplier
                )
            )
        return quotes

    def run_files(self, paths: List[str], supplier: str = "") -> List[Quote]:
        quotes: List[Quote] = []
        for path in paths:
            quotes.extend(self.run_file(path, supplier=supplier))
        return quotes
