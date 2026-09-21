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
from utils.ocr.agent_detector import clean_agent_name

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
def get_api_key() -> str:
    raw = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY_2") or ""
    return raw.strip().strip('"').strip("'")

def get_model() -> str:
    m = (os.getenv("OPENROUTER_MODEL") or "").strip().strip('"').strip("'")
    if m.startswith("oogle/"):
        m = "g" + m
    if "gemini-2.5-flash" in m.lower():
        return "google/gemini-2.5-flash"
    # If not set, or if an old free-tier model (e.g. :free, gemma) is leftover in env, use Gemini 2.5 Flash
    if not m or ":free" in m or "gemma" in m.lower():
        return "google/gemini-2.5-flash"
    return m

_TIMEOUT  = int(os.getenv("OPENROUTER_TIMEOUT", "120"))
_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"


def is_configured() -> bool:
    return bool(get_api_key())


# ── Prompt ────────────────────────────────────────────────────────────────────
_PROMPT = """\
You are analyzing a Valorant Mobile (CN version) custom match end-screen scoreboard.
The image may be from a phone or a tablet/iPad.

Return ONLY a valid JSON object. No explanation, no preamble, no markdown fences.

Layout:
- TOP CENTER: MATCH ROUND SCORE (CRITICAL):
  There are two large numbers at the top center showing the number of rounds won by each team:
  • If Team 1 won (Victory): "N 获胜 M" or "N 胜利 M" → team1_score=N, team2_score=M, outcome="Victory"
  • If Team 1 lost (Defeat): "N 败北 M" or "N 失败 M" → team1_score=N, team2_score=M, outcome="Defeat"
    Example: "6 败北 8" means team1_score=6 (cyan/green, left), team2_score=8 (red, right), outcome="Defeat".
  • If Draw: "N 平局 M" → team1_score=N, team2_score=M, outcome="Draw"
  • Left number (large, in cyan/green/blue font) = team1_score (Friendly team rounds won, integer 0-25).
  • Right number (large, in red/pink font) = team2_score (Enemy team rounds won, integer 0-25).
  • ROUND COUNTS are always small integers between 0 and 25 (e.g. 13 vs 11, 8 vs 6, 6 vs 8).
  • NEVER use player combat scores (ACS / 平均战斗评分 such as 525, 471, 308) as team scores! Player combat scores belong strictly in the "acs" field of each player.
- TOP LEFT: map name after "赛事模式-" (e.g. 莲华古城, 深海明珠, 源工重镇, 亚海悬城, 微风岛屿, 隐世修所, 霓虹町, 森寒冬港)
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
  • Neon: BRIGHT ELECTRIC CYAN/BLUE glowing spiky hair, blue face lightning marks, yellow/blue collar. If hair is blue/cyan, it is NEON, NEVER Jett!
  • Jett: PURE WHITE/SILVER hair swept upwards into a bun/ponytail, pale skin, facing left. Hair is WHITE, never blue.
  • Clove: Messy wavy PINK/PURPLE bob haircut, mischievous smirking grin, dark choker collar.
  • Reyna: Long dark violet/purple hair, purple glowing eyes and shadow aura, sharp female smirk, teardrop earring.
  • Iso: MALE agent, short dark purple/black parted curtain/bowl bangs haircut, angular cheekbones, dark combat jacket.
  • Sova: Blonde hair swept over one eye, robotic glowing blue eye, dark fur collar.
  • Omen: Dark blue/purple hooded cloak, 3 glowing vertical cyan slits on shadow face.
  • Chamber: Short combed brown hair, gold glasses, white collar shirt & navy vest, french goatee.
  • Yoru: Blue spiked hair with dark fade/undercut, eyebrow slit.
  • Viper: Dark short hair, black/green tactical gas mask covering mouth/nose.
  • Gekko: Bright neon lime-green dyed hair, yellow/purple highlights.
  • Cypher: White fedora hat with wide brim, glowing blue eyes mask, trench coat collar.
  • Sage: Long black hair in ponytail, pale skin, jade teal orb earrings/collar.
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
  • KAY/O: Metallic robot face with LED glass visor/screen.
  • Tejo: Military brown hair, tactical combat visor/goggles, comms headset.
  • Vyse: Liquid metallic reflective mask/helmet, dark thorny rose collar.
  • Waylay: Light lavender/purple hair, modern tactical combat gear.
  Set "agent" to the detected agent's name or null if unclear.

  IMPORTANT - SCOREBOARD COLUMNS (Read strictly left-to-right for each row):
  Column 1: 排名/头像/IGN (Avatar Icon + Player Name + optional MVP tag "我方-最佳" or "敌方-最佳")
  Column 2: 平均战斗评分 (Average Combat Score / ACS) -> "acs": single integer (e.g. 469, 420, 363, 353, 347, 207)
  Column 3: 击败/败阵/助攻 (Kills / Deaths / Assists) -> ALWAYS formatted as "K / D / A" separated by slashes.
    - kills: the number BEFORE the first slash
    - deaths: the number BETWEEN the two slashes
    - assists: the number AFTER the second slash
    Example: "13 / 4 / 1" means kills=13, deaths=4, assists=1.
    Example: "10 / 6 / 5" means kills=10, deaths=6, assists=5.
    Example: "10 / 7 / 4" means kills=10, deaths=7, assists=4.
    Example: "10 / 8 / 0" means kills=10, deaths=8, assists=0.
    DO NOT confuse the assists number with First Bloods or any other column!
  Column 4: 对局总伤害 (Total Damage) -> "damage": single integer (e.g. 2406, 2482, 1704, 1663)
  Column 5: 率先击败 (First Bloods) -> "first_bloods": single integer (e.g. 5, 0, 1)
  Column 6: 部署 (Plants) -> "plants": single integer (e.g. 0, 1, 2)
  Column 7: 拆除 (Defuses) -> "defuses": single integer (e.g. 0, 1)

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
      "agent": "<Agent name e.g. Chamber, Gekko, Phoenix, Reyna, Jett, Cypher or null>",
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
1. players must contain all 10 players from the table in order from top to bottom.
2. EXACTLY 5 players must have team=1, and EXACTLY 5 players must have team=2.
3. In column 3 (击败/败阵/助攻), strictly extract kills / deaths / assists from the "K / D / A" numbers.
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


MAP_NAME_MAP = {
    "森寒冬港": "Icebox",
    "极地寒港": "Icebox",
    "冰箱": "Icebox",
    "亚海悬城": "Ascent",
    "隐世修所": "Haven",
    "源工重镇": "Bind",
    "霓虹町": "Split",
    "微风岛屿": "Breeze",
    "裂变暗区": "Fracture",
    "深海明珠": "Pearl",
    "莲华古城": "Lotus",
    "日落之城": "Sunset",
    "深邃地窟": "Abyss",
}


# ── JSON extraction ───────────────────────────────────────────────────────────
def _extract_json(text: str) -> dict:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
    if "```" in clean:
        clean = clean.split("```")[0].strip()

    # Sanitize trailing commas before closing braces/brackets
    clean_sanitized = re.sub(r",\s*([\}\]])", r"\1", clean)

    # 1. Direct parse
    try:
        return json.loads(clean_sanitized)
    except json.JSONDecodeError:
        pass

    # 2. Regex codeblock parse
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    if m:
        try:
            block = re.sub(r",\s*([\}\]])", r"\1", m.group(1).strip())
            return json.loads(block)
        except json.JSONDecodeError:
            pass

    # 3. Outermost braces parse
    start, end = clean.find("{"), clean.rfind("}")
    if start != -1 and end > start:
        try:
            block = re.sub(r",\s*([\}\]])", r"\1", clean[start:end + 1])
            return json.loads(block)
        except json.JSONDecodeError:
            pass

    # 4. Fallback repair for truncated/incomplete JSON
    log.info("Direct JSON parse failed, applying fuzzy repair fallback for truncated LLM output...")
    start_pos = text.find("{")
    if start_pos != -1:
        truncated_body = text[start_pos:]

        t1_m = re.search(r'"team1_score"\s*:\s*(\d+)', truncated_body)
        t2_m = re.search(r'"team2_score"\s*:\s*(\d+)', truncated_body)
        map_m = re.search(r'"map"\s*:\s*"([^"]+)"', truncated_body)
        date_m = re.search(r'"match_date"\s*:\s*"([^"]+)"', truncated_body)
        dur_m = re.search(r'"duration"\s*:\s*"([^"]+)"', truncated_body)
        out_m = re.search(r'"outcome"\s*:\s*"([^"]+)"', truncated_body)

        players = []
        p_match = re.search(r'"players"\s*:\s*\[', truncated_body)
        if p_match:
            p_start = p_match.end()
            depth = 0
            obj_start = None
            for i in range(p_start, len(truncated_body)):
                ch = truncated_body[i]
                if ch == '{':
                    if depth == 0:
                        obj_start = i
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0 and obj_start is not None:
                        try:
                            p_obj = json.loads(truncated_body[obj_start:i + 1])
                            players.append(p_obj)
                        except Exception:
                            pass
                        obj_start = None

        if t1_m or t2_m or map_m or players:
            log.info(
                "Recovered truncated JSON structure (scores: %s-%s, map: %s, players: %d)",
                t1_m.group(1) if t1_m else "N/A",
                t2_m.group(1) if t2_m else "N/A",
                map_m.group(1) if map_m else "N/A",
                len(players),
            )
            return {
                "success": True,
                "team1_score": int(t1_m.group(1)) if t1_m else None,
                "team2_score": int(t2_m.group(1)) if t2_m else None,
                "map": map_m.group(1) if map_m else "Unknown",
                "match_date": date_m.group(1) if date_m else None,
                "duration": dur_m.group(1) if dur_m else None,
                "outcome": out_m.group(1) if out_m else "Unknown",
                "players": players,
            }

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


def _clean_round_score(v) -> Optional[int]:
    if v is None:
        return None
    n = _clean_int(v)
    # Valid match rounds are between 0 and 30. Combat scores (ACS) are > 30.
    if 0 <= n <= 30:
        return n
    log.warning("OpenRouter: Rejected invalid round score %d (likely combat score / ACS)", n)
    return None


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
            ar = w / float(h)
            if ar < 1.65:
                # Tablet / iPad: Scoreboard table spans 31.7% to 82.5%
                y_start = int(0.317 * h)
                y_end = int(0.825 * h)
                sample_xs = (0.20, 0.30, 0.40, 0.50)
            else:
                # Standard Phone
                y_start = int(0.28 * h)
                y_end = int(0.92 * h)
                sample_xs = (0.25, 0.35, 0.45, 0.55)

            row_h = (y_end - y_start) / 10

            row_types = []
            for i in range(10):
                cy = int(y_start + (i + 0.5) * row_h)
                votes_teal = 0
                votes_red = 0
                for frac_x in sample_xs:
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


def _to_result(
    data: dict,
    elapsed_ms: float,
    image_bytes: Optional[bytes] = None,
    model: Optional[str] = None,
) -> MatchOCRResult:
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
        # Handle K/D/A passed as slash string in "kda" or "kills"
        kills = deaths = assists = 0
        raw_kda = p.get("kda") or p.get("kills")
        if isinstance(raw_kda, str) and "/" in raw_kda:
            parts = [re.sub(r"[^\d]", "", s) for s in raw_kda.split("/")]
            if len(parts) >= 3:
                kills = _clean_int(parts[0])
                deaths = _clean_int(parts[1])
                assists = _clean_int(parts[2])
            elif len(parts) == 2:
                kills = _clean_int(parts[0])
                deaths = _clean_int(parts[1])
                assists = _clean_int(p.get("assists"))
            else:
                kills = _clean_int(p.get("kills"))
                deaths = _clean_int(p.get("deaths"))
                assists = _clean_int(p.get("assists"))
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

    t1_players  = [_make(p, "Team 1") for p in t1]
    t2_players  = [_make(p, "Team 2") for p in t2]
    conf        = _confidence(players_raw)
    needs_review = conf < 0.60 or len(players_raw) != 10
    active_model = model or get_model()

    raw_map = str(data.get("map") or "Unknown").strip()
    clean_map = MAP_NAME_MAP.get(raw_map, raw_map)

    return MatchOCRResult(
        success=True,
        engine=f"OpenRouter/{active_model}",
        processing_time_ms=elapsed_ms,
        confidence=conf,
        needs_review=needs_review,
        map_name=clean_map,
        match_date=str(data.get("match_date") or "Unknown"),
        duration=str(data.get("duration") or "Unknown"),
        team1_score=_clean_round_score(data.get("team1_score")),
        team2_score=_clean_round_score(data.get("team2_score")),
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
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY (or OPENROUTER_API_KEY_2) not set")

    model   = get_model()
    masked_key = f"{api_key[:7]}...{api_key[-4:]}" if len(api_key) >= 12 else "***"
    log.info("Sending match screenshot to OpenRouter model: %s (key=%s)", model, masked_key)
    mime    = _detect_mime(image_bytes)
    b64_img = base64.b64encode(image_bytes).decode()
    data_url = f"data:{mime};base64,{b64_img}"

    payload = {
        "model": model,
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
        "response_format": {"type": "json_object"},
        "max_tokens": 4096,
        "temperature": 0.05,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
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
        model, elapsed_ms,
        result.get("usage", {}).get("completion_tokens", "?"),
        finish,
    )
    log.debug("Raw response (first 500 chars): %s", raw_content[:500])

    parsed = _extract_json(raw_content)
    return _to_result(parsed, elapsed_ms, image_bytes=image_bytes, model=model)
