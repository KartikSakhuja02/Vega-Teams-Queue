"""
ocr-server/preprocessing.py
-----------------------------
Image preprocessing before the vision model:
  1. Decode bytes → numpy BGR
  2. Validate (size, channels)
  3. Attempt to detect + crop the scoreboard region
  4. Resize to model-friendly dimensions (preserving aspect ratio)
  5. Convert to PIL RGB for the model processor

Key design principle: do not over-process.
The VLM handles noise/artifacts well — only crop and resize, don't threshold.
"""
from __future__ import annotations

import logging
from typing import Optional

import cv2
import numpy as np
from PIL import Image

from config import CFG

log = logging.getLogger(__name__)

# ── HSV ranges for scoreboard row detection (from local OCR work) ─────────────
# Team 1 — teal/green rows
_T1_LO = np.array([60,  18, 18], dtype=np.uint8)
_T1_HI = np.array([118, 230, 230], dtype=np.uint8)
# Team 2 — maroon/red rows (hue wraps at 0/180)
_T2_LO_A = np.array([0,   28, 15], dtype=np.uint8)
_T2_HI_A = np.array([20,  240, 180], dtype=np.uint8)
_T2_LO_B = np.array([155, 28, 15], dtype=np.uint8)
_T2_HI_B = np.array([180, 240, 180], dtype=np.uint8)

_COV_MIN = 0.10   # min fraction of pixels to count a row as coloured
_MIN_BLOCK_PX = 60  # minimum height of a team block in pixels


class PreprocessingError(ValueError):
    """Raised when an image cannot be processed."""


def decode_image(image_bytes: bytes) -> np.ndarray:
    """Decode raw bytes (PNG/JPEG/WebP) to BGR numpy array."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise PreprocessingError("Could not decode image (unsupported format or corrupted data)")
    h, w = img.shape[:2]
    if h < CFG.min_image_px or w < CFG.min_image_px:
        raise PreprocessingError(
            f"Image too small: {w}×{h}. Minimum: {CFG.min_image_px}px on each side."
        )
    if len(img.shape) != 3 or img.shape[2] != 3:
        raise PreprocessingError("Image must be a colour (3-channel) image")
    return img


def _row_coverage(img_bgr: np.ndarray, x_lo: int, x_hi: int):
    """Per-row colour coverage using both HSV masks and RGB channel ratio."""
    h, w = img_bgr.shape[:2]
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    samp_hsv = hsv[:, x_lo:x_hi]

    t1_mask = cv2.inRange(samp_hsv, _T1_LO, _T1_HI)
    t2_mask = cv2.bitwise_or(
        cv2.inRange(samp_hsv, _T2_LO_A, _T2_HI_A),
        cv2.inRange(samp_hsv, _T2_LO_B, _T2_HI_B),
    )
    c1_hsv = np.mean(t1_mask, axis=1) / 255.0
    c2_hsv = np.mean(t2_mask, axis=1) / 255.0

    # RGB ratio method as supplement
    samp_bgr = img_bgr[:, x_lo:x_hi].astype(np.float32)
    mg = np.mean(samp_bgr[:, :, 1], axis=1)   # Green channel
    mr = np.mean(samp_bgr[:, :, 2], axis=1)   # Red channel
    brightness = (mg + mr) / 2
    valid = (brightness > 20) & (brightness < 200)
    diff = mg - mr
    c1_rgb = np.where(valid & (diff > 6), np.clip(diff / 60.0, 0, 1), 0.0)
    c2_rgb = np.where(valid & (-diff > 6), np.clip(-diff / 60.0, 0, 1), 0.0)

    # Take max of both methods
    return np.maximum(c1_hsv, c1_rgb), np.maximum(c2_hsv, c2_rgb)


def _largest_contiguous_block(mask_1d: np.ndarray, min_h: int = 1):
    """Find the longest run of True values; returns (start, end) or None."""
    best = None
    start = -1
    for i, v in enumerate(mask_1d):
        if v and start < 0:
            start = i
        elif not v and start >= 0:
            if (i - start) >= min_h and (best is None or (i - start) > (best[1] - best[0])):
                best = (start, i)
            start = -1
    if start >= 0:
        length = len(mask_1d) - start
        if length >= min_h and (best is None or length > (best[1] - best[0])):
            best = (start, len(mask_1d))
    return best


def detect_scoreboard_crop(img_bgr: np.ndarray) -> Optional[np.ndarray]:
    """
    Attempt to detect and crop the scoreboard table region.
    Returns the cropped BGR image, or None if detection fails.

    We look for two contiguous colour bands (Team 1 teal, Team 2 maroon).
    If either band is not found, we fall back to the full image.
    """
    h, w = img_bgr.shape[:2]
    skip = int(h * 0.15)           # skip score/header at top

    x_lo, x_hi = int(w * 0.15), int(w * 0.85)
    cov1, cov2 = _row_coverage(img_bgr, x_lo, x_hi)
    cov1[:skip] = 0.0
    cov2[:skip] = 0.0

    b1 = _largest_contiguous_block(cov1 > _COV_MIN, _MIN_BLOCK_PX)
    b2 = _largest_contiguous_block(cov2 > _COV_MIN, _MIN_BLOCK_PX)

    if b1 is None or b2 is None or b1[0] >= b2[0]:
        log.debug("Scoreboard crop detection failed — using full image")
        return None

    # Generous padding: include header row above team1 and footer below team2
    pad_top    = max(0, b1[0] - int(h * 0.12))   # header row
    pad_bottom = min(h, b2[1] + int(h * 0.02))   # tiny footer
    crop = img_bgr[pad_top:pad_bottom, :]
    log.info("Scoreboard crop: y=[%d,%d] → (%d×%d)", pad_top, pad_bottom, crop.shape[1], crop.shape[0])
    return crop


def resize_for_model(img_bgr: np.ndarray, max_px: int) -> np.ndarray:
    """
    Resize image so the longest side ≤ max_px, preserving aspect ratio.
    Uses LANCZOS for downscaling (best quality).
    Does NOT upscale (the VLM handles small images fine).
    """
    h, w = img_bgr.shape[:2]
    if max(h, w) <= max_px:
        return img_bgr
    scale = max_px / max(h, w)
    new_w, new_h = int(w * scale), int(h * scale)
    return cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)


def prepare_image(image_bytes: bytes) -> tuple[np.ndarray, Image.Image]:
    """
    Full preprocessing pipeline.

    Returns:
        (preprocessed_bgr, pil_rgb) — the BGR crop for debug saving,
        and the PIL RGB image to feed to the model processor.
    """
    img = decode_image(image_bytes)

    if CFG.crop_scoreboard:
        crop = detect_scoreboard_crop(img)
        img = crop if crop is not None else img

    img = resize_for_model(img, CFG.max_image_px)

    # Convert BGR → RGB → PIL for the model
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    return img, pil
