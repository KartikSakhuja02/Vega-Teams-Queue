"""
utils/match_ocr.py  (v4 — fully local, optimised)
---------------------------------------------------
Valorant match end-screen OCR — OpenCV + Tesseract only, zero API calls.

Key fixes over v3:
  ✓  Row detection: each team block is split into 5 equal sub-rows (was
     treating the whole team section as a single row → garbage numbers)
  ✓  Column positions recalibrated from real screenshots
  ✓  Digit cells: bright-pixel thresholding isolates white text from
     teal/red backgrounds (much better than Otsu on coloured BG)
  ✓  Every row crop is upscaled to ≥80 px tall before Tesseract
  ✓  Score detection uses the raw top-centre crop + multiple regex fallbacks
  ✓  IGN column starts after the agent icon area

Install on Raspberry Pi / Railway:
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
    log.warning("Tesseract not found. Install: sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim")


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


# ── Column layout — % of image width after normalising to 1920 px wide ────────
#
# Calibrated from real Valorant scoreboard screenshots at multiple resolutions.
# "ign" starts AFTER the agent-icon area (~23 %) so we don't OCR the icon.
#
COLS = {
    "ign":     (0.23, 0.46),
    "acs":     (0.44, 0.52),
    "kda":     (0.50, 0.64),
    "dmg":     (0.62, 0.75),
    "fb":      (0.73, 0.81),
    "plants":  (0.79, 0.86),
    "defuses": (0.84, 0.92),
}

_PLAYERS_PER_TEAM = 5   # standard Valorant match

# Tesseract configs
_CFG_DIGITS = "--psm 7 --oem 1 -c tessedit_char_whitelist=0123456789"
_CFG_KDA    = "--psm 7 --oem 1 -c tessedit_char_whitelist=0123456789/|lI"
_CFG_TEXT   = "--psm 7 --oem 1"


# ── 1. Load & normalise ────────────────────────────────────────────────────────

def _load_and_normalise(image_bytes: bytes) -> Optional[np.ndarray]:
    """Decode and resize to exactly 1920 px wide (proportional height)."""
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    if w == 1920:
        return img
    interp = cv2.INTER_CUBIC if w < 1920 else cv2.INTER_AREA
    return cv2.resize(img, (1920, int(h * 1920 / w)), interpolation=interp)


# ── 2. Global image enhancement ───────────────────────────────────────────────

def _enhance(img: np.ndarray) -> np.ndarray:
    """Denoise → unsharp-mask → CLAHE on luminance."""
    img = cv2.fastNlMeansDenoisingColored(img, None, 4, 4, 7, 21)
    blur = cv2.GaussianBlur(img, (0, 0), 1.5)
    img  = cv2.addWeighted(img, 1.4, blur, -0.4, 0)
    lab  = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)


# ── 3. Score & metadata extraction ────────────────────────────────────────────

def _extract_score_and_meta(img: np.ndarray) -> tuple[int, int, str, str, str, str]:
    """
    Extract match score + metadata from the header region.
    Returns (t1_score, t2_score, outcome, map_name, date, duration).
    """
    h, w = img.shape[:2]

    # ── Score: top 22 %, centre 44 % of width ──
    score_crop = img[0:int(h * 0.22), int(w * 0.28):int(w * 0.72)]

    # Boost contrast on the score numbers (they are large & brightly coloured)
    score_gray = cv2.cvtColor(score_crop, cv2.COLOR_BGR2GRAY)
    score_gray = cv2.resize(score_gray, (score_gray.shape[1] * 2, score_gray.shape[0] * 2),
                            interpolation=cv2.INTER_CUBIC)
    _, score_bw = cv2.threshold(score_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    score_pil = Image.fromarray(score_bw)

    # Try chi_sim first (to read 获胜), fall back to eng
    for lang in ("chi_sim+eng", "eng"):
        raw = pytesseract.image_to_string(score_pil, config="--psm 6 --oem 1", lang=lang)
        m = re.search(r"(\d{1,2})\s*获胜\s*(\d{1,2})", raw)
        if m:
            t1, t2 = int(m.group(1)), int(m.group(2))
            return t1, t2, "Victory", *_extract_meta(img)

    # Fallback: grab any two 1-2 digit numbers from the score region
    raw_full = pytesseract.image_to_string(score_pil, config="--psm 6 --oem 1", lang="chi_sim+eng")
    nums = re.findall(r"\b(\d{1,2})\b", raw_full)
    if len(nums) >= 2:
        t1, t2 = int(nums[0]), int(nums[1])
        outcome = "Victory" if t1 >= t2 else "Defeat"
        return t1, t2, outcome, *_extract_meta(img)

    return 0, 0, "Unknown", *_extract_meta(img)


def _extract_meta(img: np.ndarray) -> tuple[str, str, str]:
    """Extract map name, date, duration from the top-left metadata block."""
    h, w = img.shape[:2]
    crop = img[int(h * 0.08):int(h * 0.22), int(w * 0.07):int(w * 0.38)]
    pil  = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
    txt  = pytesseract.image_to_string(pil, config="--psm 6 --oem 1", lang="chi_sim+eng")

    map_name = "Unknown"
    match_date = "Unknown"
    duration = "Unknown"

    mode_m = re.search(
        r"([^\n\r]*(?:模式|明珠|深海|莲华|古城|Lotus|Pearl|Ascent|Haven|Split|Bind"
        r"|Breeze|Fracture|Icebox|Sunset|Abyss)[^\n\r]*)", txt
    )
    if mode_m:
        map_name = mode_m.group(1).strip()[:50]
    elif txt.strip():
        map_name = txt.splitlines()[0].strip()[:40] or "Unknown"

    date_m = re.search(r"(\d{4}[/\-]\d{2}[/\-]\d{2}(?:\s+\d{2}:\d{2})?)", txt)
    if date_m:
        match_date = date_m.group(1)

    dur_m = re.search(r"用时\s*(\d{1,2}:\d{2})", txt)
    if not dur_m:
        dur_m = re.search(r"\b(\d{1,2}:\d{2})\s*$", txt.strip())
    if dur_m:
        duration = dur_m.group(1)

    return map_name, match_date, duration


# ── 4. Row segmentation ────────────────────────────────────────────────────────

# HSV bounds for team colours (OpenCV: H 0-180)
_T1_LO = np.array([72,  30, 30], dtype=np.uint8)   # teal / green
_T1_HI = np.array([140, 255, 220], dtype=np.uint8)

_T2_LO_A = np.array([0,   30, 20], dtype=np.uint8)  # red/maroon (low hue)
_T2_HI_A = np.array([18,  255, 170], dtype=np.uint8)
_T2_LO_B = np.array([158, 30, 20], dtype=np.uint8)  # red/maroon (high hue wrap)
_T2_HI_B = np.array([180, 255, 170], dtype=np.uint8)


def _team_label_per_row(img: np.ndarray) -> np.ndarray:
    """
    Returns int8 array [0=none, 1=team1, 2=team2] for each image row.
    Samples only the middle 45 % of the width to avoid agent-icon & icon noise.
    """
    h, w = img.shape[:2]
    x0, x1 = int(w * 0.30), int(w * 0.75)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    s = hsv[:, x0:x1]

    t1  = cv2.inRange(s, _T1_LO, _T1_HI)
    t2a = cv2.inRange(s, _T2_LO_A, _T2_HI_A)
    t2b = cv2.inRange(s, _T2_LO_B, _T2_HI_B)
    t2  = cv2.bitwise_or(t2a, t2b)

    cov_t1 = np.mean(t1, axis=1) / 255.0
    cov_t2 = np.mean(t2, axis=1) / 255.0

    labels = np.zeros(h, dtype=np.int8)
    THRESH = 0.18
    labels[cov_t1 > THRESH] = 1
    labels[(cov_t2 > THRESH) & (cov_t1 <= THRESH)] = 2
    return labels


def _find_team_blocks(labels: np.ndarray) -> list[tuple[int, int, int]]:
    """
    Merge consecutive same-team pixels into large blocks (one block per team section).
    Returns [(y_start, y_end, team_id), ...] — typically 2 entries (one per team).
    """
    h = len(labels)
    blocks: list[tuple[int, int, int]] = []
    i = 0
    while i < h:
        t = int(labels[i])
        if t == 0:
            i += 1
            continue
        j = i + 1
        while j < h and labels[j] == t:
            j += 1
        if j - i >= 20:          # ignore tiny blobs
            blocks.append((i, j, t))
        i = j
    return blocks


def _split_block_into_rows(
    y_start: int, y_end: int, team_id: int,
    img: np.ndarray,
    n: int = _PLAYERS_PER_TEAM,
) -> list[tuple[int, int, int]]:
    """
    Split a team colour block into `n` individual player rows.

    Strategy:
      1. Look for thin dark horizontal dividers (gradient peaks) inside the block.
      2. If enough dividers are found, use them.
      3. Otherwise fall back to equal division.
    """
    block_h = y_end - y_start
    if block_h < n * 5:
        return []

    # ── Try to detect row-separator lines via brightness gradient ──
    h_img, w_img = img.shape[:2]
    x0, x1 = int(w_img * 0.30), int(w_img * 0.75)
    region = cv2.cvtColor(img[y_start:y_end, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)

    # Vertical gradient (change between consecutive rows)
    grad = np.abs(np.diff(region, axis=0))
    row_grad = np.mean(grad, axis=1)

    # Smooth
    kernel_size = max(3, block_h // 30)
    row_grad = np.convolve(row_grad, np.ones(kernel_size) / kernel_size, mode="same")

    # We expect (n-1) dividers inside the block
    min_dist = block_h // (n + 2)
    threshold = np.mean(row_grad) + np.std(row_grad) * 0.3

    # Simple local-max peak finder (no scipy needed)
    peaks = []
    for idx in range(min_dist, len(row_grad) - min_dist):
        if row_grad[idx] < threshold:
            continue
        local = row_grad[max(0, idx - min_dist):idx + min_dist + 1]
        if row_grad[idx] == np.max(local):
            # Enforce minimum distance from last accepted peak
            if not peaks or (idx - peaks[-1]) >= min_dist:
                peaks.append(idx)

    # Keep only the best (n-1) peaks
    if len(peaks) >= n - 1:
        peaks = sorted(peaks)[:n - 1]
        boundaries = [0] + peaks + [block_h - 1]
    else:
        # Fallback: equal division
        step = block_h / n
        boundaries = [int(i * step) for i in range(n + 1)]

    rows = []
    for i in range(n):
        ry0 = y_start + boundaries[i]
        ry1 = y_start + boundaries[i + 1]
        rows.append((ry0, ry1, team_id))
    return rows


# ── 5. Cell preprocessing ──────────────────────────────────────────────────────

_MIN_ROW_H = 80   # upscale rows to at least this height before OCR


def _upscale_crop(crop: np.ndarray, min_h: int = _MIN_ROW_H) -> np.ndarray:
    h, w = crop.shape[:2]
    if h < min_h:
        scale = min_h / h
        crop = cv2.resize(crop, (int(w * scale), min_h), interpolation=cv2.INTER_CUBIC)
    return crop


def _prep_digit_cell(crop: np.ndarray) -> np.ndarray:
    """
    Isolate bright-white text on a coloured (teal/red) background.
    Valorant scoreboard numbers are rendered in bright white — a simple
    brightness threshold reliably separates them from the background.
    """
    crop = _upscale_crop(crop)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop

    # Bright-pixel threshold: keep only pixels brighter than 160/255
    _, bw = cv2.threshold(gray, 155, 255, cv2.THRESH_BINARY)

    # If result is mostly white (inverted), flip it
    if cv2.countNonZero(bw) > bw.size * 0.60:
        bw = cv2.bitwise_not(bw)

    # Slight morph cleanup
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, k)

    # Pad for Tesseract
    return cv2.copyMakeBorder(bw, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)


def _prep_text_cell(crop: np.ndarray) -> np.ndarray:
    """Enhance a text (IGN) cell — CLAHE + brightness threshold."""
    crop = _upscale_crop(crop, min_h=_MIN_ROW_H)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
    gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4)).apply(gray)
    # White text on dark-ish background after CLAHE
    _, bw = cv2.threshold(gray, 140, 255, cv2.THRESH_BINARY)
    if cv2.countNonZero(bw) > bw.size * 0.60:
        bw = cv2.bitwise_not(bw)
    return cv2.copyMakeBorder(bw, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)


# ── 6. Tesseract helpers ───────────────────────────────────────────────────────

def _tess(img_gray: np.ndarray, cfg: str, lang: str = "eng") -> str:
    """Run Tesseract on a preprocessed (already binary/gray) numpy array."""
    try:
        pil = Image.fromarray(img_gray)
        return pytesseract.image_to_string(pil, config=cfg, lang=lang).strip()
    except Exception as e:
        log.debug("Tesseract: %s", e)
        return ""


def _ocr_int(crop: np.ndarray) -> int:
    proc = _prep_digit_cell(crop)
    txt  = _tess(proc, _CFG_DIGITS)
    dig  = re.sub(r"\D", "", txt)
    return int(dig) if dig else 0


def _ocr_kda(crop: np.ndarray) -> tuple[int, int, int]:
    proc = _prep_digit_cell(crop)
    txt  = _tess(proc, _CFG_KDA)
    # Normalise common Tesseract misreads in KDA
    txt = (txt
           .replace("l", "/").replace("I", "1")
           .replace("O", "0").replace("|", "/")
           .replace(" ", ""))
    m = re.search(r"(\d+)/(\d+)/(\d+)", txt)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    nums = re.findall(r"\d+", txt)
    if len(nums) >= 3:
        return int(nums[0]), int(nums[1]), int(nums[2])
    if len(nums) == 2:
        return int(nums[0]), int(nums[1]), 0
    if len(nums) == 1:
        return int(nums[0]), 0, 0
    return 0, 0, 0


def _ocr_ign(crop: np.ndarray) -> tuple[str, bool, Optional[str]]:
    """OCR IGN cell. Returns (ign, is_mvp, mvp_type)."""
    proc = _prep_text_cell(crop)
    txt  = _tess(proc, _CFG_TEXT, lang="chi_sim+eng")

    is_mvp   = False
    mvp_type = None
    if "我方" in txt and "最佳" in txt:
        is_mvp, mvp_type = True, "Team MVP"
    elif "敌方" in txt and "最佳" in txt:
        is_mvp, mvp_type = True, "Match MVP"
    elif re.search(r"MVP", txt, re.IGNORECASE):
        is_mvp, mvp_type = True, "Team MVP"

    # Strip badge text and noise
    clean = re.sub(r"(我方|敌方|最佳|MVP)", " ", txt, flags=re.IGNORECASE)
    clean = re.sub(r"\s+", " ", clean).strip()
    # Keep alphanumerics, Chinese chars, and common IGN chars
    clean = re.sub(r"[^\w\u4e00-\u9fff.\-!^~]", "", clean).strip()
    return (clean or "Unknown"), is_mvp, mvp_type


# ── 7. OCR one player row ──────────────────────────────────────────────────────

def _extract_player_row(row_img: np.ndarray, team: str) -> PlayerRowStats:
    """
    `row_img` is a full-width (1920 px) crop of a single player row.
    Each column is sliced by COLS percentage positions.
    """
    w = row_img.shape[1]

    def crop(key: str) -> np.ndarray:
        x0 = int(COLS[key][0] * w)
        x1 = int(COLS[key][1] * w)
        return row_img[:, x0:x1]

    ign, is_mvp, mvp_type  = _ocr_ign(crop("ign"))
    acs                    = _ocr_int(crop("acs"))
    kills, deaths, assists = _ocr_kda(crop("kda"))
    dmg                    = _ocr_int(crop("dmg"))
    fb                     = _ocr_int(crop("fb"))
    plants                 = _ocr_int(crop("plants"))
    defuses                = _ocr_int(crop("defuses"))

    return PlayerRowStats(
        ign=ign, team=team, is_mvp=is_mvp, mvp_type=mvp_type,
        acs=acs, kills=kills, deaths=deaths, assists=assists,
        damage=dmg, first_bloods=fb, plants=plants, defuses=defuses,
    )


# ── 8. Main synchronous pipeline ──────────────────────────────────────────────

def _parse_local(image_bytes: bytes) -> MatchOCRResult:
    if not _CV2_OK:
        return MatchOCRResult(success=False, error="opencv-python-headless not installed.")
    if not _TESS_OK:
        return MatchOCRResult(
            success=False,
            error="Tesseract not installed. Run: sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim",
        )

    t0 = time.perf_counter()

    # ── Load & normalise ──────────────────────────────────────────────────────
    img = _load_and_normalise(image_bytes)
    if img is None:
        return MatchOCRResult(success=False, error="Could not decode image.")

    img = _enhance(img)
    h, w = img.shape[:2]

    # ── Score & metadata ──────────────────────────────────────────────────────
    t1_score, t2_score, outcome, map_name, match_date, duration = (
        _extract_score_and_meta(img)
    )

    # ── Row detection ─────────────────────────────────────────────────────────
    labels = _team_label_per_row(img)
    blocks = _find_team_blocks(labels)

    if not blocks:
        return MatchOCRResult(
            success=False,
            error="Could not detect scoreboard rows — make sure the full end-screen is visible.",
            processing_time_ms=round((time.perf_counter() - t0) * 1000, 1),
        )

    # ── Split each team block → individual player rows ────────────────────────
    all_rows: list[tuple[int, int, int]] = []
    for y_start, y_end, team_id in blocks:
        rows = _split_block_into_rows(y_start, y_end, team_id, img)
        all_rows.extend(rows)

    all_rows.sort(key=lambda r: r[0])
    log.info("Row segmentation: %d blocks → %d player rows detected.", len(blocks), len(all_rows))

    # ── OCR every row ─────────────────────────────────────────────────────────
    result = MatchOCRResult(
        success=True,
        engine="OpenCV+Tesseract (local)",
        map_name=map_name,
        match_date=match_date,
        duration=duration,
        team1_score=t1_score,
        team2_score=t2_score,
        outcome=outcome,
    )

    for y0, y1, team_id in all_rows:
        row_crop = img[y0:y1, :]
        label    = "Team 1 (Green)" if team_id == 1 else "Team 2 (Red)"
        stats    = _extract_player_row(row_crop, label)
        if team_id == 1:
            result.team1_players.append(stats)
        else:
            result.team2_players.append(stats)

    result.processing_time_ms = round((time.perf_counter() - t0) * 1000, 1)
    result.success = len(result.all_players) > 0
    if not result.success:
        result.error = "No players detected. Ensure the screenshot shows the full match scoreboard."

    log.info(
        "OCR done: %d T1 + %d T2 players in %.0f ms",
        len(result.team1_players), len(result.team2_players), result.processing_time_ms,
    )
    return result


# ── Public async wrapper ───────────────────────────────────────────────────────

async def process_match_screenshot(image_bytes: bytes) -> MatchOCRResult:
    """Run the blocking OCR pipeline in a thread pool to keep the event loop free."""
    return await asyncio.get_running_loop().run_in_executor(None, _parse_local, image_bytes)
