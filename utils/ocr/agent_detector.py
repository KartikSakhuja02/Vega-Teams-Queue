"""
utils/ocr/agent_detector.py
----------------------------
Valorant agent recognition & Discord emoji integration.

Combines:
1. Canonical Agent registry & normalization (handles spelling variations like Pheonix/Phoenix, KAYO/KAY/O, Harbour/Harbor).
2. Discord custom emoji resolution (looks up <:Agent:ID> from guild/bot emojis).
3. Fast Computer Vision template matching fallback (matches scoreboard avatar crops against agents/*.png).
"""
from __future__ import annotations

import glob
import logging
import os
import re
from pathlib import Path
from typing import Optional

try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

from utils.ocr.models import MatchOCRResult, PlayerRowStats

log = logging.getLogger(__name__)

# Path to agents directory
AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "agents"

# Canonical list of Valorant / Valorant Mobile agents
CANONICAL_AGENTS = [
    "Astra", "Breach", "Brimstone", "Chamber", "Clove", "Cypher", "Deadlock",
    "Fade", "Gekko", "Harbor", "Iso", "Jett", "KAY/O", "Killjoy", "Neon",
    "Omen", "Phoenix", "Raze", "Reyna", "Sage", "Skye", "Sova", "Tejo",
    "Viper", "Vyse", "Waylay", "Yoru",
]

# Normalization map for aliases, typos, and file naming quirks
_AGENT_ALIASES: dict[str, str] = {
    "pheonix": "Phoenix",
    "phoenix": "Phoenix",
    "harbour": "Harbor",
    "harbor": "Harbor",
    "kayo": "KAY/O",
    "kay/o": "KAY/O",
    "kay-o": "KAY/O",
    "kj": "Killjoy",
    "dead lock": "Deadlock",
    "brim": "Brimstone",
}
for agent in CANONICAL_AGENTS:
    _AGENT_ALIASES[agent.lower()] = agent


def clean_agent_name(name: Optional[str]) -> Optional[str]:
    """Normalize raw agent string to canonical name."""
    if not name:
        return None

    cleaned = str(name).strip().strip("[](){}:'\"_")
    if not cleaned or cleaned.lower() in ("null", "none", "unknown"):
        return None

    key = cleaned.lower()
    if key in _AGENT_ALIASES:
        return _AGENT_ALIASES[key]

    # Substring search (e.g. "Agent: Jett")
    for alias_key, canonical in _AGENT_ALIASES.items():
        if alias_key in key:
            return canonical

    return cleaned.capitalize()


def get_emoji_candidate_names(agent_name: Optional[str]) -> list[str]:
    """
    Return list of valid emoji names to check in Discord.
    Discord emoji names only allow alphanumeric and underscores.
    """
    canon = clean_agent_name(agent_name)
    if not canon:
        return []

    names = []
    # Base alphanumeric name
    safe = re.sub(r"[^a-zA-Z0-9_]", "", canon)
    if safe:
        names.append(safe)

    # Common variations in Discord servers:
    if canon == "Phoenix":
        names.extend(["Phoenix", "Pheonix", "phoenix", "pheonix"])
    elif canon == "Harbor":
        names.extend(["Harbor", "Harbour", "harbor", "harbour"])
    elif canon == "KAY/O":
        names.extend(["KAYO", "kayo", "Kayo", "KayO"])
    else:
        names.extend([canon, canon.lower(), canon.upper()])

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            deduped.append(n)
    return deduped


def get_agent_emoji(bot, agent_name: Optional[str], guild=None) -> str:
    """
    Find Discord custom emoji for an agent.
    Checks guild emojis first (if guild provided), then all bot emojis.
    Returns '<:Name:ID>' string, or '' if not found.
    """
    if not agent_name or not bot:
        return ""

    candidates = [c.lower() for c in get_emoji_candidate_names(agent_name)]
    if not candidates:
        return ""

    # 1. Search in current guild emojis
    if guild and hasattr(guild, "emojis"):
        for emoji in guild.emojis:
            if emoji.name.lower() in candidates:
                return str(emoji)

    # 2. Search in global bot emojis (all mutual guilds)
    if hasattr(bot, "emojis"):
        for emoji in bot.emojis:
            if emoji.name.lower() in candidates:
                return str(emoji)

    return ""


# ── Template Matching Cache ───────────────────────────────────────────────────

_AGENT_TEMPLATES: dict[str, np.ndarray] = {}
_TEMPLATES_LOADED = False


