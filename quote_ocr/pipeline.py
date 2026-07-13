"""End-to-end pipeline: image / PDF file -> structured Quote records."""
from __future__ import annotations

import os
from typing import List, Optional

from .config import Config
from .loader import load_pages
from .models import Quote
from .ocr_engine import OcrEngine, TextItem
from .parser import QuoteParser
from .table_parser import TableParser


class QuotePipeline:
    """Wires the OCR engine and parser together.

    The engine (PP-OCRv5) is loaded once and reused across files, so process a
    whole folder with a single QuotePipeline instance. The layout parser is
    chosen per page (auto), so a folder mixing matrix and section sheets works.
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.engine = OcrEngine(self.config)
        self.section_parser = QuoteParser(self.config)
        self.table_parser = TableParser(self.config)

    def _pick_parser(self, items: List[TextItem]):
        layout = self.config.layout
        if layout == "section":
            return self.section_parser
        if layout == "matrix":
            return self.table_parser
        # auto: use the matrix parser when the page has BID/OFFER column headers.
        rows = self.section_parser.group_rows(items)
        if any(TableParser._is_header_row(r) for r in rows):
            return self.table_parser
        return self.section_parser

    def run_file(self, path: str, supplier: str = "") -> List[Quote]:
        source = os.path.basename(path)
        quotes: List[Quote] = []
        for page_no, image in load_pages(path):
            items = self.engine.run(image)
            parser = self._pick_parser(items)
            quotes.extend(
                parser.parse(
                    items, source_file=source, page=page_no, supplier=supplier
                )
            )
        return quotes

    def run_files(self, paths: List[str], supplier: str = "") -> List[Quote]:
        quotes: List[Quote] = []
        for path in paths:
            quotes.extend(self.run_file(path, supplier=supplier))
        return quotes
