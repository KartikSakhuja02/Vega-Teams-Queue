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
from PIL import Image, ImageOps

from utils.ocr.models import MatchOCRResult, PlayerRowStats
from utils.ocr.agent_detector import clean_agent_name

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

  IMPORTANT - MVP BADGE DETECTION (text in front of player name):
  • "我方-最佳" or "我方最佳" (yellow / gold text on teal/green background) = OUR TEAM MVP (Team 1).
    Set is_mvp=true, mvp_type="Team MVP".
  • "敌方-最佳" or "敌方最佳" (light blue / cyan text on maroon/red background) = ENEMY TEAM MVP (Team 2).
    Set is_mvp=true, mvp_type="Enemy MVP".
  • Do not include the badge text ("我方-最佳" or "敌方-最佳") inside the player's name.

  IMPORTANT - AGENT DETECTION (character portrait avatar square next to player name):
  Identify the Valorant agent played by each player from their character avatar portrait.
  Valid agents include:
  Astra, Breach, Brimstone, Chamber, Clove, Cypher, Deadlock, Fade, Gekko, Harbor, Iso, Jett, KAY/O, Killjoy, Neon, Omen, Phoenix, Raze, Reyna, Sage, Skye, Sova, Tejo, Viper, Vyse, Waylay, Yoru.
  Visual Guide for Agent Portrait Avatars:
  • Jett: White/silver swept-up hair, pale skin, facing left, blue/grey tint.
  • Neon: Bright electric cyan/blue spiky glowing hair, blue facial lightning marks.
  • Sova: Blonde hair covering one eye, robotic blue eye, fur collar.
  • Omen: Dark blue/purple hooded cloak, 3 glowing vertical cyan slits on shadow face.
  • Chamber: Short combed brown hair, gold glasses, white collar shirt & navy vest, french goatee.
  • Yoru: Blue spiked hair with dark fade/undercut, eyebrow slit.
  • Viper: Dark short hair, black/green tactical gas mask covering mouth/nose.
  • Reyna: Long dark purple hair, purple eyes/glow, sharp smirk.
  • Gekko: Bright neon lime-green dyed hair, yellow/purple highlights.
  • Cypher: White fedora hat with wide brim, glowing blue eyes mask, trench coat collar.
  • Sage: Long black hair in ponytail, pale skin, jade teal orb earrings/collar.
  • Clove: Short pink/purple wavy bob hair, mischievous grin, dark choker.
  • Breach: Orange/red hair and thick beard, bionic mechanical neck/shoulders.
  • Brimstone: Grey beard/mustache, dark beret cap, orange tactical headset.
  • Killjoy: Yellow beanie hat, round glasses, green jacket.
  • Phoenix: Dark skin, black short fade hair, yellow/orange flame jacket collar.
  • Fade: Black hair with white/grey streaks, heterochromia eyes, dark coat.
  • Raze: Orange backwards cap/headband, curly dark hair, headphones around neck.
  • Skye: Green headband over brown hair, leaf feather motif.
  • Astra: Purple braids/dreadlocks, golden arm, astral stars collar.
  • Deadlock: Blonde hair tied back, metal prosthetic neck collar, scar across left eye.
  • Harbor: Thick black beard & mustache, teal wave armor collar.
  • Iso: Dark bowl/curtain haircut with purple undertone, angular facial shadow.
  • KAY/O: Metallic robot face with LED glass visor/screen.
  • Tejo: Military brown hair, tactical combat visor/goggles, comms headset.
  • Vyse: Liquid metallic reflective mask/helmet, dark thorny rose collar.
  • Waylay: Light lavender/purple hair, modern tactical combat gear.
  Set "agent" to the detected agent's name or null if unclear.

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
      "name": "<exact name without MVP badge>",
      "agent": "<Agent name e.g. Iso, Neon, Sova, Phoenix, Killjoy, Cypher, Jett or null>",
      "team": <1 or 2>,
      "is_mvp": <true/false>,
      "mvp_type": <"Team MVP" or "Enemy MVP" or "Match MVP" or null>,
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
    async with inference_semaphore:
        return await _call_ollama_internal(image_bytes, prompt, json_format=json_format)


