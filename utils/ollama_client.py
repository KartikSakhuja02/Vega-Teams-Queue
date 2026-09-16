"""
utils/ollama_client.py
-----------------------
Async Ollama client for local vision AI inference.

Uses aiohttp (already in requirements) to call Ollama's REST API directly.
No extra dependencies required.

Environment variables
---------------------
  OLLAMA_BASE_URL      URL of your Ollama server.  Default: http://127.0.0.1:11434
  OLLAMA_MODEL         Model to use for vision.     Default: gemma3:4b
  OLLAMA_TIMEOUT       Request timeout in seconds.  Default: 180
  OLLAMA_MAX_TOKENS    Max tokens in response.      Default: 2048

Important networking note
--------------------------
If the Discord bot is hosted on Railway, Railway's localhost is NOT your PC.
Run ngrok with:  ngrok http --host-header=localhost:11434 11434
Set OLLAMA_BASE_URL to the ngrok HTTPS URL in Railway's env vars.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from typing import Optional

import aiohttp
from PIL import Image

from utils.ocr.models import MatchOCRResult, PlayerRowStats

log = logging.getLogger(__name__)

# ── Config (read once at import — changes require bot restart) ────────────────
_BASE_URL   = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
_MODEL      = os.getenv("OLLAMA_MODEL",    "qwen2.5vl:3b")
_TIMEOUT    = int(os.getenv("OLLAMA_TIMEOUT",    "180"))
_MAX_TOKENS = int(os.getenv("OLLAMA_MAX_TOKENS", "2048"))
_NUM_CTX    = int(os.getenv("OLLAMA_NUM_CTX",    "8192"))

# GPU concurrency guard: RTX 2050 / 4 GB VRAM → 1 vision inference at a time.
inference_semaphore = asyncio.Semaphore(1)

# Supported MIME types
SUPPORTED_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}

# Default prompt when user sends no text with the image
DEFAULT_PROMPT = (
    "Analyze this image carefully. "
    "Describe what you see and identify important text, errors, warnings, "
    "UI elements, or other relevant information."
)

# Headers sent with every request — required for ngrok tunnels
_HEADERS = {
    "User-Agent": "ollama-discord-bot/1.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "ngrok-skip-browser-warning": "true",
}

# ── Scoreboard OCR prompt (same as OpenRouter) ────────────────────────────────
_SCOREBOARD_PROMPT = """\
You are analyzing a Valorant Mobile (CN version) custom match end-screen scoreboard.

Return ONLY a valid JSON object. No explanation, no preamble, no markdown fences.

Layout:
- TOP CENTER: "N 获胜 M" → team1_score=N, team2_score=M
- TOP LEFT: map name after "赛事模式-" (e.g. 莲华古城, 深海明珠, 源工重镇, 亚海悬城, 微风岛屿)
- TOP LEFT: date "YYYY/MM/DD HH:MM" and duration "用时 MM:SS"
- TABLE: 10 player rows total.
  IMPORTANT - TEAM ASSIGNMENT:
  The table may be sorted by individual score ("个人排名"), so green and red rows are INTERLEAVED.
  Every match is 5v5 — there must be EXACTLY 5 players on team 1 and EXACTLY 5 players on team 2.
  Determine team for each row by background color:
  • GREEN / TEAL row = team 1 (Friendly / 我方)
  • RED / MAROON row = team 2 (Enemy / 敌方)
  • GOLD / YELLOW row = the viewer's highlighted row. Assign this player to whichever team needs to reach 5 players.
  • "我方-最佳" (Team MVP) is on Team 1. "敌方-最佳" (Enemy MVP) is on Team 2.
  Columns: 排名/头像/IGN | 平均战斗评分(ACS) | 击败/败阵/助攻(K/D/A) | 对局总伤害(damage) | 率先击败(first_bloods) | 部署(plants) | 拆除(defuses)

Return exactly this JSON (no extra keys):
{
  "success": true,
  "team1_score": <int or null>,
  "team2_score": <int or null>,
  "map": "<map name only>",
  "match_date": "<YYYY/MM/DD HH:MM or null>",
  "duration": "<MM:SS or null>",
  "outcome": "Victory or Defeat",
  "players": [
    {
      "name": "<exact name>",
      "team": <1 or 2>,
      "is_mvp": <true/false>,
      "mvp_type": <"Team MVP" or "Match MVP" or null>,
      "acs": <int or null>,
      "kills": <int or null>,
      "deaths": <int or null>,
      "assists": <int or null>,
      "damage": <int or null>,
      "first_bloods": <int or null>,
      "plants": <int or null>,
      "defuses": <int or null>
    }
  ]
}

