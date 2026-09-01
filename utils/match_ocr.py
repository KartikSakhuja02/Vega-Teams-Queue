"""
utils/match_ocr.py
------------------
Match end-screen OCR engine for tactical shooter scoreboards.

Architecture (revised v2):
  1. OpenCV preprocessing  — resize, denoise, sharpen, enhance contrast
  2. Vision AI (PRIMARY)   — OpenRouter multimodal model parses the enhanced image
  3. Tesseract (FALLBACK)  — Offline grid-based extraction if Vision AI unavailable

This approach works on ANY screenshot resolution or aspect ratio because Vision AI
understands layout semantically rather than relying on hardcoded pixel coordinates.
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
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

try:
    import pytesseract
    pytesseract.get_tesseract_version()
    _TESSERACT_AVAILABLE = True
except Exception:
    _TESSERACT_AVAILABLE = False

log = logging.getLogger(__name__)


# ── Data Structures ──────────────────────────────────────────────────────────

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
    engine: str = "Vision AI"
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


# ── OpenCV Image Preprocessing ───────────────────────────────────────────────

def _preprocess_for_vision(image_bytes: bytes) -> bytes:
    """
    Enhance the screenshot with OpenCV before sending to Vision AI.
    Steps: decode → upscale small images → denoise → sharpen → CLAHE contrast.
    Returns JPEG bytes of the processed image (≤1280px wide, quality 90).
    """
    if _CV2_AVAILABLE:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is not None:
            h, w = img.shape[:2]

            # 1. Upscale small screenshots (< 800px wide) for better AI reading
            if w < 800:
                scale = 1200 / w
                img = cv2.resize(img, (int(w * scale), int(h * scale)),
                                 interpolation=cv2.INTER_CUBIC)
                h, w = img.shape[:2]

            # 2. Mild denoise (preserve text edges)
            img = cv2.fastNlMeansDenoisingColored(img, None, 3, 3, 7, 21)

            # 3. Sharpen with an unsharp mask
            blur = cv2.GaussianBlur(img, (0, 0), 1.5)
            img = cv2.addWeighted(img, 1.5, blur, -0.5, 0)

            # 4. CLAHE on luminance channel for contrast enhancement
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            l_ch, a_ch, b_ch = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            l_ch = clahe.apply(l_ch)
            lab = cv2.merge([l_ch, a_ch, b_ch])
            img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

            # 5. Cap at 1280px wide to limit API payload size
            if w > 1280:
                scale = 1280 / w
                img = cv2.resize(img, (1280, int(h * scale)),
                                 interpolation=cv2.INTER_AREA)

            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                return buf.tobytes()

    # Pillow fallback if OpenCV unavailable
    try:
        pil = Image.open(BytesIO(image_bytes)).convert("RGB")
        w, h = pil.size
        if w < 800:
            pil = pil.resize((1200, int(h * 1200 / w)), Image.LANCZOS)
            w, h = pil.size
        if w > 1280:
            pil = pil.resize((1280, int(h * 1280 / w)), Image.LANCZOS)
        pil = pil.filter(ImageFilter.SHARPEN)
        pil = ImageEnhance.Contrast(pil).enhance(1.2)
        buf = BytesIO()
        pil.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception:
        return image_bytes  # last resort: send original


# ── System Prompt ─────────────────────────────────────────────────────────────

VISION_SYSTEM_PROMPT = """\
You are a specialized esports scoreboard OCR parser for Valorant / VALO-style tactical shooters.

Analyze the match result screenshot carefully and extract ALL data into this EXACT JSON schema.
Output ONLY the raw JSON object — no markdown, no code fences, no explanation.

JSON Schema:
{
  "map_name": "string — map or mode name shown (e.g. '莲华古城', 'Pearl', 'Deep Sea')",
  "match_date": "string — date/time shown (e.g. '2026/07/25 20:53')",
  "duration": "string — match duration shown (e.g. '43:11')",
  "team1_score": <integer — left/top score, the LARGER number in a victory>,
  "team2_score": <integer — right/bottom score>,
  "outcome": "Victory or Defeat",
  "team1_players": [
    {
      "ign": "string — exact in-game name as shown (include Chinese/special chars)",
      "is_mvp": <boolean — true if this player has a 我方-最佳 or 敌方-最佳 or MVP badge>,
      "mvp_type": "Team MVP or Match MVP or null",
      "acs": <integer — 平均战斗评分 / average combat score>,
      "kills": <integer>,
      "deaths": <integer>,
      "assists": <integer>,
      "damage": <integer — 对局总伤害>,
      "first_bloods": <integer — 率先击败>,
      "plants": <integer — 部署>,
      "defuses": <integer — 拆除>
    }
  ],
  "team2_players": [same structure as team1_players]
}