def _load_templates():
    """Load and cache agent images from agents/ directory."""
    global _AGENT_TEMPLATES, _TEMPLATES_LOADED
    if _TEMPLATES_LOADED or not _CV2_AVAILABLE:
        return

    if not AGENTS_DIR.exists():
        log.warning("Agents directory not found at %s", AGENTS_DIR)
        _TEMPLATES_LOADED = True
        return

    for path in glob.glob(str(AGENTS_DIR / "*.png")):
        base_name = Path(path).stem
        canonical = clean_agent_name(base_name) or base_name
        im = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if im is not None:
            _AGENT_TEMPLATES[canonical] = im

    _TEMPLATES_LOADED = True
    log.info("Loaded %d agent templates for CV matching", len(_AGENT_TEMPLATES))


def match_agent_crop(crop_bgr: np.ndarray) -> tuple[Optional[str], float]:
    """
    Match an avatar image crop against all cached agent templates.
    Returns (agent_name, score).
    """
    if not _CV2_AVAILABLE or crop_bgr is None or crop_bgr.size == 0:
        return None, 0.0

    _load_templates()
    if not _AGENT_TEMPLATES:
        return None, 0.0

    ch, cw = crop_bgr.shape[:2]
    if ch < 12 or cw < 12:
        return None, 0.0

    best_agent: Optional[str] = None
    best_val: float = -1.0

    # Try target sizes fitting inside the crop
    target_sizes = [int(ch * s) for s in (0.75, 0.85, 0.95)]
    target_sizes = [s for s in target_sizes if 10 <= s <= min(ch, cw)]
    if not target_sizes:
        target_sizes = [min(ch, cw)]

    for agent_name, templ in _AGENT_TEMPLATES.items():
        bgr = templ[:, :, :3]
        for ts in target_sizes:
            try:
                t_resized = cv2.resize(bgr, (ts, ts), interpolation=cv2.INTER_AREA)
                res = cv2.matchTemplate(crop_bgr, t_resized, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(res)
                if max_val > best_val:
                    best_val = float(max_val)
                    best_agent = agent_name
            except Exception:
                continue

    return best_agent, best_val


def extract_row_avatar_crops(
    img_bgr: np.ndarray,
    rows: Optional[list[tuple[int, int]]] = None,
) -> list[Optional[np.ndarray]]:
    """
    Extract 10 avatar crops from a scoreboard image.
    Uses fallback coordinates if rows are not provided.
    """
    if img_bgr is None:
        return [None] * 10

    h, w = img_bgr.shape[:2]
    crops: list[Optional[np.ndarray]] = []

    # Calibrated avatar column: left 7% to 16% of width
    ax0 = int(0.075 * w)
    ax1 = int(0.165 * w)

    if not rows or len(rows) != 10:
        # Calibrated fallback row percentages
        fallback_rows = [
            (0.272, 0.352), (0.352, 0.432), (0.432, 0.508), (0.508, 0.584), (0.584, 0.660),
            (0.665, 0.740), (0.740, 0.815), (0.815, 0.888), (0.888, 0.956), (0.956, 1.000),
        ]
        row_bounds = [(int(y0 * h), int(y1 * h)) for y0, y1 in fallback_rows]
    else:
        row_bounds = rows

    for y0, y1 in row_bounds:
        c = img_bgr[max(0, y0):min(h, y1), max(0, ax0):min(w, ax1)]
        crops.append(c if c.size > 0 else None)

    return crops


def resolve_player_agents(
    result: MatchOCRResult,
    image_bytes: Optional[bytes] = None,
) -> MatchOCRResult:
    """
    Normalize agent names in OCR result and perform template matching fallback
    for any missing agents if image_bytes is provided.
    """
    if not result or not result.success:
        return result

    all_players = result.team1_players + result.team2_players
    if not all_players:
        return result

    # 1. Clean existing agent names
    for p in all_players:
        p.agent = clean_agent_name(p.agent)

    # 2. Check if any players need template matching
    needs_template = any(p.agent is None for p in all_players)

    if needs_template and image_bytes and _CV2_AVAILABLE:
        try:
            nparr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                crops = extract_row_avatar_crops(img)
                # Map crops to players (first 5 to team1, last 5 to team2)
                for idx, p in enumerate(all_players):
                    if p.agent is None and idx < len(crops) and crops[idx] is not None:
                        matched_agent, score = match_agent_crop(crops[idx])
                        if matched_agent and score >= 0.40:
                            p.agent = matched_agent
                            log.info("CV Matched agent for row %d (%s): %s (conf=%.2f)", idx, p.ign, matched_agent, score)
        except Exception as exc:
            log.warning("Agent template matching fallback error: %s", exc)

    return result