Rules:
1. players must contain all 10 players from the table.
2. EXACTLY 5 players must have team=1, and EXACTLY 5 players must have team=2.
3. K/D/A format is kills/deaths/assists separated by "/".
4. Use null for any number you cannot read confidently. Never guess.
5. Return ONLY the JSON object. Nothing before or after.
"""


def is_configured() -> bool:
    """True when OLLAMA_BASE_URL is set (non-default) OR we're running locally."""
    return bool(os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_MODEL"))


async def check_connection() -> tuple[bool, str]:
    """
    Ping Ollama and verify the configured model is present.
    Returns (ok: bool, message: str).
    """
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout, headers=_HEADERS) as session:
            async with session.get(f"{_BASE_URL}/api/tags") as resp:
                if resp.status != 200:
                    return False, f"Ollama returned HTTP {resp.status}"
                data = await resp.json()
                models = [m.get("name", "") for m in data.get("models", [])]
                if _MODEL not in models:
                    return False, (
                        f"Model '{_MODEL}' not found in Ollama. "
                        f"Available: {', '.join(models) or 'none'}"
                    )
                return True, f"Ollama OK — model '{_MODEL}' ready"
    except aiohttp.ClientConnectorError:
        return False, f"Ollama not reachable at {_BASE_URL}"
    except asyncio.TimeoutError:
        return False, f"Ollama connection timed out at {_BASE_URL}"
    except Exception as exc:
        return False, f"Ollama check failed: {exc}"


def _prepare_image(image_bytes: bytes, max_dim: int = 1920) -> bytes:
    """Resize huge screenshots to max 1080p equivalent to keep token count fast and within context."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
            if max(w, h) > max_dim:
                ratio = max_dim / max(w, h)
                new_size = (int(w * ratio), int(h * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
                out = io.BytesIO()
                fmt = img.format if img.format in ("PNG", "JPEG", "WEBP") else "JPEG"
                img.save(out, format=fmt, quality=95)
                return out.getvalue()
    except Exception as exc:
        log.debug("Image resize error (ignored): %s", exc)
    return image_bytes


async def _call_ollama(image_bytes: bytes, prompt: str, json_format: bool = False) -> str:
    """Low-level: send image+prompt to Ollama, return raw text content."""
    image_bytes = _prepare_image(image_bytes)
    image_b64 = base64.b64encode(image_bytes).decode()

    payload: dict = {
        "model": _MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [image_b64],
            }
        ],
        "stream": False,
        "options": {
            "num_ctx": _NUM_CTX,
            "num_predict": _MAX_TOKENS,
            "temperature": 0.05,
        },
    }
    if json_format:
        payload["format"] = "json"

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=_HEADERS) as session:
            async with session.post(
                f"{_BASE_URL}/api/chat",
                json=payload,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Ollama returned HTTP {resp.status}: {body[:300]}"
                    )
                data = await resp.json()

    except aiohttp.ClientConnectorError as exc:
        raise RuntimeError(
            f"Cannot connect to Ollama at {_BASE_URL}. Is Ollama running?"
        ) from exc
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Ollama timed out after {_TIMEOUT}s."
        ) from exc

    msg = data.get("message") or {}
    content = msg.get("content", "").strip()
    if not content:
        raise RuntimeError(
            f"Ollama returned empty response. "
            f"done={data.get('done')}, done_reason={data.get('done_reason')}"
        )
    return content


async def analyze_image(image_bytes: bytes, prompt: str = DEFAULT_PROMPT) -> str:
    """Send an image to Ollama for general vision analysis. Returns plain text."""
    return await _call_ollama(image_bytes, prompt)


# ── Scoreboard OCR helpers (mirrors openrouter_client.py) ────────────────────

def _extract_json(text: str) -> dict:
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No valid JSON in response: {text[:300]!r}")


def _clean_int(v) -> int:
    if v is None:
        return 0
    if isinstance(v, str) and "/" in v:
        return _clean_int(v.split("/")[0])
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _confidence(players: list[dict]) -> float:
    FIELDS = ("acs", "kills", "deaths", "assists", "damage")
    total  = len(players) * len(FIELDS)
    if total == 0:
        return 0.0
    nulls      = sum(1 for p in players for f in FIELDS if p.get(f) is None)
    null_ratio = nulls / total
    player_ok  = 1.0 if len(players) == 10 else 0.5
    return round(min(1.0, (1.0 - null_ratio) * player_ok), 3)


def _detect_row_teams(image_bytes: bytes) -> list[int]:
    """
    Sample row background colors to detect whether each of the 10 rows belongs
    to Team 1 (Teal/Green) or Team 2 (Red/Maroon).
    Handles yellow/gold active player highlight row.
    Returns list of 10 ints (1 or 2), or [] if not detectable.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            w, h = img.size
            y_start = int(0.28 * h)
            y_end = int(0.92 * h)
            row_h = (y_end - y_start) / 10

            row_types = []
            for i in range(10):
                cy = int(y_start + (i + 0.5) * row_h)
                votes_teal = 0
                votes_red = 0
                for frac_x in (0.25, 0.35, 0.45, 0.55):
                    sx = int(frac_x * w)
                    pixels = [
                        img.getpixel((min(w - 1, max(0, sx + dx)), min(h - 1, max(0, cy + dy))))
                        for dx in (-3, 0, 3) for dy in (-3, 0, 3)
                    ]
                    r = sum(p[0] for p in pixels) / len(pixels)
                    g = sum(p[1] for p in pixels) / len(pixels)
                    b = sum(p[2] for p in pixels) / len(pixels)

                    if g > r + 15 and b > r + 10:
                        votes_teal += 1
                    elif r > g + 15:
                        votes_red += 1

                if votes_teal > votes_red and votes_teal >= 2:
                    row_types.append(1)
                elif votes_red > votes_teal and votes_red >= 2:
                    row_types.append(2)
                else:
                    row_types.append(0)  # Gold highlight or ambiguous

            c1 = row_types.count(1)
            c2 = row_types.count(2)
            if (c1 + c2) >= 7:
                teams = []
                for t in row_types:
                    if t == 1:
                        teams.append(1)
                    elif t == 2:
                        teams.append(2)
                    else:
                        if c1 < 5:
                            teams.append(1)
                            c1 += 1
                        else:
                            teams.append(2)
                            c2 += 1
                if teams.count(1) == 5 and teams.count(2) == 5:
                    return teams
    except Exception as exc:
        log.debug("Row color detection exception: %s", exc)
    return []


