"""
utils/match_ocr.py  (v3 — fully local, zero API calls)
--------------------------------------------------------
Valorant match end-screen OCR using OpenCV + Tesseract only.

Pipeline:
  1. Normalise image to 1920px wide  (makes all column positions consistent)
  2. Preprocess  — denoise, unsharp-mask, CLAHE
  3. Score extraction   — OCR top-centre region, regex for "N 获胜 M"
  4. Metadata extraction — map, date, duration from top-left
  5. Table row segmentation — HSV colour masks find green/red row bands
  6. Per-row OCR  — proportional column crops + cell-specific Tesseract configs
  7. Return structured MatchOCRResult (no network calls)

Requirements (install on Raspberry Pi / Railway):
  pip install opencv-python-headless pytesseract numpy Pillow
  sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# ── Lazy imports (fail gracefully so the bot still boots if libs are missing) ─
try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False
    log.warning("opencv-python-headless not installed — OCR unavailable.")

try:
    import pytesseract
    pytesseract.get_tesseract_version()
    _TESS_OK = True
except Exception:
    _TESS_OK = False
    log.warning("Tesseract binary not found — OCR unavailable. "
                "Install: sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim")

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


# ── Column layout (as % of image width after normalising to 1920px wide) ──────
#
# Calibrated from multiple Valorant scoreboard screenshots.
# Each tuple: (x_start_pct, x_end_pct)
#
COLS = {
    "ign":     (0.145, 0.380),   # in-game name + MVP badge
    "acs":     (0.440, 0.530),   # average combat score
    "kda":     (0.510, 0.660),   # kills/deaths/assists
    "dmg":     (0.625, 0.760),   # total damage
    "fb":      (0.740, 0.815),   # first bloods
    "plants":  (0.800, 0.870),   # spike plants
    "defuses": (0.855, 0.930),   # spike defuses
}

# Tesseract page-segmentation configs per column type
_CFG_DIGITS = "--psm 7 --oem 1 -c tessedit_char_whitelist=0123456789"
_CFG_KDA    = "--psm 7 --oem 1 -c tessedit_char_whitelist=0123456789/|lI"
_CFG_TEXT   = "--psm 7 --oem 1"                # Chinese + Latin for IGN


# ── Step 1: Image loading & normalisation ─────────────────────────────────────

def _load_and_normalise(image_bytes: bytes) -> Optional[np.ndarray]:
    """
    Decode image bytes and resize to exactly 1920px wide (preserving aspect).
    This ensures all column % positions work regardless of original resolution.
    """
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return None

    h, w = img.shape[:2]
    if w == 1920:
        return img

    interp = cv2.INTER_CUBIC if w < 1920 else cv2.INTER_AREA
    new_h = int(h * 1920 / w)
    return cv2.resize(img, (1920, new_h), interpolation=interp)


# ── Step 2: Global preprocessing ──────────────────────────────────────────────

def _enhance(img: np.ndarray) -> np.ndarray:
    """Denoise → unsharp mask → CLAHE on luminance."""
    # Fast denoise (preserves text edges)
    img = cv2.fastNlMeansDenoisingColored(img, None, h=4, hColor=4,
                                          templateWindowSize=7, searchWindowSize=21)
    # Unsharp mask (sharpens text)
    blur = cv2.GaussianBlur(img, (0, 0), 1.5)
    img = cv2.addWeighted(img, 1.4, blur, -0.4, 0)

    # CLAHE on L channel
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    l_ch = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l_ch)
    img = cv2.cvtColor(cv2.merge([l_ch, a_ch, b_ch]), cv2.COLOR_LAB2BGR)
    return img


# ── Step 3: Score + metadata extraction ───────────────────────────────────────

def _extract_score_and_meta(img: np.ndarray) -> tuple[int, int, str, str, str, str]:
    """
    Returns (team1_score, team2_score, outcome, map_name, match_date, duration).
    Looks at the top portion of the image for the big "N 获胜 M" header.
    """
    h, w = img.shape[:2]

    # Score region: top 22%, centre 45% of width
    score_crop = img[0: int(h * 0.22), int(w * 0.28): int(w * 0.72)]
    score_text = _tess_text(score_crop, _CFG_TEXT, lang="chi_sim+eng")

    t1_score = t2_score = 0
    outcome = "Unknown"

    # "12 获胜 10" or "8获胜5"
    m = re.search(r"(\d{1,2})\s*获胜\s*(\d{1,2})", score_text)
    if m:
        t1_score, t2_score = int(m.group(1)), int(m.group(2))
        outcome = "Victory"
    else:
        # Try just two numbers separated by whitespace / non-digit
        nums = re.findall(r"\d{1,2}", score_text)
        if len(nums) >= 2:
            t1_score, t2_score = int(nums[0]), int(nums[1])
            outcome = "Victory" if t1_score >= t2_score else "Defeat"

    # Metadata: top-left, roughly 0-35% width × top 20% height
    meta_crop = img[int(h * 0.09): int(h * 0.21), int(w * 0.07): int(w * 0.38)]
    meta_text = _tess_text(meta_crop, _CFG_TEXT, lang="chi_sim+eng")

    map_name = "Unknown"
    match_date = "Unknown"
    duration = "Unknown"

    # Map name: text before a newline containing mode keywords
    mode_m = re.search(r"([^\n\r]*(?:模式|明珠|深海|莲华|古城|Lotus|Pearl|Ascent|Haven|Split|Bind|Breeze|Fracture|Icebox|Sunset|Abyss)[^\n\r]*)", meta_text)
    if mode_m:
        map_name = mode_m.group(1).strip()
    elif meta_text.strip():
        map_name = meta_text.splitlines()[0].strip()[:40] or "Unknown"

    # Date: YYYY/MM/DD HH:MM
    date_m = re.search(r"(\d{4}[/\-]\d{2}[/\-]\d{2}(?:\s+\d{2}:\d{2})?)", meta_text)
    if date_m:
        match_date = date_m.group(1)

    # Duration: "用时 MM:SS"
    dur_m = re.search(r"用时\s*(\d{1,2}:\d{2})", meta_text)
    if not dur_m:
        dur_m = re.search(r"(\d{2}:\d{2})\s*$", meta_text.strip())
    if dur_m:
        duration = dur_m.group(1)

    return t1_score, t2_score, outcome, map_name, match_date, duration


# ── Step 4: Row segmentation ───────────────────────────────────────────────────

# HSV ranges for team colours
# Team 1 — teal/green rows (hue ≈ 75–130)
_T1_LO = np.array([72,  35, 35], dtype=np.uint8)
_T1_HI = np.array([138, 255, 210], dtype=np.uint8)

# Team 2 — red/maroon rows (hue wraps around 0/180)
_T2_LO_A = np.array([0,   35, 25], dtype=np.uint8)
_T2_HI_A = np.array([18,  255, 165], dtype=np.uint8)
_T2_LO_B = np.array([158, 35, 25], dtype=np.uint8)
_T2_HI_B = np.array([180, 255, 165], dtype=np.uint8)


def _row_team_labels(img: np.ndarray) -> np.ndarray:
    """
    Returns a 1-D int array, one element per image row:
      0 = not a player row
      1 = Team 1 (teal)
      2 = Team 2 (red/maroon)
    Uses the MIDDLE half of the image width (avoids agent-icon & action-icon noise).
    """
    h, w = img.shape[:2]
    x0, x1 = int(w * 0.30), int(w * 0.75)   # sample columns in data area

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    sample = hsv[:, x0:x1]

    t1 = cv2.inRange(sample, _T1_LO, _T1_HI)
    t2a = cv2.inRange(sample, _T2_LO_A, _T2_HI_A)
    t2b = cv2.inRange(sample, _T2_LO_B, _T2_HI_B)
    t2 = cv2.bitwise_or(t2a, t2b)

    row_t1 = np.mean(t1, axis=1) / 255.0   # fraction of sampled pixels matching T1 colour
    row_t2 = np.mean(t2, axis=1) / 255.0

    labels = np.zeros(h, dtype=np.int8)
    THRESH = 0.20   # at least 20% coverage to count as a coloured row
    labels[row_t1 > THRESH] = 1
    labels[(row_t2 > THRESH) & (row_t1 <= THRESH)] = 2
    return labels


def _find_player_row_bands(labels: np.ndarray) -> list[tuple[int, int, int]]:
    """
    Convert per-pixel team labels → contiguous row bands.
    Returns list of (y_start, y_end, team_id) sorted by y_start.
    Ignores bands narrower than 10px (noise / divider lines).
    """
    bands: list[tuple[int, int, int]] = []
    h = len(labels)
    i = 0
    while i < h:
        team = labels[i]
        if team == 0:
            i += 1
            continue
        j = i + 1
        while j < h and labels[j] == team:
            j += 1
        if j - i >= 10:
            bands.append((i, j, int(team)))
        i = j
    return bands


# ── Step 5: Cell preprocessing ────────────────────────────────────────────────

def _prep_digit_cell(crop: np.ndarray) -> np.ndarray:
    """Binarise a numeric cell for Tesseract."""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
    # Upscale tiny crops
    h, w = gray.shape[:2]
    if h < 30 or w < 40:
        gray = cv2.resize(gray, (max(w * 2, 80), max(h * 2, 40)),
                          interpolation=cv2.INTER_CUBIC)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Invert if text is dark on light
    if cv2.countNonZero(bw) > bw.size * 0.6:
        bw = cv2.bitwise_not(bw)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kernel)
    # Pad to help Tesseract
    bw = cv2.copyMakeBorder(bw, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=255)
    return bw


def _prep_text_cell(crop: np.ndarray) -> np.ndarray:
    """Enhance a text (IGN) cell with CLAHE."""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
    h, w = gray.shape[:2]
    if h < 30 or w < 60:
        gray = cv2.resize(gray, (max(w * 2, 120), max(h * 2, 40)),
                          interpolation=cv2.INTER_CUBIC)
    gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4)).apply(gray)
    gray = cv2.copyMakeBorder(gray, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=0)
    return gray


# ── Step 6: Tesseract helpers ──────────────────────────────────────────────────

def _tess_text(img: np.ndarray, cfg: str, lang: str = "chi_sim+eng") -> str:
    if not _TESS_OK:
        return ""
    try:
        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB) if len(img.shape) == 3 else img)
        return pytesseract.image_to_string(pil, config=cfg, lang=lang).strip()
    except Exception as e:
        log.debug("Tesseract error: %s", e)
        return ""


def _ocr_int(crop: np.ndarray) -> int:
    """OCR a single integer from a digit cell."""
    proc = _prep_digit_cell(crop)
    txt = _tess_text(proc, _CFG_DIGITS, lang="eng")
    digits = re.sub(r"\D", "", txt)
    return int(digits) if digits else 0


def _ocr_kda(crop: np.ndarray) -> tuple[int, int, int]:
    """OCR a K/D/A cell and return (kills, deaths, assists)."""
    proc = _prep_digit_cell(crop)
    txt = _tess_text(proc, _CFG_KDA, lang="eng")

    # Normalise common Tesseract misreads
    txt = txt.replace("l", "/").replace("I", "1").replace("O", "0").replace("|", "/")

    m = re.search(r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)", txt)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

    nums = re.findall(r"\d+", txt)
    if len(nums) >= 3:
        return int(nums[0]), int(nums[1]), int(nums[2])
    elif len(nums) == 2:
        return int(nums[0]), int(nums[1]), 0
    elif len(nums) == 1:
        return int(nums[0]), 0, 0
    return 0, 0, 0


def _ocr_ign(crop: np.ndarray) -> tuple[str, bool, Optional[str]]:
    """
    OCR the IGN + MVP badge cell.
    Returns (ign_clean, is_mvp, mvp_type).
    """
    proc = _prep_text_cell(crop)
    txt = _tess_text(proc, _CFG_TEXT, lang="chi_sim+eng")

    is_mvp = False
    mvp_type = None

    if "我方" in txt and "最佳" in txt:
        is_mvp = True
        mvp_type = "Team MVP"
    elif "敌方" in txt and "最佳" in txt:
        is_mvp = True
        mvp_type = "Match MVP"
    elif re.search(r"MVP", txt, re.IGNORECASE):
        is_mvp = True
        mvp_type = "Team MVP"

    # Strip badges / noisy suffixes from the IGN
    clean = re.sub(r"(我方|敌方|最佳|MVP|[\-—_·\s]{2,})", " ", txt, flags=re.IGNORECASE)
    clean = re.sub(r"\s+", " ", clean).strip()

    # Remove purely non-alphanumeric noise lines (keep Chinese + Latin + digits + common ign chars)
    clean = re.sub(r"[^\w\u4e00-\u9fff.\-!]", "", clean)
    clean = clean.strip() or "Unknown"

    return clean, is_mvp, mvp_type


# ── Step 7: Row → PlayerRowStats ──────────────────────────────────────────────

def _extract_row(row_img: np.ndarray, team: str) -> PlayerRowStats:
    """Crop each column from a single player row and OCR it."""
    h, w = row_img.shape[:2]

    def crop(col_key: str) -> np.ndarray:
        x0 = int(COLS[col_key][0] * w)
        x1 = int(COLS[col_key][1] * w)
        return row_img[:, x0:x1]

    ign, is_mvp, mvp_type = _ocr_ign(crop("ign"))
    acs                   = _ocr_int(crop("acs"))
    kills, deaths, assists = _ocr_kda(crop("kda"))
    dmg                   = _ocr_int(crop("dmg"))
    fb                    = _ocr_int(crop("fb"))
    plants                = _ocr_int(crop("plants"))
    defuses               = _ocr_int(crop("defuses"))

    return PlayerRowStats(
        ign=ign, team=team,
        is_mvp=is_mvp, mvp_type=mvp_type,
        acs=acs, kills=kills, deaths=deaths, assists=assists,
        damage=dmg, first_bloods=fb, plants=plants, defuses=defuses,
    )


# ── Main synchronous parser ────────────────────────────────────────────────────

def _parse_local(image_bytes: bytes) -> MatchOCRResult:
    """Full local OCR pipeline. Returns a MatchOCRResult."""
    if not _CV2_OK:
        return MatchOCRResult(success=False,
                              error="opencv-python-headless not installed.")
    if not _TESS_OK:
        return MatchOCRResult(success=False,
                              error="Tesseract not installed. "
                                    "Run: sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim")

    t0 = time.perf_counter()

    # 1. Load + normalise to 1920px wide
    img = _load_and_normalise(image_bytes)
    if img is None:
        return MatchOCRResult(success=False, error="Could not decode image.")

    # 2. Enhance globally
    img = _enhance(img)
    h, w = img.shape[:2]

    # 3. Extract score + metadata
    t1_score, t2_score, outcome, map_name, match_date, duration = (
        _extract_score_and_meta(img)
    )

    # 4. Segment rows by colour
    labels = _row_team_labels(img)
    bands  = _find_player_row_bands(labels)

    if not bands:
        return MatchOCRResult(
            success=False,
            error="Could not detect scoreboard rows. "
                  "Make sure the image shows the full match end-screen.",
            processing_time_ms=round((time.perf_counter() - t0) * 1000, 1),
        )

    # 5. OCR each row
    result = MatchOCRResult(
        success=True,
        engine="OpenCV+Tesseract (local)",
        map_name=map_name,
        match_date=match_date,
        duration=duration,
        team1_score=t1_score,
        team2_score=t2_score,
        outcome=outcome,
        processing_time_ms=0.0,
    )

    log.info("Found %d row bands in %dpx image.", len(bands), w)

    for y_start, y_end, team_id in bands:
        row_crop = img[y_start:y_end, :]
        # The row image is full-width (1920px) so COLS % positions apply directly
        team_label = "Team 1 (Green)" if team_id == 1 else "Team 2 (Red)"
        stats = _extract_row(row_crop, team_label)

        if team_id == 1:
            result.team1_players.append(stats)
        else:
            result.team2_players.append(stats)

    result.processing_time_ms = round((time.perf_counter() - t0) * 1000, 1)
    log.info(
        "Local OCR complete: %d T1 players, %d T2 players, %.0fms",
        len(result.team1_players), len(result.team2_players), result.processing_time_ms,
    )

    result.success = len(result.all_players) > 0
    if not result.success:
        result.error = "No players detected. Check that the screenshot shows the full scoreboard."

    return result


# ── Public async entrypoint ────────────────────────────────────────────────────

async def process_match_screenshot(image_bytes: bytes) -> MatchOCRResult:
    """
    Async wrapper: runs the blocking OpenCV + Tesseract pipeline in a thread
    so the Discord event loop is never blocked.
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _parse_local, image_bytes)
    return result
