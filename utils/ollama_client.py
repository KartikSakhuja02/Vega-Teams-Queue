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
import json
import logging
import os
import re
import time
from typing import Optional

import aiohttp

from utils.ocr.models import MatchOCRResult, PlayerRowStats

log = logging.getLogger(__name__)

# ── Config (read once at import — changes require bot restart) ────────────────
_BASE_URL   = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
_MODEL      = os.getenv("OLLAMA_MODEL",    "gemma3:4b")
_TIMEOUT    = int(os.getenv("OLLAMA_TIMEOUT",    "180"))
_MAX_TOKENS = int(os.getenv("OLLAMA_MAX_TOKENS", "2048"))

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
- TABLE: 10 player rows:
    First 5 = GREEN/TEAL background = team 1
    Last  5 = RED/MAROON background = team 2
  Columns: 队伍排名(name) | 平均战斗评分(ACS) | 击败/敌阵/助攻(K/D/A) | 对局总伤害(damage) | 率先击败(first_bloods) | 部署(plants) | 拆除(defuses)
  MVP badges: "我方-最佳" = Team MVP, "敌方-最佳" = Match MVP

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
1. players must have exactly 10 entries. Index 0-4 = team=1, index 5-9 = team=2.
2. Use null for any field you cannot read confidently. Never guess.
3. K/D/A format is kills/deaths/assists separated by "/".
4. Return ONLY the JSON. Nothing before or after it.
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


async def _call_ollama(image_bytes: bytes, prompt: str) -> str:
    """Low-level: send image+prompt to Ollama, return raw text content."""
    image_b64 = base64.b64encode(image_bytes).decode()

    payload = {
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
            "num_predict": _MAX_TOKENS,
            "temperature": 0.05,
        },
    }

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


def _to_result(data: dict, elapsed_ms: float) -> MatchOCRResult:
    players_raw = data.get("players") or []
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
    raw_text = await _call_ollama(image_bytes, _SCOREBOARD_PROMPT)
    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    log.debug("Ollama scoreboard raw (first 400): %s", raw_text[:400])
    parsed = _extract_json(raw_text)
    return _to_result(parsed, elapsed_ms)