def _to_result(data: dict, elapsed_ms: float, image_bytes: Optional[bytes] = None) -> MatchOCRResult:
    players_raw = data.get("players") or []

    # 1. Ground-truth color-assisted team detection
    if image_bytes and len(players_raw) == 10:
        detected_teams = _detect_row_teams(image_bytes)
        if detected_teams and len(detected_teams) == 10:
            log.info("Applying color-detected row teams: %s", detected_teams)
            for p, team_id in zip(players_raw, detected_teams):
                p["team"] = team_id

    # 2. Strict 5v5 validation and auto-balancer safeguard
    t1 = [p for p in players_raw if p.get("team") == 1]
    t2 = [p for p in players_raw if p.get("team") == 2]

    if len(players_raw) == 10 and (len(t1) != 5 or len(t2) != 5):
        log.warning("Unbalanced teams detected (%d vs %d), enforcing 5v5 balance", len(t1), len(t2))
        if len(t1) < 5:
            needed = 5 - len(t1)
            for p in reversed(t2):
                if needed <= 0:
                    break
                p["team"] = 1
                needed -= 1
        elif len(t2) < 5:
            needed = 5 - len(t2)
            for p in reversed(t1):
                if needed <= 0:
                    break
                p["team"] = 2
                needed -= 1
        t1 = [p for p in players_raw if p.get("team") == 1]
        t2 = [p for p in players_raw if p.get("team") == 2]

    def _make(p: dict, team_label: str) -> PlayerRowStats:
        kills = deaths = assists = 0
        raw_k = p.get("kills")
        if isinstance(raw_k, str) and "/" in raw_k:
            parts = raw_k.split("/")
            kills, deaths, assists = _clean_int(parts[0]), _clean_int(parts[1]), _clean_int(parts[2])
        else:
            kills   = _clean_int(p.get("kills"))
            deaths  = _clean_int(p.get("deaths"))
            assists = _clean_int(p.get("assists"))
        c = 0.8
        return PlayerRowStats(
            ign=str(p.get("name") or "Unknown").strip(),
            team=team_label,
            is_mvp=bool(p.get("is_mvp")),
            mvp_type=p.get("mvp_type"),
            acs=_clean_int(p.get("acs")),
            kills=kills, deaths=deaths, assists=assists,
            damage=_clean_int(p.get("damage")),
            first_bloods=_clean_int(p.get("first_bloods")),
            plants=_clean_int(p.get("plants")),
            defuses=_clean_int(p.get("defuses")),
            ign_conf=c, acs_conf=c, kda_conf=c,
            dmg_conf=c, fb_conf=c, plants_conf=c, defuses_conf=c,
        )

    t1_players   = [_make(p, "Team 1") for p in t1]
    t2_players   = [_make(p, "Team 2") for p in t2]
    conf         = _confidence(players_raw)
    needs_review = conf < 0.60 or len(players_raw) != 10

    return MatchOCRResult(
        success=True,
        engine=f"Ollama/{_MODEL}",
        processing_time_ms=elapsed_ms,
        confidence=conf,
        needs_review=needs_review,
        map_name=str(data.get("map") or "Unknown"),
        match_date=str(data.get("match_date") or "Unknown"),
        duration=str(data.get("duration") or "Unknown"),
        team1_score=_clean_int(data.get("team1_score")),
        team2_score=_clean_int(data.get("team2_score")),
        outcome=str(data.get("outcome") or "Unknown"),
        team1_players=t1_players,
        team2_players=t2_players,
    )


