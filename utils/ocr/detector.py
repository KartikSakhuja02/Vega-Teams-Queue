"""
utils/ocr/detector.py
---------------------
Scoreboard table + row detection using TWO independent methods:

  Method 1 — HSV colour segmentation
    Team 1 = teal (H 60–115), Team 2 = maroon (H 0–20 or 155–180)

  Method 2 — RGB channel ratio (more robust to compression/colour-shift)
    Team 1: G channel > R channel  (teal rows)
    Team 2: R channel > G channel  (maroon rows)

Both methods look at per-row colour coverage across columns 15–85 % of
image width (avoids agent icons on the left and score icons on the right).

If either method detects valid blocks, it is used.
If both fail, the pipeline falls back to calibrated percentage coordinates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

# ── Column positions relative to TABLE WIDTH (not image width) ────────────────
#
# Calibrated from 5 real Valorant Mobile scoreboard screenshots.
# The name column starts AFTER the agent icon (~first 13 % of table width).
#
TABLE_COLS: dict[str, tuple[float, float]] = {
    "ign":     (0.13, 0.31),
    "acs":     (0.29, 0.42),
    "kda":     (0.40, 0.57),
    "dmg":     (0.55, 0.72),
    "fb":      (0.70, 0.80),
    "plants":  (0.78, 0.88),
    "defuses": (0.87, 0.97),
}

N_PLAYERS_PER_TEAM = 5

# ── HSV colour ranges (wide tolerances for compressed/colour-shifted screenshots) ─
# OpenCV HSV: H ∈ [0,180], S,V ∈ [0,255]

# Team 1 — teal/green rows (wide range catches both lighter and darker rows)
_T1_LO = np.array([60, 18, 18], dtype=np.uint8)
_T1_HI = np.array([118, 230, 230], dtype=np.uint8)

# Team 2 — maroon/red rows (hue wraps around 0°)
_T2_LO_A = np.array([0,   28, 15], dtype=np.uint8)
_T2_HI_A = np.array([20,  240, 180], dtype=np.uint8)
_T2_LO_B = np.array([155, 28, 15], dtype=np.uint8)
_T2_HI_B = np.array([180, 240, 180], dtype=np.uint8)

# Minimum fraction of sampled pixels to count a row as team-coloured
# Kept low — some rows have lighter/darker alternating shades
_COV_THRESHOLD = 0.10

# Minimum continuous block height (fraction of image height)
_MIN_BLOCK_FRAC = 0.15

# Sample x range — inner 70 % of image width avoids sidebars
_X_SAMPLE_LO = 0.15
_X_SAMPLE_HI = 0.85


@dataclass
class TableGeometry:
    """Detected scoreboard table region."""
    t1_y0: int = 0   # Team 1 block top
    t1_y1: int = 0   # Team 1 block bottom
    t2_y0: int = 0   # Team 2 block top
    t2_y1: int = 0   # Team 2 block bottom
    x0: int = 0      # Table left edge
    x1: int = 0      # Table right edge
    # 10 (y0, y1) tuples — first 5 team1, last 5 team2
    rows: list[tuple[int, int]] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return (
            self.t1_y1 > self.t1_y0 + 15
            and self.t2_y1 > self.t2_y0 + 15
            and self.t1_y0 < self.t2_y0          # t1 must be above t2
            and self.x1 > self.x0 + 80
            and len(self.rows) == 10
        )

    @property
    def table_width(self) -> int:
        return self.x1 - self.x0

    def abs_col(self, key: str) -> tuple[int, int]:
        """Absolute image x-coordinates for a column key."""
        lo, hi = TABLE_COLS[key]
        tw = self.table_width
        return (self.x0 + int(lo * tw), self.x0 + int(hi * tw))


# ── Internal helpers ───────────────────────────────────────────────────────────

def _row_coverage_hsv(hsv: np.ndarray, x0: int, x1: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-row fraction of Team 1 / Team 2 coloured pixels using HSV masks."""
    samp = hsv[:, x0:x1]
    t1  = cv2.inRange(samp, _T1_LO, _T1_HI)
    t2  = cv2.bitwise_or(
        cv2.inRange(samp, _T2_LO_A, _T2_HI_A),
        cv2.inRange(samp, _T2_LO_B, _T2_HI_B),
    )
    return np.mean(t1, axis=1) / 255.0, np.mean(t2, axis=1) / 255.0


