"""End-to-end pipeline: image / PDF file -> structured Quote records."""
from __future__ import annotations

import os
from typing import List, Optional

import numpy as np

from .config import Config
from .loader import load_pages
from .models import Quote
from .ocr_engine import OcrEngine, TextItem
from .parser import QuoteParser
from .preprocess import enhance, is_active
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

    def _parse_page(self, items: List[TextItem], **kw) -> List[Quote]:
        layout = self.config.layout
        if layout == "section":
            return self.section_parser.parse(items, **kw)
        if layout == "matrix":
            return self.table_parser.parse(items, **kw)

        # auto: prefer the matrix parser when the page has BID/OFFER column
        # headers; fall back to the section parser if it yields nothing (e.g. a
        # simple narrow sheet with no column headers, or an unusual structure).
        rows = self.section_parser.group_rows(items)
        if any(TableParser._is_header_row(r) for r in rows):
            quotes = self.table_parser.parse(items, **kw)
            if quotes:
                return quotes
        return self.section_parser.parse(items, **kw)

    def _ocr(self, image: np.ndarray) -> List[TextItem]:
        upscale = self.config.upscale
        tw = self.config.target_width
        if tw and image.shape[1] < tw:
            upscale = max(upscale, tw / image.shape[1])
        if is_active(self.config) or upscale != 1.0:
            image = enhance(
                image,
                upscale=upscale,
                grayscale=self.config.grayscale,
                sharpen=self.config.sharpen,
                contrast=self.config.contrast,
            )
        return self.engine.run(image)

    def run_file(self, path: str, supplier: str = "") -> List[Quote]:
        source = os.path.basename(path)
        quotes: List[Quote] = []
        for page_no, image in load_pages(path):
            items = self._ocr(image)
            quotes.extend(
                self._parse_page(
                    items, source_file=source, page=page_no, supplier=supplier
                )
            )
        return quotes

    def run_files(self, paths: List[str], supplier: str = "") -> List[Quote]:
        quotes: List[Quote] = []
        for path in paths:
            quotes.extend(self.run_file(path, supplier=supplier))
        return quotes

    def dump_ocr(self, path: str) -> str:
        """Return a human-readable dump of what OCR detected, row by row.

        This is the diagnostic to tell an OCR limitation (a tenor line simply
        not detected/recognised) from a parser issue (line detected but dropped).
        """
        lines: List[str] = []
        for page_no, image in load_pages(path):
            items = self._ocr(image)
            rows = self.section_parser.group_rows(items)
            lines.append(f"# {os.path.basename(path)}  page {page_no}  "
                         f"({len(items)} text boxes, {len(rows)} rows)")
            for r, row in enumerate(rows):
                y = int(sum(it.cy for it in row) / len(row))
                cells = " | ".join(
                    f"{it.text}" for it in sorted(row, key=lambda x: x.cx)
                )
                lines.append(f"  row {r:>2} y={y:>5}: {cells}")
            lines.append("")
        return "\n".join(lines)