async def _call_ollama_internal(image_bytes: bytes, prompt: str, json_format: bool = False) -> str:
    """Internal HTTP call to Ollama /api/chat with auto-retry on grammar stack bug."""
    image_bytes = _prepare_image(image_bytes)
    image_b64 = base64.b64encode(image_bytes).decode()

    # Note: With vision models (qwen2.5vl:3b), passing format="json" activates llama.cpp's BNF grammar
    # parser which can crash with "Unexpected empty grammar stack after accepting piece", aborting
    # generation and returning empty response (done=False, done_reason=None).
    # We do not pass format="json" by default as the prompt already requires JSON and _extract_json parses it.
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

    # Automatic fallback: if format="json" caused empty response due to llama.cpp grammar bug, retry without it
    if not content and json_format:
        log.warning("Ollama returned empty response with format='json' (grammar stack bug) — retrying without grammar constraint...")
        return await _call_ollama_internal(image_bytes, prompt, json_format=False)

    if not content:
        raise RuntimeError(
            f"Ollama returned empty response. "
            f"done={data.get('done')}, done_reason={data.get('done_reason')}"
        )
    return content


async def _call_ollama_generate(
    image_bytes: bytes,
    prompt: str,
    temperature: float = 0.0,
    num_predict: int = 48,
    stop: Optional[list[str]] = None,
) -> str:
    """Send image+prompt to Ollama /api/generate for fast direct vision completion."""
    image_bytes = _prepare_image(image_bytes, max_dim=1280)
    image_b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": _MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {
            "num_ctx": max(_NUM_CTX, 8192),
            "temperature": temperature,
            "num_predict": num_predict,
            "repeat_penalty": 1.25,
            "repeat_last_n": 64,
            "stop": stop or ["\n", "\r", "@@", "----"],
        },
    }
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=_HEADERS) as session:
            async with session.post(f"{_BASE_URL}/api/generate", json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Ollama returned HTTP {resp.status}: {body[:300]}")
                data = await resp.json()
    except aiohttp.ClientConnectorError as exc:
        raise RuntimeError(f"Cannot connect to Ollama at {_BASE_URL}. Is Ollama running?") from exc
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"Ollama timed out after {_TIMEOUT}s.") from exc

    return data.get("response", "").strip()


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
            agent=clean_agent_name(p.get("agent")),
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
    raw_text = await _call_ollama(image_bytes, _SCOREBOARD_PROMPT, json_format=False)
    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    log.debug("Ollama scoreboard raw (first 400): %s", raw_text[:400])
    parsed = _extract_json(raw_text)
    return _to_result(parsed, elapsed_ms, image_bytes=image_bytes)


_INVALID_IGN_VALUES = {
    "null", "none", "unknown", "n/a", "", "player", "ign", "username",
    "+添加语音", "+ 添加语音", "添加语音", "暂未设置标签",
    "名片", "文明", "菁英", "超凡", "神话", "钻石", "铂金", "黄金", "白银", "青铜", "铁牌",
    "总览", "战绩", "数据", "战力", "排位赛", "主页访客", "最近访客", "无畏时刻", "瓦谷展示", "动态",
}


def _normalize_image_orientation(image_bytes: bytes, rot_deg: int = 0) -> bytes:
    """
    Transpose EXIF orientation, and if height > width (sideways mobile screenshot),
    rotate 90 degrees CCW to restore native landscape layout of Valorant Mobile.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            if rot_deg:
                img = img.rotate(rot_deg, expand=True)
            elif img.height > img.width:
                # Sideways phone screenshot: rotate 90 CCW to make landscape
                img = img.rotate(90, expand=True)
            buf = io.BytesIO()
            fmt = img.format if img.format in ("PNG", "JPEG", "WEBP") else "PNG"
            img.save(buf, format=fmt)
            return buf.getvalue()
    except Exception as exc:
        log.debug("Orientation normalization error: %s", exc)
        return image_bytes


def _crop_profile_card(image_bytes: bytes) -> bytes:
    """Crop the top profile header card (top 45% height) where the avatar and username reside."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
            crop = img.crop((0, 0, w, int(h * 0.45)))
            buf = io.BytesIO()
            fmt = img.format if img.format in ("PNG", "JPEG", "WEBP") else "PNG"
            crop.save(buf, format=fmt)
            return buf.getvalue()
    except Exception as exc:
        log.debug("Crop profile card error (falling back to full image): %s", exc)
        return image_bytes


