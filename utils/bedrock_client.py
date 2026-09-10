"""
utils/bedrock_client.py
------------------------
Async Amazon Bedrock client for Valorant scoreboard OCR.

Authentication: AWS Bedrock long-term API key (bearer token).
  Set env vars on Railway:
    AWS_BEARER_TOKEN_BEDROCK = rpa_xxxxx   (your Bedrock API key)
    AWS_REGION               = ap-south-1  (Mumbai)

Model: configurable via BEDROCK_MODEL_ID env var.
  Recommended choices in ap-south-1:
    amazon.nova-pro-v1:0           (default — AWS native, vision, cheap, no extra form)
    amazon.nova-lite-v1:0          (faster, cheaper, slightly lower accuracy)
    anthropic.claude-3-5-haiku-20241022-v1:0   (excellent at JSON, needs model access approval)
    anthropic.claude-3-5-sonnet-20241022-v2:0  (best accuracy, most expensive)

The boto3 SDK automatically picks up AWS_BEARER_TOKEN_BEDROCK from the environment.
No boto3 session configuration is needed beyond the region.

Flow:
  image_bytes → Bedrock Converse API → structured JSON text → parse → validate → MatchOCRResult
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

from utils.ocr.models import MatchOCRResult, PlayerRowStats

log = logging.getLogger(__name__)

# ── Config from environment ───────────────────────────────────────────────────
_REGION    = os.getenv("AWS_REGION", "ap-south-1")
_MODEL_ID  = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
_BEARER    = os.getenv("AWS_BEARER_TOKEN_BEDROCK", "")
_MAX_TOKENS  = int(os.getenv("BEDROCK_MAX_TOKENS", "2048"))
_TEMPERATURE = float(os.getenv("BEDROCK_TEMPERATURE", "0.05"))


def is_configured() -> bool:
    """True when the Bedrock bearer token is set."""
    return bool(_BEARER)


# ── Prompt (same logic as the RunPod Qwen model) ──────────────────────────────
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
  "outcome": "Victory",
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
3. K/D/A format is kills/deaths/assists (e.g. "14/14/8").
4. Return ONLY the JSON. Nothing before or after it.
"""


# ── Image format detection ─────────────────────────────────────────────────────
def _detect_image_format(data: bytes) -> str:
    """Detect image format from magic bytes. Returns Bedrock-accepted format string."""
    if data[:2] == b'\xff\xd8':
        return "jpeg"
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return "gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "webp"
    return "jpeg"   # safe default for Discord screenshots


# ── JSON extraction ────────────────────────────────────────────────────────────
def _extract_json(text: str) -> dict:
    """Pull the JSON object out of the model's text response."""
    # Direct parse
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # Strip markdown fences
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Find first { … }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No valid JSON in model response: {text[:300]!r}")


# ── Synchronous Bedrock call (run in executor) ────────────────────────────────
def _call_bedrock(image_bytes: bytes) -> dict:
    """
    Blocking boto3 call to Bedrock Converse API.
    Must be run via run_in_executor — never call from async context directly.
    """
    import boto3

    client = boto3.client("bedrock-runtime", region_name=_REGION)
    image_fmt = _detect_image_format(image_bytes)

    t0 = time.perf_counter()
    response = client.converse(
        modelId=_MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": image_fmt,
                            "source": {"bytes": image_bytes},
                        }
                    },
                    {"text": _PROMPT},
                ],
            }
        ],
        inferenceConfig={
            "maxTokens": _MAX_TOKENS,
            "temperature": _TEMPERATURE,
        },
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    # Extract text from Converse response
    content = response.get("output", {}).get("message", {}).get("content", [])
    if not content:
        raise RuntimeError("Bedrock returned empty content")

    raw_text = content[0].get("text", "")
    log.info("Bedrock %s responded in %.0f ms (tokens: %s)",
             _MODEL_ID, elapsed_ms, response.get("usage", {}).get("outputTokens", "?"))
    log.debug("Raw response (first 400 chars): %s", raw_text[:400])

    parsed = _extract_json(raw_text)
    parsed["_elapsed_ms"] = elapsed_ms
    return parsed


# ── Response → MatchOCRResult ──────────────────────────────────────────────────
def _clean_int(v) -> int:
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _confidence_from_nulls(players: list[dict]) -> float:
    """0.0–1.0 based on how many numeric fields are non-null."""
    FIELDS = ("acs", "kills", "deaths", "assists", "damage")
    total  = len(players) * len(FIELDS)
    if total == 0:
        return 0.0
    nulls = sum(1 for p in players for f in FIELDS if p.get(f) is None)
    null_ratio   = nulls / total
    player_bonus = 1.0 if len(players) == 10 else 0.5
    return round(min(1.0, (1.0 - null_ratio) * player_bonus), 3)


def _to_match_result(data: dict, elapsed_ms: float) -> MatchOCRResult:
    players_raw = data.get("players") or []
    t1 = [p for p in players_raw if p.get("team") == 1]
    t2 = [p for p in players_raw if p.get("team") == 2]

    def _make(p: dict, team_label: str) -> PlayerRowStats:
        kda_str = p.get("kills")
        # Handle "14/14/8" format returned by some models
        kills = deaths = assists = 0
        if isinstance(kda_str, str) and "/" in kda_str:
            parts = kda_str.split("/")
            kills, deaths, assists = _clean_int(parts[0]), _clean_int(parts[1]), _clean_int(parts[2])
        else:
            kills   = _clean_int(p.get("kills"))
            deaths  = _clean_int(p.get("deaths"))
            assists = _clean_int(p.get("assists"))

        conf = 0.8   # base confidence for Bedrock result
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
            ign_conf=conf, acs_conf=conf, kda_conf=conf,
            dmg_conf=conf, fb_conf=conf, plants_conf=conf, defuses_conf=conf,
        )

    t1_players = [_make(p, "Team 1") for p in t1]
    t2_players = [_make(p, "Team 2") for p in t2]

    confidence  = _confidence_from_nulls(players_raw)
    needs_review = confidence < 0.60 or len(players_raw) != 10

    return MatchOCRResult(
        success=True,
        engine=f"Bedrock/{_MODEL_ID}",
        processing_time_ms=elapsed_ms,
        confidence=confidence,
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
    Send a screenshot to Amazon Bedrock and return a MatchOCRResult.

    Runs the blocking boto3 call in a thread pool so the Discord
    event loop is never blocked.

    Raises RuntimeError / BotoCoreError on failure — caller should catch
    and fall back to local Tesseract.
    """
    loop = asyncio.get_running_loop()
    t0   = time.monotonic()

    data = await loop.run_in_executor(None, _call_bedrock, image_bytes)

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    result = _to_match_result(data, elapsed_ms)

    log.info(
        "Bedrock OCR: conf=%.2f needs_review=%s players=%d+%d %.0fms",
        result.confidence, result.needs_review,
        len(result.team1_players), len(result.team2_players), elapsed_ms,
    )
    return result
