"""
utils/ocr/preprocessor.py
--------------------------
Multiple preprocessing pipelines for different cell types.

Each variant returns a grayscale image (ready for Tesseract) with:
  - black text on white background (Tesseract's preferred format)
  - upscaled to ensure text height ≥ 30 px

Variants are named A–D. For numeric cells we run A, B, C and vote.
For text cells we use the single best variant (D).
"""
from __future__ import annotations

import cv2
import numpy as np


def _ensure_min_height(gray: np.ndarray, min_h: int = 40) -> np.ndarray:
    h, w = gray.shape[:2]
    if h < min_h:
        scale = min_h / h
        gray = cv2.resize(gray, (int(w * scale), min_h), interpolation=cv2.INTER_CUBIC)
    return gray


def _pad(img: np.ndarray, px: int = 10) -> np.ndarray:
    return cv2.copyMakeBorder(img, px, px, px, px, cv2.BORDER_CONSTANT, value=255)


def _to_gray(crop: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop


def variant_a(crop: np.ndarray) -> np.ndarray:
    """
    A: grayscale → 3× upscale → Otsu binarise → invert if needed.
    Works best for high-contrast digit cells.
    """
    gray = _to_gray(crop)
    gray = cv2.resize(gray, (0, 0), fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(bw) < 127:   # mostly black → text is white-on-black, invert
        bw = cv2.bitwise_not(bw)
    return _pad(bw)


def variant_b(crop: np.ndarray) -> np.ndarray:
    """
    B: grayscale → CLAHE → 2× upscale → adaptive threshold.
    Better for cells with uneven illumination or subtle colour gradients.
    """
    gray = _to_gray(crop)
    gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4)).apply(gray)
    gray = cv2.resize(gray, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    bw = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 15, 4
    )
    return _pad(bw)


def variant_c(crop: np.ndarray) -> np.ndarray:
    """
    C: grayscale → Gaussian blur → 2× upscale → Otsu.
    Reduces noise speckles before thresholding.
    """
    gray = _to_gray(crop)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    gray = cv2.resize(gray, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(bw) < 127:
        bw = cv2.bitwise_not(bw)
    return _pad(bw)


def variant_d(crop: np.ndarray) -> np.ndarray:
    """
    D: grayscale → 2× upscale → BINARY_INV at 180.
    Valorant text is bright white — this directly isolates it.
    Best for clearly-lit digit cells and name cells.
    """
    gray = _to_gray(crop)
    gray = cv2.resize(gray, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)
    # Fallback: if result is nearly empty, try Otsu
    white_frac = cv2.countNonZero(bw) / bw.size
    if white_frac < 0.02 or white_frac > 0.90:
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if np.mean(bw) < 127:
            bw = cv2.bitwise_not(bw)
    return _pad(bw)


def variant_e(crop: np.ndarray) -> np.ndarray:
    """
    E: grayscale → unsharp mask → 2× upscale → BINARY_INV at 170.
    Sharpening helps when the screenshot is slightly blurry or compressed.
    """
    gray = _to_gray(crop)
    blur = cv2.GaussianBlur(gray, (0, 0), 1.5)
    gray = cv2.addWeighted(gray, 1.5, blur, -0.5, 0)
    gray = cv2.resize(gray, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 170, 255, cv2.THRESH_BINARY_INV)
    white_frac = cv2.countNonZero(bw) / bw.size
    if white_frac < 0.02 or white_frac > 0.90:
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if np.mean(bw) < 127:
            bw = cv2.bitwise_not(bw)
    return _pad(bw)


# Convenience bundles ──────────────────────────────────────────────────────────

DIGIT_VARIANTS = [variant_a, variant_c, variant_d]   # 3-way consensus for numbers
TEXT_VARIANTS  = [variant_d, variant_b]               # 2-way for names
KDA_VARIANTS   = [variant_a, variant_d, variant_e]    # slash is a detail — need sharpness
