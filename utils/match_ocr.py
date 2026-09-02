"""
utils/match_ocr.py  (v6 — per-cell OCR, optimised preprocessing)
-----------------------------------------------------------------
Valorant match scoreboard OCR — fully local, zero API calls.

Implementation follows the guide:
  1. Normalise to fixed canvas (1920 × 1080 → 16:9 standard)
  2. Crop each cell individually by fixed relative coordinates
  3. Per-cell preprocessing:
       • Grayscale → 2× upscale (INTER_CUBIC) → THRESH_BINARY_INV at 180
       (inverts so text = black, background = white — Tesseract's sweet spot)
  4. PSM / whitelist per column type:
       • IGN text   : --psm 7  --oem 3  (chi_sim+eng)
       • Numbers    : --psm 8  --oem 3  -c whitelist=0123456789
       • K/D/A      : --psm 7  --oem 3  -c whitelist=0123456789/

Install:
  pip install opencv-python-headless pytesseract numpy Pillow
  sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False
    log.warning("opencv-python-headless not installed — OCR disabled.")

try:
    import pytesseract
    pytesseract.get_tesseract_version()
    _TESS_OK = True
except Exception:
    _TESS_OK = False
    log.warning("Tesseract not found. Run: sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim")


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class PlayerRowStats:
    ign: str = "Unknown"
    team: str = "Team 1"
    is_mvp: bool = False
    mvp_type: Optional[str] = None
    acs: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    damage: int = 0
    first_bloods: int = 0
    plants: int = 0
    defuses: int = 0

    @property
    def kda_str(self) -> str:
        return f"{self.kills}/{self.deaths}/{self.assists}"

    @property
    def kd_ratio(self) -> float:
        return round(self.kills / max(1, self.deaths), 2)


@dataclass
class MatchOCRResult:
    success: bool = False
    error: Optional[str] = None
    engine: str = "OpenCV+Tesseract"
    processing_time_ms: float = 0.0
    map_name: str = "Unknown"
    match_date: str = "Unknown"
    duration: str = "Unknown"
    team1_score: int = 0
    team2_score: int = 0
    outcome: str = "Unknown"
    team1_players: list[PlayerRowStats] = field(default_factory=list)
    team2_players: list[PlayerRowStats] = field(default_factory=list)

    @property
    def all_players(self) -> list[PlayerRowStats]:
        return self.team1_players + self.team2_players


# ── Fixed canvas & column layout ──────────────────────────────────────────────
#
# All screenshots are normalised to a 1920 × 1080 canvas before anything else.
# Column bounds are expressed as (x_start_pct, x_end_pct) of 1920.
# Row bounds as (y_start_pct, y_end_pct) of 1080.
#
# Calibrated from multiple Valorant (and Valorant-Mobile) scoreboard screenshots.

TARGET_W = 1920
TARGET_H = 1080

# Column x-ranges (percentage of TARGET_W)
COLS = {
    "ign":     (0.155, 0.440),   # agent icon + player name
    "acs":     (0.435, 0.520),
    "kda":     (0.490, 0.635),
    "dmg":     (0.610, 0.745),
    "fb":      (0.725, 0.800),
    "plants":  (0.785, 0.858),
    "defuses": (0.843, 0.915),
}

# Row y-ranges (percentage of TARGET_H) — Team 1 first, then Team 2
# These assume the header (队伍排名, 平均战斗评分…) occupies ~24–32 % from top.
# Each player row is ~7 % of TARGET_H tall.
# Rows are defined as half-open intervals [y0, y1).
_ROW_Y: list[tuple[float, float]] = [
    # Team 1 (rows 0–4)
    (0.295, 0.375),
    (0.375, 0.455),
    (0.455, 0.535),
    (0.535, 0.610),
    (0.610, 0.685),
    # Team 2 (rows 5–9)
    (0.685, 0.760),
    (0.760, 0.835),
    (0.835, 0.905),
    (0.905, 0.960),
    (0.960, 1.000),
]

_N_PER_TEAM = 5

# Tesseract configs
_PSM7_TEXT    = "--oem 3 --psm 7"                                          # single text line
_PSM8_DIGITS  = "--oem 3 --psm 8 -c tessedit_char_whitelist=0123456789"   # single word / number
_PSM7_KDA     = "--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789/"  # K/D/A with slash


# ── Step 1 — Normalise to fixed 16:9 canvas ───────────────────────────────────

def _normalise(image_bytes: bytes) -> Optional[np.ndarray]:
    """
    Decode and resize to exactly 1920 × 1080.
    Handles any input resolution or aspect ratio by fitting to width first,
    then center-cropping / letterboxing vertically if needed.
    """
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return None

    h, w = img.shape[:2]
    if w == TARGET_W and h == TARGET_H:
        return img

    # Scale to width
    scale = TARGET_W / w
    new_h = int(h * scale)
    img = cv2.resize(img, (TARGET_W, new_h),
                     interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)

    # Pad or crop vertically to reach TARGET_H
    cur_h = img.shape[0]
    if cur_h < TARGET_H:
        pad = TARGET_H - cur_h
        img = cv2.copyMakeBorder(img, 0, pad, 0, 0, cv2.BORDER_CONSTANT, value=0)
    elif cur_h > TARGET_H:
        img = img[:TARGET_H, :]

    return img


# ── Step 2 — Per-cell preprocessing ───────────────────────────────────────────

def preprocess_cell(img_crop: np.ndarray, is_number_only: bool = False) -> np.ndarray:
    """
    Prepare a single cell crop for Tesseract:
      1. Grayscale
      2. 2× upscale (INTER_CUBIC) — Tesseract performs best with ~30–40 px text height
      3. THRESH_BINARY_INV at 180 → text becomes BLACK, background WHITE
      4. White border padding (Tesseract needs breathing room)
    """
    # Grayscale
    gray = cv2.cvtColor(img_crop, cv2.COLOR_BGR2GRAY) if len(img_crop.shape) == 3 else img_crop

    # 2× upscale
    gray = cv2.resize(gray, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

    # For pure number cells a slight blur reduces noise before threshold
    if is_number_only:
        gray = cv2.GaussianBlur(gray, (3, 3), 0)

    # BINARY_INV at 180: bright text → black, dark background → white
    _, thresh = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)

    # Check result quality: if almost entirely black (over-thresholded), try Otsu
    white_frac = cv2.countNonZero(thresh) / thresh.size
    if white_frac < 0.02 or white_frac > 0.95:
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # Padding — helps Tesseract not clip edge characters
    return cv2.copyMakeBorder(thresh, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)


# ── Step 3 — Score & metadata ──────────────────────────────────────────────────

def _ocr_score(img: np.ndarray) -> tuple[int, int, str]:
    """Extract the 'N 获胜 M' header score from the top-centre region."""
    h, w = img.shape[:2]
    crop = img[0:int(h * 0.24), int(w * 0.28):int(w * 0.72)]
    proc = preprocess_cell(crop, is_number_only=False)
    pil  = Image.fromarray(proc)

    for lang in ("chi_sim+eng", "eng"):
        txt = pytesseract.image_to_string(pil, config="--oem 3 --psm 6", lang=lang)
        m = re.search(r"(\d{1,2})\s*获胜\s*(\d{1,2})", txt)
        if m:
            return int(m.group(1)), int(m.group(2)), "Victory"
        nums = re.findall(r"\b(\d{1,2})\b", txt)
        if len(nums) >= 2:
            a, b = int(nums[0]), int(nums[1])
            return a, b, ("Victory" if a >= b else "Defeat")
    return 0, 0, "Unknown"


def _ocr_meta(img: np.ndarray) -> tuple[str, str, str]:
    """Extract map name, date, duration from top-left metadata block."""
    h, w = img.shape[:2]
    crop = img[int(h * 0.07):int(h * 0.24), int(w * 0.07):int(w * 0.38)]
    pil  = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
    txt  = pytesseract.image_to_string(pil, config="--oem 3 --psm 6", lang="chi_sim+eng")

    map_name = match_date = duration = "Unknown"

    keywords = r"模式|明珠|深海|莲华|古城|Lotus|Pearl|Ascent|Haven|Split|Bind|Breeze|Fracture|Icebox|Sunset|Abyss"
    m = re.search(rf"([^\n\r]*(?:{keywords})[^\n\r]*)", txt)
    if m:
        map_name = m.group(1).strip()[:50]
    elif txt.strip():
        map_name = txt.splitlines()[0].strip()[:40] or "Unknown"

    dm = re.search(r"(\d{4}[/\-]\d{2}[/\-]\d{2}(?:\s+\d{2}:\d{2})?)", txt)
    if dm:
        match_date = dm.group(1)

    dr = re.search(r"用时\s*(\d{1,2}:\d{2})", txt) or re.search(r"\b(\d{1,2}:\d{2})\b", txt)
    if dr:
        duration = dr.group(1)

    return map_name, match_date, duration


# ── Step 4 — Row auto-detection (fallback if fixed rows don't align) ───────────

_T1_LO = np.array([72,  25, 25], dtype=np.uint8)
_T1_HI = np.array([142, 255, 225], dtype=np.uint8)
_T2_LO_A = np.array([0,   25, 18], dtype=np.uint8)
_T2_HI_A = np.array([18,  255, 175], dtype=np.uint8)
_T2_LO_B = np.array([158, 25, 18], dtype=np.uint8)
_T2_HI_B = np.array([180, 255, 175], dtype=np.uint8)


def _detect_row_offsets(img: np.ndarray) -> Optional[dict[str, float]]:
    """
    Dynamically find where Team 1 rows begin and where Team 2 ends.
    Returns {'t1_start': float, 't2_end': float} as Y fractions of image height,
    or None if detection fails (caller falls back to fixed _ROW_Y).
    """
    h, w = img.shape[:2]
    x0, x1 = int(w * 0.35), int(w * 0.70)
    skip_y = int(h * 0.25)    # ignore the header zone

    hsv  = cv2.cvtColor(img[skip_y:, x0:x1], cv2.COLOR_BGR2HSV)
    t1   = cv2.inRange(hsv, _T1_LO, _T1_HI)
    t2   = cv2.bitwise_or(cv2.inRange(hsv, _T2_LO_A, _T2_HI_A),
                          cv2.inRange(hsv, _T2_LO_B, _T2_HI_B))

    cov1 = np.mean(t1, axis=1) / 255.0
    cov2 = np.mean(t2, axis=1) / 255.0

    rows_t1 = np.where(cov1 > 0.18)[0]
    rows_t2 = np.where(cov2 > 0.18)[0]

    if len(rows_t1) < 10 or len(rows_t2) < 10:
        return None  # not enough colour coverage → use fixed rows

    t1_start = (rows_t1[0] + skip_y) / h
    t2_end   = (rows_t2[-1] + skip_y) / h
    t1_end   = (rows_t1[-1] + skip_y) / h
    t2_start = (rows_t2[0]  + skip_y) / h

    return {
        "t1_start": t1_start,
        "t1_end":   t1_end,
        "t2_start": t2_start,
        "t2_end":   t2_end,
    }


def _build_row_y(img: np.ndarray) -> list[tuple[float, float]]:
    """
    Return 10 (y0, y1) pairs as fractions of image height.
    Tries dynamic detection first; falls back to the calibrated fixed layout.
    """
    offsets = _detect_row_offsets(img)
    if offsets:
        t1_h = offsets["t1_end"] - offsets["t1_start"]
        t2_h = offsets["t2_end"] - offsets["t2_start"]
        rh1  = t1_h / _N_PER_TEAM
        rh2  = t2_h / _N_PER_TEAM

        rows = []
        for i in range(_N_PER_TEAM):
            y0 = offsets["t1_start"] + i * rh1
            rows.append((y0, y0 + rh1))
        for i in range(_N_PER_TEAM):
            y0 = offsets["t2_start"] + i * rh2
            rows.append((y0, y0 + rh2))
        return rows

    log.debug("Dynamic row detection failed — using fixed _ROW_Y layout.")
    return _ROW_Y


# ── Step 5 — Per-cell OCR helpers ─────────────────────────────────────────────

def _cell(img: np.ndarray, row_idx: int, col_key: str) -> np.ndarray:
    """Crop a single cell from the full 1920×1080 image."""
    row_y = _build_row_y(img)      # cached implicitly by caller
    y0f, y1f = row_y[row_idx]
    x0f, x1f = COLS[col_key]
    h, w = img.shape[:2]
    return img[int(y0f * h): int(y1f * h), int(x0f * w): int(x1f * w)]


def _read_int(cell_crop: np.ndarray) -> int:
    """OCR a single integer from a number cell. PSM 8 = single word."""
    proc = preprocess_cell(cell_crop, is_number_only=True)
    txt  = pytesseract.image_to_string(Image.fromarray(proc), config=_PSM8_DIGITS, lang="eng").strip()
    dig  = re.sub(r"\D", "", txt)
    return int(dig) if dig else 0


def _read_kda(cell_crop: np.ndarray) -> tuple[int, int, int]:
    """OCR a K/D/A cell. PSM 7 = single text line."""
    proc = preprocess_cell(cell_crop, is_number_only=False)
    txt  = pytesseract.image_to_string(Image.fromarray(proc), config=_PSM7_KDA, lang="eng").strip()
    txt  = txt.replace("l", "/").replace("I", "1").replace("O", "0").replace("|", "/").replace(" ", "")
    m    = re.search(r"(\d+)/(\d+)/(\d+)", txt)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    nums = re.findall(r"\d+", txt)
    if len(nums) >= 3:
        return int(nums[0]), int(nums[1]), int(nums[2])
    return 0, 0, 0


def _read_ign(cell_crop: np.ndarray) -> tuple[str, bool, Optional[str]]:
    """OCR the player IGN + MVP badge. PSM 7 = single text line, chi_sim+eng."""
    proc = preprocess_cell(cell_crop, is_number_only=False)
    txt  = pytesseract.image_to_string(Image.fromarray(proc), config=_PSM7_TEXT, lang="chi_sim+eng").strip()

    is_mvp   = False
    mvp_type = None
    if "我方" in txt and "最佳" in txt:
        is_mvp, mvp_type = True, "Team MVP"
    elif "敌方" in txt and "最佳" in txt:
        is_mvp, mvp_type = True, "Match MVP"
    elif re.search(r"MVP", txt, re.I):
        is_mvp, mvp_type = True, "Team MVP"

    clean = re.sub(r"(我方|敌方|最佳|MVP)", " ", txt, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = re.sub(r"[^\w\u4e00-\u9fff.\-!^~]", "", clean).strip()
    return (clean or "Unknown"), is_mvp, mvp_type


# ── Step 6 — Full pipeline ────────────────────────────────────────────────────

def _parse_local(image_bytes: bytes) -> MatchOCRResult:
    if not _CV2_OK:
        return MatchOCRResult(success=False, error="opencv-python-headless not installed.")
    if not _TESS_OK:
        return MatchOCRResult(
            success=False,
            error="Tesseract not found. Run: sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim",
        )

    t0 = time.perf_counter()

    # 1. Normalise to 1920 × 1080
    img = _normalise(image_bytes)
    if img is None:
        return MatchOCRResult(success=False, error="Could not decode image.")

    # 2. Score + metadata
    t1_score, t2_score, outcome = _ocr_score(img)
    map_name, match_date, duration = _ocr_meta(img)

    # 3. Build row Y positions (dynamic or fixed)
    row_y = _build_row_y(img)
    h, w  = img.shape[:2]

    # 4. OCR every cell  (loop over 10 rows × 7 columns = 70 calls max)
    t1_players: list[PlayerRowStats] = []
    t2_players: list[PlayerRowStats] = []

    for row_idx in range(len(row_y)):
        y0f, y1f = row_y[row_idx]
        row_crop  = img[int(y0f * h): int(y1f * h), :]
        is_team1  = row_idx < _N_PER_TEAM
        team_label = "Team 1 (Green)" if is_team1 else "Team 2 (Red)"

        def _crop(key: str) -> np.ndarray:
            x0f2, x1f2 = COLS[key]
            return row_crop[:, int(x0f2 * w): int(x1f2 * w)]

        ign, is_mvp, mvp_type  = _read_ign(_crop("ign"))
        acs                    = _read_int(_crop("acs"))
        kills, deaths, assists = _read_kda(_crop("kda"))
        dmg                    = _read_int(_crop("dmg"))
        fb                     = _read_int(_crop("fb"))
        plants                 = _read_int(_crop("plants"))
        defuses                = _read_int(_crop("defuses"))

        stats = PlayerRowStats(
            ign=ign, team=team_label, is_mvp=is_mvp, mvp_type=mvp_type,
            acs=acs, kills=kills, deaths=deaths, assists=assists,
            damage=dmg, first_bloods=fb, plants=plants, defuses=defuses,
        )

        if is_team1:
            t1_players.append(stats)
        else:
            t2_players.append(stats)

    ms = round((time.perf_counter() - t0) * 1000, 1)
    log.info("OCR complete: %d T1 + %d T2 players in %.0f ms", len(t1_players), len(t2_players), ms)

    return MatchOCRResult(
        success=bool(t1_players or t2_players),
        engine="OpenCV+Tesseract (local, per-cell)",
        processing_time_ms=ms,
        map_name=map_name,
        match_date=match_date,
        duration=duration,
        team1_score=t1_score,
        team2_score=t2_score,
        outcome=outcome,
        team1_players=t1_players,
        team2_players=t2_players,
        error=None if (t1_players or t2_players) else "No players detected.",
    )


# ── Public async entry point ───────────────────────────────────────────────────

async def process_match_screenshot(image_bytes: bytes) -> MatchOCRResult:
    """Run the blocking OCR pipeline in a thread pool — keeps the bot event loop free."""
    return await asyncio.get_running_loop().run_in_executor(None, _parse_local, image_bytes)
