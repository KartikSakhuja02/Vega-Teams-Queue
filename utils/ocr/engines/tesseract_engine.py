"""
utils/ocr/engines/tesseract_engine.py
---------------------------------------
Tesseract wrapper with multi-variant consensus OCR.

Usage:
    from utils.ocr.engines.tesseract_engine import (
        ocr_int_consensus,
        ocr_kda_consensus,
        ocr_ign,
        ocr_score_region,
        ocr_meta_region,
    )
"""
from __future__ import annotations

import logging
import re
from typing import Callable

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

try:
    import pytesseract
    pytesseract.get_tesseract_version()
    _TESS_OK = True
except Exception:
    _TESS_OK = False
    log.warning("Tesseract not found. Install: sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim")

# ── Tesseract config strings ───────────────────────────────────────────────────
_PSM8_DIGITS = "--oem 3 --psm 8 -c tessedit_char_whitelist=0123456789"
_PSM7_KDA    = "--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789/|lI"
_PSM7_TEXT   = "--oem 3 --psm 7"
_PSM6_BLOCK  = "--oem 3 --psm 6"


def _tess(img_gray: np.ndarray, config: str, lang: str = "eng") -> str:
    """Run Tesseract on a preprocessed (already grayscale/binary) array."""
    if not _TESS_OK:
        return ""
    try:
        return pytesseract.image_to_string(
            Image.fromarray(img_gray), config=config, lang=lang
        ).strip()
    except Exception as e:
        log.debug("Tesseract error: %s", e)
        return ""


# ── Multi-variant OCR ──────────────────────────────────────────────────────────

def ocr_int_consensus(
    crop: np.ndarray,
    variants: list[Callable[[np.ndarray], np.ndarray]],
) -> list[str]:
    """
    Run Tesseract (PSM 8, digits only) on each preprocessed variant.
    Returns list of raw OCR strings (one per variant).
    """
    results = []
    for fn in variants:
        proc = fn(crop)
        raw  = _tess(proc, _PSM8_DIGITS)
        results.append(raw)
    return results


def ocr_kda_consensus(
    crop: np.ndarray,
    variants: list[Callable[[np.ndarray], np.ndarray]],
) -> list[str]:
    """Run Tesseract (PSM 7, KDA chars) on each variant."""
    results = []
    for fn in variants:
        proc = fn(crop)
        raw  = _tess(proc, _PSM7_KDA)
        results.append(raw)
    return results


def ocr_ign(
    crop: np.ndarray,
    variants: list[Callable[[np.ndarray], np.ndarray]],
    lang: str = "chi_sim+eng",
) -> str:
    """
    OCR player name.
    Tries each variant and returns the longest plausible result
    (longer usually means the OCR captured more of the name).
    Tesseract PSM 7 = single text line.
    """
    candidates: list[str] = []
    for fn in variants:
        proc = fn(crop)
        raw  = _tess(proc, _PSM7_TEXT, lang=lang).strip()
        if raw:
            candidates.append(raw)

    if not candidates:
        return ""

    # Prefer the candidate with the most alphanumeric content
    def score(s: str) -> int:
        return sum(1 for c in s if c.isalnum() or '\u4e00' <= c <= '\u9fff')

    return max(candidates, key=score)


def ocr_score_region(crop: np.ndarray) -> str:
    """OCR the 'N 获胜 M' score header. Returns raw string."""
    import cv2
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
    gray = cv2.resize(gray, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bw = cv2.copyMakeBorder(bw, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)
    for lang in ("chi_sim+eng", "eng"):
        txt = _tess(bw, _PSM6_BLOCK, lang=lang)
        if txt:
            return txt
    return ""


def ocr_meta_region(crop: np.ndarray) -> str:
    """OCR the metadata block (map name, date, duration)."""
    import cv2
    from PIL import Image as _Img
    pil = _Img.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
    return pytesseract.image_to_string(pil, config=_PSM6_BLOCK, lang="chi_sim+eng").strip() if _TESS_OK else ""


def is_available() -> bool:
    return _TESS_OK
