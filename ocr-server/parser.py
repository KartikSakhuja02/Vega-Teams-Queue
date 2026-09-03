"""
ocr-server/parser.py
---------------------
Clean and normalize the raw dict returned by the model.

Responsibilities:
  - Type coercion (string "14" → int 14)
  - Handle null / None gracefully
  - Normalize player list structure
  - Extract K/D/A from string format ("14/14/8") if model returns it that way
  - Clean player names (strip stray whitespace)

Does NOT validate ranges — that's validator.py's job.
"""
from __future__ import annotations

import re
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# Valorant CN map name normalization
# Maps the Chinese names to their English equivalents for consistency.
# The model may return either.
_MAP_NORMALISE = {
    "莲华古城": "Lotus",
    "深海明珠": "Pearl",
    "源工重镇": "Ascent",
    "亚海悬城": "Bind",
    "微风岛屿": "Breeze",
    "冰箱": "Icebox",
    "碎片": "Fracture",
    "分裂": "Split",
    "天堂": "Haven",
    "日落": "Sunset",
    "深渊": "Abyss",
}


def _int_or_none(v: Any) -> Optional[int]:
    """Convert value to int, or None if invalid/null."""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip()
    # Handle "14/14/8" style (take first number)
    m = re.match(r"^(\d+)", s)
    if m:
        return int(m.group(1))
    return None


def _str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _parse_kda(raw: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Parse K/D/A from various formats:
      "14/14/8" → (14, 14, 8)
      14         → (14, None, None)
      None       → (None, None, None)
    """
    if raw is None:
        return None, None, None
    s = str(raw).strip()
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)", s)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    # Just a number
    m2 = re.match(r"^(\d+)$", s)
    if m2:
        return int(m2.group(1)), None, None
    return None, None, None


def _normalise_map(raw: Any) -> str:
    if raw is None:
        return "Unknown"
    s = str(raw).strip()
    # Strip "自定义-赛事模式-" prefix if present
    s = re.sub(r"^自定义[-\s]*赛事模式[-\s]*", "", s).strip()
    # Try to normalise known map names
    for cn, en in _MAP_NORMALISE.items():
        if cn in s:
            return cn   # Return Chinese name as-is (consistent with source)
    return s if s else "Unknown"


def _clean_name(raw: Any) -> str:
    if raw is None:
        return "Unknown"
    s = str(raw).strip()
    # Remove MVP badge text that leaked into the name
    s = re.sub(r"(我方|敌方)\s*[-—·]*\s*最佳", "", s)
    s = re.sub(r"\bMVP\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s if s else "Unknown"


def parse_model_output(raw: dict) -> dict:
    """
    Normalise the model's raw dict into a clean, typed structure.
    Returns a dict with the same schema but guaranteed types.
    Never raises — unknown or malformed fields become None/defaults.
    """
    players_raw = raw.get("players") or []
    players = []

    for i, p in enumerate(players_raw):
        if not isinstance(p, dict):
            p = {}

        team = p.get("team")
        team = int(team) if team in (1, 2) else (1 if i < 5 else 2)

        # K/D/A can come as individual fields or a combined string
        kills   = _int_or_none(p.get("kills"))
        deaths  = _int_or_none(p.get("deaths"))
        assists = _int_or_none(p.get("assists"))

        # Sometimes model returns "kills": "14/14/8" — handle that
        if kills is not None and deaths is None and assists is None:
            raw_kda = p.get("kills")
            if isinstance(raw_kda, str) and "/" in raw_kda:
                kills, deaths, assists = _parse_kda(raw_kda)

        # Also check if there's a "kda" field
        if "kda" in p and (kills is None or deaths is None):
            kills2, deaths2, assists2 = _parse_kda(p["kda"])
            kills   = kills   if kills   is not None else kills2
            deaths  = deaths  if deaths  is not None else deaths2
            assists = assists  if assists is not None else assists2

        mvp_type = _str_or_none(p.get("mvp_type"))
        is_mvp   = bool(p.get("is_mvp")) or mvp_type is not None

        players.append({
            "name":          _clean_name(p.get("name")),
            "team":          team,
            "is_mvp":        is_mvp,
            "mvp_type":      mvp_type,
            "acs":           _int_or_none(p.get("acs")),
            "kills":         kills,
            "deaths":        deaths,
            "assists":       assists,
            "damage":        _int_or_none(p.get("damage")),
            "first_bloods":  _int_or_none(p.get("first_bloods")),
            "plants":        _int_or_none(p.get("plants")),
            "defuses":       _int_or_none(p.get("defuses")),
        })

    return {
        "success":      bool(raw.get("success", True)),
        "team1_score":  _int_or_none(raw.get("team1_score")),
        "team2_score":  _int_or_none(raw.get("team2_score")),
        "map":          _normalise_map(raw.get("map")),
        "match_date":   _str_or_none(raw.get("match_date")),
        "duration":     _str_or_none(raw.get("duration")),
        "outcome":      _str_or_none(raw.get("outcome")) or "Unknown",
        "players":      players,
    }