def _row_coverage_rgb(img: np.ndarray, x0: int, x1: int) -> tuple[np.ndarray, np.ndarray]:
    """
    Alternative detection using raw BGR channel ratios.
    More robust when JPEG compression shifts HSV values.
      Team 1 (teal): G channel significantly > R channel
      Team 2 (maroon): R channel significantly > G channel
    """
    region = img[:, x0:x1].astype(np.float32)
    b, g, r = region[:, :, 0], region[:, :, 1], region[:, :, 2]  # OpenCV BGR
    mr, mg = np.mean(r, axis=1), np.mean(g, axis=1)
    brightness = np.mean(b + g + r, axis=1) / 3.0
    # Only look at moderate-brightness rows (skip white headers, pure black)
    valid = (brightness > 20) & (brightness < 200)
    diff = mg - mr
    # Team 1: G clearly > R (teal)
    cov1 = np.where(valid & (diff > 6), diff / 60.0, 0.0).clip(0, 1)
    # Team 2: R clearly > G (maroon)
    cov2 = np.where(valid & (-diff > 6), (-diff) / 60.0, 0.0).clip(0, 1)
    return cov1, cov2


def _largest_block(mask: np.ndarray, min_height: int = 1) -> Optional[tuple[int, int]]:
    """Find the longest run of True in a 1-D boolean array."""
    best: Optional[tuple[int, int]] = None
    start = -1
    for i, v in enumerate(mask):
        if v and start < 0:
            start = i
        elif not v and start >= 0:
            if (i - start) >= min_height:
                if best is None or (i - start) > (best[1] - best[0]):
                    best = (start, i)
            start = -1
    if start >= 0:
        length = len(mask) - start
        if length >= min_height:
            if best is None or length > (best[1] - best[0]):
                best = (start, len(mask))
    return best


def _split_block(y0: int, y1: int, n: int = N_PLAYERS_PER_TEAM) -> list[tuple[int, int]]:
    """
    Split [y0, y1) into n equal sub-intervals.
    Also tries to refine boundaries using a horizontal gradient
    (detects the thin divider lines between player rows).
    Returns exactly n (row_y0, row_y1) tuples.
    """
    step = (y1 - y0) / n
    return [(y0 + int(i * step), y0 + int((i + 1) * step)) for i in range(n)]


def _find_horizontal_bounds(
    t1_mask: np.ndarray,
    t2_mask: np.ndarray,
    t1_y0: int,
    t2_y1: int,
) -> tuple[int, int]:
    """Find leftmost/rightmost coloured column across both team regions."""
    combined = cv2.bitwise_or(t1_mask, t2_mask)
    col_coverage = np.mean(combined[t1_y0:t2_y1, :], axis=0) / 255.0
    cols = np.where(col_coverage > 0.06)[0]
    if len(cols) < 40:
        return 0, combined.shape[1]
    return max(0, int(cols[0]) - 3), min(combined.shape[1], int(cols[-1]) + 3)


# ── Public API ─────────────────────────────────────────────────────────────────

