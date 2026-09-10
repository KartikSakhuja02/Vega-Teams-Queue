"""
utils/bedrock_client.py
------------------------
Async Amazon Bedrock client for Valorant scoreboard OCR.

IMPORTANT: AWS long-term Bedrock API keys (bearer tokens) only support
  - InvokeModel
  - InvokeModelWithResponseStream
They do NOT support the Converse API. This client uses invoke_model().

Authentication: set Railway env vars:
  AWS_BEARER_TOKEN_BEDROCK = <your key>
  AWS_REGION               = ap-south-1
  BEDROCK_MODEL_ID         = apac.amazon.nova-pro-v1:0
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

from utils.ocr.models import MatchOCRResult, PlayerRowStats

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
_REGION      = os.getenv("AWS_REGION", "ap-south-1")
_MODEL_ID    = os.getenv("BEDROCK_MODEL_ID", "apac.amazon.nova-pro-v1:0")
_BEARER      = os.getenv("AWS_BEARER_TOKEN_BEDROCK", "")
_MAX_TOKENS  = int(os.getenv("BEDROCK_MAX_TOKENS", "2048"))
_TEMPERATURE = float(os.getenv("BEDROCK_TEMPERATURE", "0.05"))


def is_configured() -> bool:
    """
    True when any Bedrock credential is available:
      - AWS_BEARER_TOKEN_BEDROCK  (long-term Bedrock API key)
      - AWS_ACCESS_KEY_ID         (IAM credentials — recommended)
    """
    return bool(
        os.getenv("AWS_BEARER_TOKEN_BEDROCK")
        or os.getenv("AWS_ACCESS_KEY_ID")
    )


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


# ── Image format detection ────────────────────────────────────────────────────
def _detect_image_format(data: bytes) -> str:
    if data[:2] == b'\xff\xd8':
        return "jpeg"
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return "gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "webp"
    return "jpeg"


# ── Request body builders (invoke_model format, not converse) ─────────────────
def _build_nova_body(image_b64: str, image_fmt: str) -> dict:
    """Amazon Nova Pro / Nova Lite request format."""
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": image_fmt,
                            "source": {"bytes": image_b64},
                        }
                    },
                    {"text": _PROMPT},
                ],
            }
        ],
        "inferenceConfig": {
            "maxTokens": _MAX_TOKENS,
            "temperature": _TEMPERATURE,
        },
    }


def _build_claude_body(image_b64: str, image_fmt: str) -> dict:
    """Anthropic Claude request format for InvokeModel."""
    return {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": _MAX_TOKENS,
        "temperature": _TEMPERATURE,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": f"image/{image_fmt}",
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
    }


def _extract_text_from_response(result: dict, model_id: str) -> str:
    """Extract the generated text from the InvokeModel response dict."""
    # Claude response: result["content"][0]["text"]
    if "anthropic" in model_id or "claude" in model_id:
        content = result.get("content") or []
        if content:
            return content[0].get("text", "")
    # Nova / default response: result["output"]["message"]["content"][0]["text"]
    try:
        return result["output"]["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        pass
    # Last resort: stringify the whole thing
    return json.dumps(result)


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
    raise ValueError(f"No valid JSON in model response: {text[:300]!r}")


# ── Synchronous InvokeModel call (run in executor) ────────────────────────────
def _call_bedrock(image_bytes: bytes) -> dict:
    """
    Blocking boto3 invoke_model call.
    Uses InvokeModel (supported by long-term Bedrock API keys).
    Must be called via run_in_executor from async context.
    """
    import boto3

    client = boto3.client("bedrock-runtime", region_name=_REGION)
    image_fmt = _detect_image_format(image_bytes)
    image_b64 = base64.b64encode(image_bytes).decode()

    # Build request body based on model provider
    if "anthropic" in _MODEL_ID or "claude" in _MODEL_ID:
        body = _build_claude_body(image_b64, image_fmt)
    else:
        body = _build_nova_body(image_b64, image_fmt)

    t0 = time.perf_counter()
    response = client.invoke_model(
        modelId=_MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    result = json.loads(response["body"].read())
    raw_text = _extract_text_from_response(result, _MODEL_ID)

    log.info("Bedrock %s responded in %.0f ms", _MODEL_ID, elapsed_ms)
    log.debug("Raw response (first 400 chars): %s", raw_text[:400])

    parsed = _extract_json(raw_text)
    parsed["_elapsed_ms"] = elapsed_ms
    return parsed


# ── Response → MatchOCRResult ─────────────────────────────────────────────────
def _clean_int(v) -> int:
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _confidence_from_nulls(players: list[dict]) -> float:
    FIELDS = ("acs", "kills", "deaths", "assists", "damage")
    total  = len(players) * len(FIELDS)
    if total == 0:
        return 0.0
    nulls      = sum(1 for p in players for f in FIELDS if p.get(f) is None)
    null_ratio = nulls / total
    player_ok  = 1.0 if len(players) == 10 else 0.5
    return round(min(1.0, (1.0 - null_ratio) * player_ok), 3)


def _to_match_result(data: dict, elapsed_ms: float) -> MatchOCRResult:
    players_raw = data.get("players") or []
    t1 = [p for p in players_raw if p.get("team") == 1]
    t2 = [p for p in players_raw if p.get("team") == 2]

    def _make(p: dict, team_label: str) -> PlayerRowStats:
        kills = deaths = assists = 0
        kda = p.get("kills")
        if isinstance(kda, str) and "/" in kda:
            parts = kda.split("/")
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
    """Send screenshot to Bedrock via InvokeModel → MatchOCRResult."""
    if not is_configured():
        raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK not set")

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
