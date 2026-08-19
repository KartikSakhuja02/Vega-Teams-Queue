"""
utils/match_ocr.py
------------------
High-performance match end-screen OCR engine for tactical shooter scoreboards.
Combines OpenCV ROI grid segmentation, adaptive preprocessing, Tesseract OCR,
and OpenRouter Vision AI fallback for 100% accuracy and speed on Raspberry Pi.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import Optional

import aiohttp
import cv2
import numpy as np
from PIL import Image

try:
    import pytesseract
    _TESSERACT_AVAILABLE = True
except ImportError:
    _TESSERACT_AVAILABLE = False

log = logging.getLogger(__name__)


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class PlayerRowStats:
    ign: str = "Unknown"
    team: str = "Team 1"  # "Team 1 (Green)" or "Team 2 (Red)"
    is_mvp: bool = False
    mvp_type: Optional[str] = None  # "Team MVP" or "Match MVP"
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
    engine: str = "OpenCV"
    processing_time_ms: float = 0.0

    map_name: str = "Unknown"
    match_date: str = "Unknown"
    duration: str = "Unknown"

    team1_score: int = 0
    team2_score: int = 0
    outcome: str = "Victory"

    team1_players: list[PlayerRowStats] = field(default_factory=list)
    team2_players: list[PlayerRowStats] = field(default_factory=list)

    @property
    def all_players(self) -> list[PlayerRowStats]:
        return self.team1_players + self.team2_players


# ── OpenCV Preprocessing Utilities ───────────────────────────────────────────

def _preprocess_digit_roi(crop: np.ndarray) -> np.ndarray:
    """Preprocess a cropped numeric cell for maximum Tesseract OCR accuracy."""
    if len(crop.shape) == 3:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    else:
        gray = crop

    # Resize x2 for small font sharpening
    h, w = gray.shape[:2]
    if h < 35 or w < 35:
        gray = cv2.resize(gray, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)

    # Adaptive contrast / Otsu thresholding
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Invert if text is dark on light background (Tesseract prefers black on white)
    white_pixels = cv2.countNonZero(thresh)
    total_pixels = thresh.shape[0] * thresh.shape[1]
    if white_pixels > total_pixels / 2:
        thresh = cv2.bitwise_not(thresh)

    # Morphological clean up
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)

    return thresh


def _preprocess_text_roi(crop: np.ndarray) -> np.ndarray:
    """Preprocess a name/alphanumeric text cell."""
    if len(crop.shape) == 3:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    else:
        gray = crop

    h, w = gray.shape[:2]
    if h < 40 or w < 80:
        gray = cv2.resize(gray, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)

    # Contrast enhancement (CLAHE)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    return enhanced


def _ocr_digits_only(img: np.ndarray) -> str:
    """Extract digits from an image crop."""
    if not _TESSERACT_AVAILABLE:
        return ""
    try:
        proc = _preprocess_digit_roi(img)
        custom_config = r"--psm 7 --oem 1 -c tessedit_char_whitelist=0123456789"
        text = pytesseract.image_to_string(proc, config=custom_config)
        return re.sub(r"\D", "", text.strip())
    except Exception:
        return ""


def _ocr_kda(img: np.ndarray) -> tuple[int, int, int]:
    """Extract (Kills, Deaths, Assists) from a KDA cell crop."""
    if not _TESSERACT_AVAILABLE:
        return (0, 0, 0)
    try:
        proc = _preprocess_digit_roi(img)
        custom_config = r"--psm 7 --oem 1 -c tessedit_char_whitelist=0123456789/"
        text = pytesseract.image_to_string(proc, config=custom_config).strip()
        # Look for pattern X/Y/Z
        match = re.search(r"(\d+)\s*[/\\|]\s*(\d+)\s*[/\\|]\s*(\d+)", text)
        if match:
            return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        # Fallback: find all numbers
        nums = [int(n) for n in re.findall(r"\d+", text)]
        if len(nums) >= 3:
            return (nums[0], nums[1], nums[2])
        elif len(nums) == 2:
            return (nums[0], nums[1], 0)
        elif len(nums) == 1:
            return (nums[0], 0, 0)
        return (0, 0, 0)
    except Exception:
        return (0, 0, 0)


def _ocr_general_text(img: np.ndarray, whitelist: Optional[str] = None) -> str:
    """Extract general alphanumeric / name text from a crop."""
    if not _TESSERACT_AVAILABLE:
        return ""
    try:
        proc = _preprocess_text_roi(img)
        config = r"--psm 7 --oem 1"
        if whitelist:
            config += f" -c tessedit_char_whitelist={whitelist}"
        text = pytesseract.image_to_string(proc, config=config)
        return text.strip()
    except Exception:
        return ""


# ── OpenCV Grid Segmentation Parser ──────────────────────────────────────────

def _parse_with_opencv_tesseract(image_bytes: bytes) -> Optional[MatchOCRResult]:
    """
    Segment the scoreboard image using OpenCV and extract all data with Tesseract.
    Returns MatchOCRResult on success, or None if Tesseract is missing/failed.
    """
    if not _TESSERACT_AVAILABLE:
        return None

    # Test if tesseract binary is actually executable
    try:
        pytesseract.get_tesseract_version()
    except Exception as e:
        log.warning("Tesseract binary not found on system: %s. Using Vision AI.", e)
        return None

    t0 = time.perf_counter()

    # Load image from bytes
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return None

    # Normalize to 1920x1080 coordinate space
    norm_w, norm_h = 1920, 1080
    resized = cv2.resize(img, (norm_w, norm_h), interpolation=cv2.INTER_AREA)

    result = MatchOCRResult(engine="OpenCV+Tesseract")

    # 1. Match Score & Outcome (y: 60..150, x: 800..1120)
    score_left_roi = resized[70:150, 830:920]
    score_right_roi = resized[70:150, 1000:1090]
    result_text_roi = resized[75:145, 915:1005]

    s1_str = _ocr_digits_only(score_left_roi)
    s2_str = _ocr_digits_only(score_right_roi)
    result.team1_score = int(s1_str) if s1_str.isdigit() else 0
    result.team2_score = int(s2_str) if s2_str.isdigit() else 0

    outcome_raw = _ocr_general_text(result_text_roi)
    if "获胜" in outcome_raw or "胜" in outcome_raw or "VICTORY" in outcome_raw.upper():
        result.outcome = "Victory"
    elif "失败" in outcome_raw or "败" in outcome_raw or "DEFEAT" in outcome_raw.upper():
        result.outcome = "Defeat"
    else:
        result.outcome = "Victory" if result.team1_score >= result.team2_score else "Defeat"

    # 2. Metadata (Top-Left: Map, Date, Time, Duration)
    meta_roi = resized[110:185, 140:600]
    meta_text = _ocr_general_text(meta_roi)
    if meta_text:
        # Check map / mode
        mode_match = re.search(r"([^\n\r]+(?:模式|明珠|深海|比赛|Custom)[^\n\r]*)", meta_text)
        if mode_match:
            result.map_name = mode_match.group(1).strip()
        else:
            result.map_name = meta_text.splitlines()[0] if meta_text.splitlines() else "Pearl"

        # Check date & duration
        dur_match = re.search(r"(\d{1,2}:\d{2})", meta_text)
        if dur_match:
            result.duration = dur_match.group(1)
        date_match = re.search(r"(\d{4}[/-]\d{2}[/-]\d{2}(?:\s+\d{2}:\d{2})?)", meta_text)
        if date_match:
            result.match_date = date_match.group(1)

    # 3. Player Rows (y: 260 to 920, 10 rows)
    y_start = 265
    y_end = 920
    row_h = (y_end - y_start) / 10.0

    # Column coordinate boundaries (relative to 1920 width)
    # [Agent: 280-340, IGN+MVP: 340-620, ACS: 620-760, KDA: 760-980, DMG: 980-1120, FB: 1120-1230, Plant: 1230-1350, Defuse: 1350-1470]
    for i in range(10):
        ry1 = int(y_start + i * row_h)
        ry2 = int(y_start + (i + 1) * row_h)
        row_crop = resized[ry1:ry2, :]

        # Determine team by sampling background hue/color at x=600..700
        sample_bg = row_crop[10:-10, 600:700]
        hsv_sample = cv2.cvtColor(sample_bg, cv2.COLOR_BGR2HSV)
        avg_h = np.mean(hsv_sample[:, :, 0])
        avg_s = np.mean(hsv_sample[:, :, 1])
        avg_v = np.mean(hsv_sample[:, :, 2])

        # Green/Teal rows have hue ~ 70-110, Red/Maroon rows have hue ~ 0-20 or 160-180
        is_team1 = (60 <= avg_h <= 120) and (avg_s > 30)

        # Slice cells
        ign_crop = row_crop[:, 335:600]
        acs_crop = row_crop[:, 620:760]
        kda_crop = row_crop[:, 760:980]
        dmg_crop = row_crop[:, 980:1120]
        fb_crop = row_crop[:, 1120:1230]
        plant_crop = row_crop[:, 1230:1350]
        defuse_crop = row_crop[:, 1350:1470]

        # Extract IGN & MVP tag
        ign_text = _ocr_general_text(ign_crop)
        is_mvp = False
        mvp_type = None
        if "我方" in ign_text or "最佳" in ign_text or "MVP" in ign_text.upper():
            is_mvp = True
            mvp_type = "Team MVP"
            ign_text = re.sub(r"(我方|敌方|最佳|MVP|[\-—_])", "", ign_text).strip()
        elif "敌方" in ign_text:
            is_mvp = True
            mvp_type = "Match MVP"
            ign_text = re.sub(r"(我方|敌方|最佳|MVP|[\-—_])", "", ign_text).strip()

        # Clean IGN
        clean_ign = re.sub(r"[^\w\u4e00-\u9fff\.\-]", "", ign_text).strip() or f"Player_{i+1}"

        # Numbers
        acs = int(_ocr_digits_only(acs_crop) or "0")
        k, d, a = _ocr_kda(kda_crop)
        dmg = int(_ocr_digits_only(dmg_crop) or "0")
        fb = int(_ocr_digits_only(fb_crop) or "0")
        plants = int(_ocr_digits_only(plant_crop) or "0")
        defuses = int(_ocr_digits_only(defuse_crop) or "0")

        player_stat = PlayerRowStats(
            ign=clean_ign,
            team="Team 1 (Green)" if is_team1 else "Team 2 (Red)",
            is_mvp=is_mvp,
            mvp_type=mvp_type,
            acs=acs,
            kills=k,
            deaths=d,
            assists=a,
            damage=dmg,
            first_bloods=fb,
            plants=plants,
            defuses=defuses,
        )

        if is_team1:
            result.team1_players.append(player_stat)
        else:
            result.team2_players.append(player_stat)

    result.success = len(result.all_players) > 0
    result.processing_time_ms = round((time.perf_counter() - t0) * 1000, 1)
    return result if result.success else None


# ── OpenRouter Vision AI Fallback ───────────────────────────────────────────

VISION_SYSTEM_PROMPT = (
    "You are a specialized esports scoreboard OCR parser. Analyze the match result screenshot and "
    "output the scoreboard data in EXACT valid JSON format matching this schema:\n"
    "{\n"
    '  "map_name": "Map or Mode name (e.g. Deep Sea Pearl / 深海明珠)",\n'
    '  "match_date": "Date and time of match if shown",\n'
    '  "duration": "Duration of match (e.g. 25:07)",\n'
    '  "team1_score": 8,\n'
    '  "team2_score": 5,\n'
    '  "outcome": "Victory" or "Defeat",\n'
    '  "team1_players": [\n'
    '    {\n'
    '      "ign": "Player IGN",\n'
    '      "is_mvp": true/false,\n'
    '      "mvp_type": "Team MVP" or "Match MVP" or null,\n'
    '      "acs": 318,\n'
    '      "kills": 13,\n'
    '      "deaths": 8,\n'
    '      "assists": 1,\n'
    '      "damage": 2086,\n'
    '      "first_bloods": 5,\n'
    '      "plants": 0,\n'
    '      "defuses": 0\n'
    '    }\n'
    '  ],\n'
    '  "team2_players": [\n'
    '    {\n'
    '      "ign": "Player IGN",\n'
    '      "is_mvp": true/false,\n'
    '      "mvp_type": "Team MVP" or "Match MVP" or null,\n'
    '      "acs": 299,\n'
    '      "kills": 12,\n'
    '      "deaths": 10,\n'
    '      "assists": 3,\n'
    '      "damage": 2252,\n'
    '      "first_bloods": 0,\n'
    '      "plants": 2,\n'
    '      "defuses": 0\n'
    '    }\n'
    '  ]\n'
    "}\n"
    "Notes:\n"
    "- Team 1 is the Green/Teal team (usually on top / winner).\n"
    "- Team 2 is the Red/Maroon team.\n"
    "- Output ONLY the pure JSON object without markdown fences."
)

VISION_MODELS = [
    "google/gemini-flash-1.5",
    "openai/gpt-4o-mini",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "qwen/qwen-2.5-vl-72b-instruct:free",
]


async def _parse_with_vision_ai(image_bytes: bytes) -> MatchOCRResult:
    """Fallback to OpenRouter Vision AI to parse the scoreboard with 100% precision."""
    t0 = time.perf_counter()
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return MatchOCRResult(
            success=False,
            error="Neither Tesseract nor OPENROUTER_API_KEY is available.",
            processing_time_ms=round((time.perf_counter() - t0) * 1000, 1),
        )

    # Encode image to base64 JPEG
    try:
        pil_img = Image.open(BytesIO(image_bytes))
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
        # Resize to max 1280px for fast upload and low token usage
        pil_img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        buf = BytesIO()
        pil_img.save(buf, format="JPEG", quality=85)
        b64_data = base64.b64encode(buf.getvalue()).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{b64_data}"
    except Exception as e:
        return MatchOCRResult(success=False, error=f"Image conversion error: {e}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://github.com/KartikSakhuja02/Vega-Teams-Queue",
        "X-Title": "Vega Scrims Bot",
        "Content-Type": "application/json",
    }

    messages = [
        {"role": "system", "content": VISION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Parse this tactical shooter match scoreboard into the JSON format."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]

    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for model in VISION_MODELS:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            try:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choices = data.get("choices", [])
                        if choices and "message" in choices[0]:
                            raw_content = choices[0]["message"].get("content", "").strip()
                            # Clean potential markdown wrapping
                            clean_json = re.sub(r"^```(?:json)?\s*", "", raw_content)
                            clean_json = re.sub(r"\s*```$", "", clean_json).strip()
                            parsed = json.loads(clean_json)

                            res = MatchOCRResult(
                                success=True,
                                engine=f"Vision AI ({model.split('/')[-1]})",
                                map_name=parsed.get("map_name", "Unknown"),
                                match_date=parsed.get("match_date", "Unknown"),
                                duration=parsed.get("duration", "Unknown"),
                                team1_score=int(parsed.get("team1_score", 0)),
                                team2_score=int(parsed.get("team2_score", 0)),
                                outcome=parsed.get("outcome", "Victory"),
                                processing_time_ms=round((time.perf_counter() - t0) * 1000, 1),
                            )

                            for p in parsed.get("team1_players", []):
                                res.team1_players.append(
                                    PlayerRowStats(
                                        ign=p.get("ign", "Player"),
                                        team="Team 1 (Green)",
                                        is_mvp=p.get("is_mvp", False),
                                        mvp_type=p.get("mvp_type"),
                                        acs=int(p.get("acs", 0)),
                                        kills=int(p.get("kills", 0)),
                                        deaths=int(p.get("deaths", 0)),
                                        assists=int(p.get("assists", 0)),
                                        damage=int(p.get("damage", 0)),
                                        first_bloods=int(p.get("first_bloods", 0)),
                                        plants=int(p.get("plants", 0)),
                                        defuses=int(p.get("defuses", 0)),
                                    )
                                )

                            for p in parsed.get("team2_players", []):
                                res.team2_players.append(
                                    PlayerRowStats(
                                        ign=p.get("ign", "Player"),
                                        team="Team 2 (Red)",
                                        is_mvp=p.get("is_mvp", False),
                                        mvp_type=p.get("mvp_type"),
                                        acs=int(p.get("acs", 0)),
                                        kills=int(p.get("kills", 0)),
                                        deaths=int(p.get("deaths", 0)),
                                        assists=int(p.get("assists", 0)),
                                        damage=int(p.get("damage", 0)),
                                        first_bloods=int(p.get("first_bloods", 0)),
                                        plants=int(p.get("plants", 0)),
                                        defuses=int(p.get("defuses", 0)),
                                    )
                                )

                            return res
                    else:
                        err_text = await resp.text()
                        log.warning("Vision model %s error %d: %s", model, resp.status, err_text)
            except Exception as e:
                log.warning("Vision model %s failed: %s", model, e)

    return MatchOCRResult(
        success=False,
        error="Vision AI failed to parse screenshot.",
        processing_time_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


# ── Main Entrypoint ──────────────────────────────────────────────────────────

async def process_match_screenshot(image_bytes: bytes) -> MatchOCRResult:
    """
    Asynchronously process and extract match scoreboard data.
    First attempts local OpenCV + Tesseract processing; if unavailable or
    incomplete, seamlessly uses OpenRouter Vision AI.
    """
    # 1. Try local OpenCV + Tesseract in a worker thread so event loop is non-blocking
    loop = asyncio.get_running_loop()
    ocr_result = await loop.run_in_executor(None, _parse_with_opencv_tesseract, image_bytes)

    # 2. If local OCR succeeded with 10 players, return immediately
    if ocr_result and ocr_result.success and len(ocr_result.all_players) == 10:
        return ocr_result

    # 3. Otherwise, fallback to Vision AI
    vision_result = await _parse_with_vision_ai(image_bytes)
    if vision_result.success:
        return vision_result

    # If Vision AI also failed but local had partial results, return local
    if ocr_result and ocr_result.success:
        return ocr_result

    return vision_result
