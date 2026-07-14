"""Image preprocessing to improve OCR on small / dense quote tables.

Dense sheets shrink the tenor text so much that the recognizer misreads the
unit letter (e.g. '1s' -> '15'), which then no longer looks like a tenor and is
dropped. Upscaling and sharpening before OCR makes those glyphs legible again.
Coordinates scale uniformly, so the downstream column logic is unaffected.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps


def enhance(
    img: np.ndarray,
    upscale: float = 1.0,
    grayscale: bool = False,
    sharpen: bool = False,
    contrast: float = 1.0,
) -> np.ndarray:
    """Return an enhanced RGB image array for OCR."""
    out = Image.fromarray(img)

    if grayscale:
        out = ImageOps.grayscale(out).convert("RGB")
    if upscale and upscale != 1.0:
        w, h = out.size
        out = out.resize((max(1, int(w * upscale)), max(1, int(h * upscale))), Image.LANCZOS)
    if contrast and contrast != 1.0:
        out = ImageEnhance.Contrast(out).enhance(contrast)
    if sharpen:
        out = out.filter(ImageFilter.SHARPEN)

    return np.asarray(out.convert("RGB"))


def is_active(config) -> bool:
    """Whether any enhancement is configured."""
    return bool(
        (config.upscale and config.upscale != 1.0)
        or config.grayscale
        or config.sharpen
        or (config.contrast and config.contrast != 1.0)
    )