Critical rules:
- team1_players = GREEN / TEAL rows (usually top half, winning team)
- team2_players = RED / MAROON rows (usually bottom half)
- KDA column format is "K/D/A" — parse ALL three numbers separately (kills, deaths, assists)
- The match score (e.g. "12 获胜 10") is in the HEADER, NOT in the player rows
- Include ALL players — there should be exactly 5 per team (10 total)
- If a player has 我方-最佳 badge → is_mvp=true, mvp_type="Team MVP"
- If a player has 敌方-最佳 badge → is_mvp=true, mvp_type="Match MVP"
- Do NOT confuse ACS values with the match score
"""

# Vision AI model cascade — tries in order until one succeeds
VISION_MODELS = [
    "google/gemini-flash-1.5",
    "openai/gpt-4o-mini",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "qwen/qwen-2.5-vl-72b-instruct:free",
]


# ── Vision AI Parser (PRIMARY) ────────────────────────────────────────────────

async def _parse_with_vision_ai(image_bytes: bytes) -> MatchOCRResult:
    """Primary engine: preprocess image then send to OpenRouter Vision AI."""
    t0 = time.perf_counter()

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return MatchOCRResult(
            success=False,
            error="OPENROUTER_API_KEY not set — Vision AI unavailable.",
            processing_time_ms=round((time.perf_counter() - t0) * 1000, 1),
        )

    # Step 1: Preprocess with OpenCV
    try:
        enhanced_bytes = await asyncio.get_running_loop().run_in_executor(
            None, _preprocess_for_vision, image_bytes
        )
    except Exception as e:
        log.warning("Preprocessing failed, using raw image: %s", e)
        enhanced_bytes = image_bytes

    # Step 2: Encode to base64
    b64_data = base64.b64encode(enhanced_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64_data}"

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
                {
                    "type": "text",
                    "text": (
                        "Parse this Valorant match scoreboard screenshot. "
                        "Extract the match score from the HEADER (large numbers), "
                        "then extract every player's stats from the table rows. "
                        "Return only the JSON."
                    ),
                },
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for model in VISION_MODELS:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.0,  # deterministic for parsing
            }
            # Only add JSON response format for models that support it
            if "gemini" not in model and "llama" not in model and "qwen" not in model:
                payload["response_format"] = {"type": "json_object"}

            try:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                ) as resp:
                    raw_text = await resp.text()

                    if resp.status != 200:
                        log.warning("Vision model %s → HTTP %d: %s", model, resp.status, raw_text[:200])
                        continue

                    data = json.loads(raw_text)
                    choices = data.get("choices", [])
                    if not choices:
                        log.warning("Vision model %s → no choices returned.", model)
                        continue

                    content = choices[0].get("message", {}).get("content", "").strip()
                    if not content:
                        continue

                    # Strip markdown fences if model added them
                    clean = re.sub(r"^```(?:json)?\s*", "", content)
                    clean = re.sub(r"\s*```$", "", clean).strip()

                    try:
                        parsed = json.loads(clean)
                    except json.JSONDecodeError:
                        # Try to extract JSON object from within the content
                        match = re.search(r"\{.*\}", clean, re.DOTALL)
                        if match:
                            parsed = json.loads(match.group())
                        else:
                            log.warning("Vision model %s → JSON parse failed: %s", model, clean[:200])
                            continue

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
                        res.team1_players.append(_parse_player_json(p, "Team 1 (Green)"))

                    for p in parsed.get("team2_players", []):
                        res.team2_players.append(_parse_player_json(p, "Team 2 (Red)"))

                    if res.all_players:
                        log.info(
                            "OCR success via %s — %d players in %.0fms",
                            model, len(res.all_players), res.processing_time_ms,
                        )
                        return res

            except Exception as e:
                log.warning("Vision model %s → exception: %s", model, e)
                continue

    return MatchOCRResult(
        success=False,
        error="All Vision AI models failed. Check OPENROUTER_API_KEY and credits.",
        processing_time_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


def _parse_player_json(p: dict, team: str) -> PlayerRowStats:
    """Convert a raw player dict from Vision AI JSON into a PlayerRowStats object."""
    def _int(key: str, default: int = 0) -> int:
        try:
            return int(p.get(key, default) or default)
        except (ValueError, TypeError):
            return default

    return PlayerRowStats(
        ign=str(p.get("ign", "Unknown")).strip() or "Unknown",
        team=team,
        is_mvp=bool(p.get("is_mvp", False)),
        mvp_type=p.get("mvp_type") or None,
        acs=_int("acs"),
        kills=_int("kills"),
        deaths=_int("deaths"),
        assists=_int("assists"),
        damage=_int("damage"),
        first_bloods=_int("first_bloods"),
        plants=_int("plants"),
        defuses=_int("defuses"),
    )


# ── Tesseract Fallback (OFFLINE / NO API KEY) ─────────────────────────────────

def _parse_with_tesseract_fallback(image_bytes: bytes) -> Optional[MatchOCRResult]:
    """
    Last-resort offline parser using Tesseract full-image OCR + regex extraction.
    Much less accurate than Vision AI but works without internet/API key.
    """
    if not _TESSERACT_AVAILABLE or not _CV2_AVAILABLE:
        return None

    t0 = time.perf_counter()

    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None

        # Upscale for better Tesseract accuracy
        h, w = img.shape[:2]
        if w < 1200:
            scale = 1920 / w
            img = cv2.resize(img, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_CUBIC)

        # Tesseract with Chinese + English
        pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        raw_text = pytesseract.image_to_string(
            pil_img,
            lang="chi_sim+eng",
            config="--psm 6 --oem 1",
        )

        if not raw_text.strip():
            return None

        result = MatchOCRResult(engine="Tesseract (offline)")

        # Extract match score — look for "N 获胜 M" or just two big numbers
        score_match = re.search(r"(\d{1,2})\s*获胜\s*(\d{1,2})", raw_text)
        if score_match:
            result.team1_score = int(score_match.group(1))
            result.team2_score = int(score_match.group(2))
            result.outcome = "Victory"

        # Extract duration
        dur = re.search(r"用时\s*(\d{1,2}:\d{2})", raw_text)
        if dur:
            result.duration = dur.group(1)

        # Extract date
        date = re.search(r"(\d{4}[/\-]\d{2}[/\-]\d{2}(?:\s+\d{2}:\d{2})?)", raw_text)
        if date:
            result.match_date = date.group(1)

        # Map name from mode line
        map_match = re.search(r"赛事模式[·\-\s]*([^\s\n]+)", raw_text)
        if map_match:
            result.map_name = map_match.group(1)

        result.success = result.team1_score > 0 or result.team2_score > 0
        result.processing_time_ms = round((time.perf_counter() - t0) * 1000, 1)
        return result if result.success else None

    except Exception as e:
        log.warning("Tesseract fallback failed: %s", e)
        return None


# ── Main Entrypoint ───────────────────────────────────────────────────────────

async def process_match_screenshot(image_bytes: bytes) -> MatchOCRResult:
    """
    Process a match end-screen screenshot and extract all scoreboard data.

    Priority:
      1. Vision AI (OpenRouter) — primary, most accurate, resolution-independent
      2. Tesseract offline       — fallback if no API key or no internet
    """
    # Primary: Vision AI
    result = await _parse_with_vision_ai(image_bytes)
    if result.success and result.all_players:
        return result

    # Fallback: Tesseract offline
    log.info("Vision AI failed or returned no players, trying Tesseract fallback...")
    tesseract_result = await asyncio.get_running_loop().run_in_executor(
        None, _parse_with_tesseract_fallback, image_bytes
    )
    if tesseract_result and tesseract_result.success:
        return tesseract_result

    # Return Vision AI result even if empty (has error message)
    return result