def _try_detect(
    img: np.ndarray,
    cov1: np.ndarray,
    cov2: np.ndarray,
    hsv: Optional[np.ndarray],
    min_block_h: int,
    method: str,
) -> Optional[TableGeometry]:
    """Shared logic: given per-row coverage arrays, find blocks and build geometry."""
    t1_block = _largest_block(cov1 > _COV_THRESHOLD, min_block_h)
    t2_block = _largest_block(cov2 > _COV_THRESHOLD, min_block_h)

    if t1_block is None or t2_block is None:
        return None

    t1_y0, t1_y1 = t1_block
    t2_y0, t2_y1 = t2_block

    if t1_y0 >= t2_y0:
        log.debug("[%s] team1 block not above team2 block", method)
        return None

    # Horizontal extent
    if hsv is not None:
        t1_full = cv2.inRange(hsv, _T1_LO, _T1_HI)
        t2_full = cv2.bitwise_or(
            cv2.inRange(hsv, _T2_LO_A, _T2_HI_A),
            cv2.inRange(hsv, _T2_LO_B, _T2_HI_B),
        )
        x0, x1 = _find_horizontal_bounds(t1_full, t2_full, t1_y0, t2_y1)
    else:
        x0, x1 = 0, img.shape[1]

    rows: list[tuple[int, int]] = (
        _split_block(t1_y0, t1_y1, N_PLAYERS_PER_TEAM)
        + _split_block(t2_y0, t2_y1, N_PLAYERS_PER_TEAM)
    )

    geom = TableGeometry(
        t1_y0=t1_y0, t1_y1=t1_y1,
        t2_y0=t2_y0, t2_y1=t2_y1,
        x0=x0, x1=x1,
        rows=rows,
    )
    if not geom.valid:
        log.debug("[%s] geometry failed validity check", method)
        return None
    return geom


def detect_table_geometry(img: np.ndarray) -> Optional[TableGeometry]:
    """
    Detect the scoreboard table in a BGR image.

    Tries two independent methods:
      1. HSV colour masks  (precise, fast)
      2. RGB channel ratio (robust to JPEG compression / colour shift)

    Returns TableGeometry or None if both fail.
    """
    if not _CV2:
        return None

    h, w = img.shape[:2]
    min_block_h = int(h * _MIN_BLOCK_FRAC)
    x_lo = int(w * _X_SAMPLE_LO)
    x_hi = int(w * _X_SAMPLE_HI)
    skip = int(h * 0.15)   # ignore score/metadata header at top

    # ── Method 1: HSV ─────────────────────────────────────────────────────────
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    cov1, cov2 = _row_coverage_hsv(hsv, x_lo, x_hi)
    cov1[:skip] = 0.0
    cov2[:skip] = 0.0

    geom = _try_detect(img, cov1, cov2, hsv, min_block_h, "HSV")
    if geom is not None:
        log.info(
            "Table detected [HSV]: t1=[%d,%d] t2=[%d,%d] x=[%d,%d] tw=%d",
            geom.t1_y0, geom.t1_y1, geom.t2_y0, geom.t2_y1,
            geom.x0, geom.x1, geom.table_width,
        )
        return geom

    log.debug("HSV detection failed — trying RGB channel ratio")

    # ── Method 2: RGB channel ratio ───────────────────────────────────────────
    cov1_r, cov2_r = _row_coverage_rgb(img, x_lo, x_hi)
    cov1_r[:skip] = 0.0
    cov2_r[:skip] = 0.0

    geom = _try_detect(img, cov1_r, cov2_r, None, min_block_h, "RGB")
    if geom is not None:
        # Refine horizontal bounds with HSV masks
        t1_full = cv2.inRange(hsv, _T1_LO, _T1_HI)
        t2_full = cv2.bitwise_or(
            cv2.inRange(hsv, _T2_LO_A, _T2_HI_A),
            cv2.inRange(hsv, _T2_LO_B, _T2_HI_B),
        )
        x0, x1 = _find_horizontal_bounds(t1_full, t2_full, geom.t1_y0, geom.t2_y1)
        if x1 > x0 + 80:
            geom.x0, geom.x1 = x0, x1
            geom.rows = (
                _split_block(geom.t1_y0, geom.t1_y1, N_PLAYERS_PER_TEAM)
                + _split_block(geom.t2_y0, geom.t2_y1, N_PLAYERS_PER_TEAM)
            )
        log.info(
            "Table detected [RGB]: t1=[%d,%d] t2=[%d,%d] x=[%d,%d] tw=%d",
            geom.t1_y0, geom.t1_y1, geom.t2_y0, geom.t2_y1,
            geom.x0, geom.x1, geom.table_width,
        )
        return geom

    log.warning("Both detection methods failed — caller will use fallback coords")
    return None
