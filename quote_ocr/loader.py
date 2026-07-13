"""Load input files into page images.

Images work out of the box. PDF support is enabled automatically when PyMuPDF
(``pip install pymupdf``) is present, so the pipeline is "extensible to PDF"
with no code change -- just install the optional dependency.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import numpy as np
from PIL import Image

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES


def load_pages(path: str, pdf_dpi: int = 200) -> List[Tuple[int, np.ndarray]]:
    """Return a list of (page_number, RGB ndarray) for an image or PDF file."""
    p = Path(path)
    suffix = p.suffix.lower()

    if suffix in IMAGE_SUFFIXES:
        img = Image.open(p).convert("RGB")
        return [(1, np.asarray(img))]

    if suffix in PDF_SUFFIXES:
        return _load_pdf(p, pdf_dpi)

    raise ValueError(
        f"Unsupported file type '{suffix}'. Supported: "
        f"{', '.join(sorted(SUPPORTED_SUFFIXES))}"
    )


def _load_pdf(path: Path, dpi: int) -> List[Tuple[int, np.ndarray]]:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - depends on optional dep
        raise ImportError(
            "PDF input requires PyMuPDF. Install it with:  pip install pymupdf"
        ) from exc

    pages: List[Tuple[int, np.ndarray]] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append((i, np.asarray(img)))
    return pages


def collect_inputs(paths: List[str]) -> List[str]:
    """Expand files and directories into a flat, sorted list of input files."""
    out: List[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(
                str(f)
                for f in sorted(p.rglob("*"))
                if f.suffix.lower() in SUPPORTED_SUFFIXES
            )
        elif p.suffix.lower() in SUPPORTED_SUFFIXES:
            out.append(str(p))
    return out
