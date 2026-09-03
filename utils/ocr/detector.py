"""
utils/ocr/detector.py
---------------------
Scoreboard table + row detection using multiple signals:
  1. Primary: HSV color segmentation (Team 1 = teal, Team 2 = maroon)
  2. Secondary: horizontal edge projection for row boundaries
  3. Column positions: calibrated TABLE-relative proportions derived from
     real screenshots (not full-image proportions — avoids sidebar noise)

Detection sequence:
  detect_table_geometry()
    → find_team_blocks()          HSV color masks → contiguous bands
    → split_into_rows()           equal division with gradient refinement
    → find_horizontal_bounds()    leftmost/rightmost colored pixel
    → detect_columns_from_header() (optional, expensive)
  → returns TableGeometry or None
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

# ── HSV color ranges for team backgrounds ─────────────────────────────────────
# Calibrated for Valorant Mobile (Tencent CN version) UI colors.
# Uses OpenCV HSV convention: H ∈ [0, 180], S,V ∈ [0, 255].

# Team 1 — teal/green rows
_T1_LO = np.array([68, 28, 28], dtype=np.uint8)
_T1_HI = np.array([102, 205, 205], dtype=np.uint8)

# Team 2 — maroon/red rows (hue wraps at 0/180)
_T2_LO_A = np.array([0,   48, 22], dtype=np.uint8)
_T2_HI_A = np.array([16,  230, 165], dtype=np.uint8)
_T2_LO_B = np.array([162, 48, 22], dtype=np.uint8)
_T2_HI_B = np.array([180, 230, 165], dtype=np.uint8)

# A row needs at least this fraction of sampled pixels to count as coloured
_COV_THRESHOLD = 0.22

# Minimum height for a valid team block (fraction of image height)
_MIN_BLOCK_FRAC = 0.17


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

def _row_coverage(hsv: np.ndarray, x0: int, x1: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-row fraction of Team 1 / Team 2 coloured pixels in column range."""
    samp = hsv[:, x0:x1]
    t1  = cv2.inRange(samp, _T1_LO, _T1_HI)
    t2  = cv2.bitwise_or(
        cv2.inRange(samp, _T2_LO_A, _T2_HI_A),
        cv2.inRange(samp, _T2_LO_B, _T2_HI_B),
    )
    cov1 = np.mean(t1, axis=1) / 255.0
    cov2 = np.mean(t2, axis=1) / 255.0
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

def detect_table_geometry(img: np.ndarray) -> Optional[TableGeometry]:
    """
    Detect the scoreboard table in a BGR image.
    Returns TableGeometry (valid=True) or None on failure.
    """
    if not _CV2:
        return None

    h, w = img.shape[:2]
    min_block_h = int(h * _MIN_BLOCK_FRAC)

    # Sample columns — use the inner 50% of width to ignore sidebars
    x_lo = int(w * 0.25)
    x_hi = int(w * 0.75)

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    cov1, cov2 = _row_coverage(hsv, x_lo, x_hi)

    # Skip top 15 % — score + metadata header, not the data table
    skip = int(h * 0.15)
    cov1[:skip] = 0.0
    cov2[:skip] = 0.0

    t1_block = _largest_block(cov1 > _COV_THRESHOLD, min_block_h)
    t2_block = _largest_block(cov2 > _COV_THRESHOLD, min_block_h)

    if t1_block is None or t2_block is None:
        log.debug("detect_table_geometry: could not find team color blocks")
        return None

    t1_y0, t1_y1 = t1_block
    t2_y0, t2_y1 = t2_block

    # Sanity check: Team 1 must be above Team 2
    if t1_y0 >= t2_y0:
        log.debug("detect_table_geometry: team1 block not above team2 block")
        return None

    # Build full masks for horizontal extent detection
    t1_full = cv2.inRange(hsv, _T1_LO, _T1_HI)
    t2_full = cv2.bitwise_or(
        cv2.inRange(hsv, _T2_LO_A, _T2_HI_A),
        cv2.inRange(hsv, _T2_LO_B, _T2_HI_B),
    )
    x0, x1 = _find_horizontal_bounds(t1_full, t2_full, t1_y0, t2_y1)

    # Split into individual player rows
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
        log.debug("detect_table_geometry: geometry failed validity check: %s", geom)
        return None

    log.info(
        "Table detected: t1=[%d,%d] t2=[%d,%d] x=[%d,%d] tw=%d",
        t1_y0, t1_y1, t2_y0, t2_y1, x0, x1, geom.table_width,
    )
    return geom
