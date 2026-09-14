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


def _detect_row_teams(image_bytes: bytes) -> list[int]:
    """Sample row background colors to detect Team 1 (Teal/Green) vs Team 2 (Red/Maroon)."""
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
                    row_types.append(0)

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
        log.debug("OpenRouter row color detection error: %s", exc)
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
        log.warning("OpenRouter: Unbalanced teams detected (%d vs %d), enforcing 5v5 balance", len(t1), len(t2))
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
    return _to_result(parsed, elapsed_ms, image_bytes=image_bytes)
