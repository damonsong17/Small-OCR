"""Thin wrapper around RapidOCR (PP-OCRv5 models) that returns positioned text.

Isolating the OCR engine here means the parser never depends on RapidOCR's exact
output shape, and the engine can be swapped (e.g. for a mock in tests).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np

from .config import Config


@dataclass
class TextItem:
    """A single detected text box with its polygon and confidence."""

    text: str
    box: np.ndarray  # shape (4, 2): four (x, y) corners
    score: float

    @property
    def cx(self) -> float:
        return float(self.box[:, 0].mean())

    @property
    def cy(self) -> float:
        return float(self.box[:, 1].mean())

    @property
    def x0(self) -> float:
        return float(self.box[:, 0].min())

    @property
    def height(self) -> float:
        return float(self.box[:, 1].max() - self.box[:, 1].min())


class OcrEngine:
    """Runs PP-OCRv5 via RapidOCR and yields TextItem objects."""

    def __init__(self, config: Config):
        # Imported lazily so that importing the package does not require the
        # (heavier) OCR dependencies until you actually run inference.
        from rapidocr import (
            EngineType,
            LangDet,
            LangRec,
            ModelType,
            OCRVersion,
            RapidOCR,
        )

        ocr_version = {
            "PP-OCRv4": OCRVersion.PPOCRV4,
            "PP-OCRv5": OCRVersion.PPOCRV5,
            "PP-OCRv6": OCRVersion.PPOCRV6,
        }[config.ocr_version]
        model_type = {
            "mobile": ModelType.MOBILE,
            "server": ModelType.SERVER,
            "small": ModelType.SMALL,
            "tiny": ModelType.TINY,
            "medium": ModelType.MEDIUM,
        }[config.model_type]
        engine_type = {
            "onnxruntime": EngineType.ONNXRUNTIME,
            "openvino": EngineType.OPENVINO,
        }[config.engine]
        det_lang = {"ch": LangDet.CH, "en": LangDet.EN, "multi": LangDet.MULTI}[
            config.det_lang
        ]
        rec_lang = {"ch": LangRec.CH, "en": LangRec.EN}.get(
            config.rec_lang, LangRec.CH
        )

        params = {
            "Det.engine_type": engine_type,
            "Rec.engine_type": engine_type,
            "Cls.engine_type": engine_type,
            "Det.ocr_version": ocr_version,
            "Rec.ocr_version": ocr_version,
            "Det.model_type": model_type,
            "Rec.model_type": model_type,
            "Det.lang_type": det_lang,
            "Rec.lang_type": rec_lang,
            "Global.text_score": config.text_score,
        }
        self._engine = RapidOCR(params=params)

    def run(self, image: np.ndarray) -> List[TextItem]:
        result = self._engine(image)
        if result is None or result.boxes is None:
            return []
        items: List[TextItem] = []
        for box, text, score in zip(result.boxes, result.txts, result.scores):
            items.append(
                TextItem(
                    text=str(text),
                    box=np.asarray(box, dtype=float),
                    score=float(score),
                )
            )
        return items