async def extract_scoreboard(image_bytes: bytes) -> MatchOCRResult:
    """
    Parse a Valorant match scoreboard screenshot → MatchOCRResult.
    Uses the same JSON prompt as openrouter_client.py.
    Raises RuntimeError on failure so match_ocr.py can fall back.
    """
    if not is_configured():
        raise RuntimeError("OLLAMA_BASE_URL / OLLAMA_MODEL not configured")

    t0 = time.monotonic()
    raw_text = await _call_ollama(image_bytes, _SCOREBOARD_PROMPT, json_format=True)
    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    log.debug("Ollama scoreboard raw (first 400): %s", raw_text[:400])
    parsed = _extract_json(raw_text)
    return _to_result(parsed, elapsed_ms, image_bytes=image_bytes)


_PROFILE_IGN_PROMPT = """\
You are an expert game profile OCR assistant specializing in Valorant Mobile (无畏契约手游).
Analyze this player profile overview screenshot.

Extract the player's primary In-Game Name (IGN) / username:
- The username can be in Chinese characters (汉字, e.g. "棠槽"), English letters (e.g. "klein-"), numbers, or mixed.
- Location:
  1. Find the player avatar / level badge (e.g. Lv. 150 or Lv. 464) in the upper-left of the profile card.
  2. Directly to the right of the avatar (and after any small gender icon ♂/♀ or VIP badge), is the player's bold USERNAME (for example: "棠槽" or "klein-").
  3. IMPORTANT - EXCLUSIONS:
     • Directly underneath the username, there is a grey rounded box with user-written signature/status text (e.g. "发你麻个枪", "rust", followed by "+ 添加语音"). DO NOT extract this signature/status box!
     • Do NOT extract "+ 添加语音", account ID ("编号"), level numbers, or rank text ("神话", "超凡", "铂金", etc.).
- Output ONLY the player's primary username itself.

Return ONLY a valid JSON object in this exact format:
{
  "ign": "<exact player username>"
}
"""


async def extract_profile_ign(image_bytes: bytes) -> Optional[str]:
    """
    Extract the player's in-game name (IGN) from a profile screenshot using Ollama vision.
    Supports English, Chinese (汉字), numbers, and mixed names.
    Returns the cleaned IGN string, or None if not found or on error.
    """
    if not is_configured():
        log.warning("Ollama is not configured for profile OCR.")
        return None

    try:
        # Avoid json_format=True as constrained grammar decoding can cause empty responses on vision models
        raw_text = await _call_ollama(image_bytes, _PROFILE_IGN_PROMPT, json_format=False)
        log.debug("Profile IGN OCR raw: %s", raw_text[:300])

        ign: Optional[str] = None

        # 1. Try standard JSON extraction
        try:
            parsed = _extract_json(raw_text)
            if isinstance(parsed, dict):
                ign = parsed.get("ign")
        except Exception:
            pass

        # 2. Fallback regex extraction if JSON extraction didn't work
        if not ign or not isinstance(ign, str):
            match = re.search(r'["\']ign["\']\s*:\s*["\']([^"\']+)["\']', raw_text, re.IGNORECASE)
            if match:
                ign = match.group(1)

        # 3. Clean up the extracted IGN
        if ign and isinstance(ign, str):
            cleaned = ign.strip().strip('"').strip("'")
            if "#" in cleaned:
                cleaned = cleaned.split("#")[0].strip()
            if "\n" in cleaned:
                cleaned = cleaned.split("\n")[0].strip()
            if cleaned.lower() not in ("null", "none", "unknown", "n/a", "", "player", "<exact player username>"):
                return cleaned

    except Exception as e:
        log.error("Failed to extract profile IGN via Ollama: %s", e)

    return None

