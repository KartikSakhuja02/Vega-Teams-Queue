"""
utils/openrouter_client.py
---------------------------
Async OpenRouter client for Valorant scoreboard OCR.

Uses the OpenAI-compatible chat completions API with base64 image input.
Already uses aiohttp which is in requirements.txt — no new dependencies.

Required Railway env var:
  OPENROUTER_API_KEY_2 = sk-or-v1-...

Optional:
  OPENROUTER_MODEL   = inclusionai/ling-3.0-flash-vl:free  (default)
  OPENROUTER_TIMEOUT = 60   (seconds, default 60)
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

# ── Config ────────────────────────────────────────────────────────────────────
_API_KEY  = os.getenv("OPENROUTER_API_KEY_2", "")
_MODEL    = os.getenv("OPENROUTER_MODEL", "inclusionai/ling-3.0-flash-vl:free")
_TIMEOUT  = int(os.getenv("OPENROUTER_TIMEOUT", "120"))
_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"


def is_configured() -> bool:
    return bool(_API_KEY)


# ── Prompt ────────────────────────────────────────────────────────────────────
_PROMPT = """\
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


# ── Image format detection ────────────────────────────────────────────────────
def _detect_mime(data: bytes) -> str:
    if data[:2] == b'\xff\xd8':
        return "image/jpeg"
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    return "image/jpeg"


# ── JSON extraction ───────────────────────────────────────────────────────────
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


# ── Response → MatchOCRResult ─────────────────────────────────────────────────
def _clean_int(v) -> int:
    if v is None:
        return 0
    if isinstance(v, str) and "/" in v:
        # Handle "14/14/8" passed as a field accidentally
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
        # Handle K/D/A passed as a single "kills" string
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

    t1_players  = [_make(p, "Team 1") for p in t1]
    t2_players  = [_make(p, "Team 2") for p in t2]
    conf        = _confidence(players_raw)
    needs_review = conf < 0.60 or len(players_raw) != 10

    return MatchOCRResult(
        success=True,
        engine=f"OpenRouter/{_MODEL}",
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


# ── Public async API ──────────────────────────────────────────────────────────
async def extract_scoreboard(image_bytes: bytes) -> MatchOCRResult:
    """
    Send screenshot to OpenRouter vision model → MatchOCRResult.
    Fully async — no thread executor needed (aiohttp is already async).
    Raises on failure so match_ocr.py can fall back to Tesseract.
    """
    if not is_configured():
        raise RuntimeError("OPENROUTER_API_KEY not set")

    mime    = _detect_mime(image_bytes)
    b64_img = base64.b64encode(image_bytes).decode()
    data_url = f"data:{mime};base64,{b64_img}"

    payload = {
        "model": _MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                    {
                        "type": "text",
                        "text": _PROMPT,
                    },
                ],
            }
        ],
        "max_tokens": 2048,
        "temperature": 0.05,
    }

    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/KartikSakhuja02/Vega-Teams-Queue",
        "X-Title": "Vega Esports Scrims Bot",
    }

    t0      = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    result  = None
    MAX_RETRIES = 3

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(1, MAX_RETRIES + 1):
            async with session.post(_BASE_URL, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    break
                elif resp.status == 429:
                    body = await resp.text()
                    wait = 3 * (2 ** (attempt - 1))   # 3s, 6s, 12s
                    if attempt < MAX_RETRIES:
                        log.warning(
                            "OpenRouter 429 rate-limit (attempt %d/%d) — retrying in %ds",
                            attempt, MAX_RETRIES, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise RuntimeError(
                            f"OpenRouter rate-limited after {MAX_RETRIES} attempts: {body[:200]}"
                        )
                else:
                    body = await resp.text()
                    raise RuntimeError(f"OpenRouter HTTP {resp.status}: {body[:300]}")

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    # Log the full raw response so we can diagnose issues
    log.debug("OpenRouter raw response: %s", json.dumps(result)[:800])

    # Extract text from OpenAI-format response
    choices = result.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {json.dumps(result)[:400]}")

    choice      = choices[0]
    finish      = choice.get("finish_reason", "unknown")
    message     = choice.get("message") or {}
    raw_content = message.get("content")

    # Some models return content as a list of {type, text} objects
    if isinstance(raw_content, list):
        texts = [c.get("text", "") for c in raw_content if c.get("type") == "text"]
        raw_content = "\n".join(texts)

    if not raw_content:
        # Log enough to diagnose without flooding
        log.warning(
            "OpenRouter empty content — finish_reason=%s usage=%s error=%s",
            finish,
            result.get("usage"),
            result.get("error"),
        )
        raise RuntimeError(
            f"OpenRouter returned empty content (finish_reason={finish!r})"
        )

    log.info(
        "OpenRouter %s responded in %.0f ms (tokens: %s, finish: %s)",
        _MODEL, elapsed_ms,
        result.get("usage", {}).get("completion_tokens", "?"),
        finish,
    )
    log.debug("Raw response (first 500 chars): %s", raw_content[:500])

    parsed = _extract_json(raw_content)
    return _to_result(parsed, elapsed_ms)