def _clean_profile_ign(raw: str) -> Optional[str]:
    if not raw:
        return None
    first_line = raw.strip().split("\n")[0].strip()
    cleaned = first_line.strip("`'\" \t\r")
    cleaned = re.sub(r"^[♀♂·\s>@#*~_=-]+", "", cleaned)
    cleaned = re.sub(r"[♀♂·\s<@#*~_=-]+$", "", cleaned).strip()
    if "#" in cleaned:
        cleaned = cleaned.split("#")[0].strip()

    # Reject repetitive hallucination loops (e.g. @@@@@@@@@, aaaaaaaa, ........)
    if re.search(r"(.)\1{3,}", cleaned):
        return None

    # Must contain at least one valid alphanumeric character or CJK ideograph
    if not re.search(r"[a-zA-Z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", cleaned):
        return None

    # Reject pure numbers <= 3 digits (e.g. avatar level badges 144, 464, 51, etc.)
    if cleaned.isdigit() and len(cleaned) <= 3:
        return None
    if cleaned.lower() in _INVALID_IGN_VALUES or not cleaned:
        return None
    return cleaned


def _parse_ign_from_lines(raw_text: str) -> Optional[str]:
    if not raw_text:
        return None
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for line in lines:
        if "编号" in line or "ID" in line or line.startswith("Lv.") or "添加语音" in line:
            continue
        cleaned = _clean_profile_ign(line)
        if cleaned:
            return cleaned
    return None


async def extract_profile_ign(image_bytes: bytes) -> Optional[str]:
    """
    Extract the player's in-game name (IGN) from a profile screenshot using Ollama vision.
    Handles sideways/rotated phone screenshots (e.g. portrait photos with black bars),
    English, Chinese (汉字), numbers, and mixed names.
    Returns the cleaned IGN string, or None if not found or on error.
    """
    if not is_configured():
        log.warning("Ollama is not configured for profile OCR.")
        return None

    # Check if original image was portrait (sideways)
    is_portrait = False
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            is_portrait = img.height > img.width
    except Exception:
        pass

    async def _try_extract(raw_bytes: bytes) -> Optional[str]:
        cropped_bytes = _crop_profile_card(raw_bytes)
        prompt = (
            "Look at the player profile card in this Valorant Mobile screenshot. "
            "Find the player avatar/icon in the upper card and read the bold username/IGN directly next to it. "
            "Do not read the status tag or bio below it. "
            "Output ONLY the username/IGN and nothing else."
        )

        raw_text = await _call_ollama_generate(
            cropped_bytes,
            prompt,
            temperature=0.0,
            num_predict=32,
            stop=["\n", "\r", "@@", "----"],
        )
        log.info("Profile IGN OCR raw output: %s", repr(raw_text))

        ign = _clean_profile_ign(raw_text)
        if ign:
            return ign

        # Fallback: line-by-line transcription
        fallback_prompt = "Transcribe all text lines in this image."
        raw_lines = await _call_ollama_generate(
            cropped_bytes,
            fallback_prompt,
            temperature=0.0,
            num_predict=48,
            stop=["\n\n", "@@"],
        )
        return _parse_ign_from_lines(raw_lines)

    try:
        # Attempt 1: Normal orientation (with auto 90 CCW rotation if portrait)
        norm_bytes = _normalize_image_orientation(image_bytes)
        ign = await _try_extract(norm_bytes)
        if ign:
            return ign

        # Attempt 2: If portrait was detected and 90 CCW failed, try 270 CCW (90 CW)
        if is_portrait:
            norm_bytes_270 = _normalize_image_orientation(image_bytes, rot_deg=270)
            ign = await _try_extract(norm_bytes_270)
            if ign:
                return ign

    except Exception as e:
        log.error("Failed to extract profile IGN via Ollama: %s", e)

    return None

