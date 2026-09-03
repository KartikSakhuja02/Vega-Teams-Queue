"""
utils/ocr/validator.py
-----------------------
Field-level and match-level validation + confidence scoring.

Validation pipeline per numeric field:
  raw OCR string
    → syntax clean (fix l→1, O→0, S→5, etc.)
    → format validation (is it digits? correct pattern?)
    → range validation (within absolute bounds?)
    → normal-range check (within typical Valorant values?)
    → confidence assignment

Match-level validation:
  → exactly 10 players (5+5)
  → team scores plausible (0–13 rounds for standard match)
  → no impossible aggregate stats

Targeted re-OCR hint: if a field has confidence < RETRIGGER_THRESHOLD,
the pipeline will retry with additional preprocessing variants.
"""
from __future__ import annotations

import re
from typing import Optional

# ── Numeric substitution rules for OCR errors ─────────────────────────────────
# Apply ONLY to numeric fields, never to player names.
_NUM_SUBS: list[tuple[str, str]] = [
    ("O", "0"), ("o", "0"),
    ("l", "1"), ("I", "1"), ("i", "1"), ("|", "1"),
    ("S", "5"), ("s", "5"),
    ("B", "8"),
    ("G", "6"), ("b", "6"),
    ("Z", "2"),
    (" ", ""),   # remove spaces
]

_KDA_SUBS: list[tuple[str, str]] = [
    ("O", "0"), ("o", "0"),
    ("l", "/"), ("I", "/"), ("|", "/"),
    ("B", "8"), ("S", "5"),
]


def _clean_digits(raw: str) -> str:
    s = raw.strip()
    for old, new in _NUM_SUBS:
        s = s.replace(old, new)
    return re.sub(r"\D", "", s)   # keep only digits


def _clean_kda(raw: str) -> str:
    s = raw.strip()
    for old, new in _KDA_SUBS:
        s = s.replace(old, new)
    # Normalise various separators to "/"
    s = re.sub(r"[\s\-_]+", "/", s)
    s = re.sub(r"/+", "/", s)     # deduplicate slashes
    return s


# ── Field specs ────────────────────────────────────────────────────────────────
# Each tuple: (absolute_min, absolute_max, normal_min, normal_max)
#   absolute: hard impossible bounds (anything outside = invalid)
#   normal:   typical Valorant values (outside normal → lower confidence)
_SPECS: dict[str, tuple[int, int, int, int]] = {
    "acs":     (0, 900,  30, 700),
    "kills":   (0, 50,   0,  35),
    "deaths":  (0, 50,   0,  30),
    "assists": (0, 50,   0,  30),
    "dmg":     (0, 15000, 100, 8000),
    "fb":      (0, 15,   0,  8),
    "plants":  (0, 15,   0,  10),
    "defuses": (0, 15,   0,  10),
}

RETRIGGER_THRESHOLD = 0.45   # retry OCR if confidence below this


def _confidence_from_value(value: int, field: str) -> float:
    """
    Assign a 0–1 confidence based on whether the parsed integer
    falls within absolute and normal bounds.
    """
    abs_lo, abs_hi, norm_lo, norm_hi = _SPECS[field]
    if not (abs_lo <= value <= abs_hi):
        return 0.0   # impossible value
    if norm_lo <= value <= norm_hi:
        return 1.0   # in normal range
    # Outside normal but within absolute → penalise proportionally
    if value < norm_lo:
        return 0.5
    # value > norm_hi
    excess = value - norm_hi
    span   = abs_hi - norm_hi
    return max(0.1, 1.0 - 0.5 * (excess / max(1, span)))


# ── Consensus helper ───────────────────────────────────────────────────────────

def consensus_int(values: list[str], field: str) -> tuple[int, float]:
    """
    From a list of raw OCR strings (one per preprocessing variant),
    return (best_int_value, confidence).

    Strategy:
      1. Clean and parse each string.
      2. Vote: pick the most-common valid value.
      3. If the winner has ≥ 2 votes out of 3 variants → high confidence.
      4. Otherwise take the value with best individual confidence.
    """
    parsed: dict[int, int] = {}   # value → vote count
    raw_cleaned: list[str] = []

    for raw in values:
        cleaned = _clean_digits(raw)
        raw_cleaned.append(cleaned)
        if not cleaned:
            continue
        try:
            v = int(cleaned)
        except ValueError:
            continue
        abs_lo, abs_hi, _, _ = _SPECS[field]
        if abs_lo <= v <= abs_hi:
            parsed[v] = parsed.get(v, 0) + 1

    if not parsed:
        return 0, 0.0

    # Consensus winner
    winner = max(parsed, key=lambda v: (parsed[v], -v if v > 0 else v))
    votes = parsed[winner]
    n_total = len(values)

    base_conf = _confidence_from_value(winner, field)
    # Scale by agreement ratio
    agree_bonus = (votes / n_total) * 0.4
    confidence = min(1.0, base_conf + agree_bonus)

    return winner, round(confidence, 3)


def validate_int(raw: str, field: str) -> tuple[int, float]:
    """Single-variant int validation (no consensus)."""
    cleaned = _clean_digits(raw)
    if not cleaned:
        return 0, 0.0
    try:
        v = int(cleaned)
    except ValueError:
        return 0, 0.0
    return v, _confidence_from_value(v, field)


def consensus_kda(values: list[str]) -> tuple[int, int, int, float]:
    """
    Parse K/D/A from multiple preprocessing variants.
    Returns (kills, deaths, assists, confidence).
    """
    triples: dict[tuple, int] = {}

    def _parse_one(raw: str) -> Optional[tuple[int, int, int]]:
        cleaned = _clean_kda(raw)
        m = re.search(r"(\d+)/(\d+)/(\d+)", cleaned)
        if not m:
            # Try extracting any three digit groups
            nums = re.findall(r"\d+", cleaned)
            if len(nums) >= 3:
                m2 = tuple(int(x) for x in nums[:3])
                return m2  # type: ignore
            return None
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

    for raw in values:
        t = _parse_one(raw)
        if t:
            # Validate individually
            k, d, a = t
            if (0 <= k <= 50 and 0 <= d <= 50 and 0 <= a <= 50):
                triples[t] = triples.get(t, 0) + 1

    if not triples:
        return 0, 0, 0, 0.0

    winner = max(triples, key=lambda v: (triples[v], sum(v)))
    votes = triples[winner]
    k, d, a = winner

    # Confidence: all individual fields' confidence + agreement
    avg_c = (
        _confidence_from_value(k, "kills")
        + _confidence_from_value(d, "deaths")
        + _confidence_from_value(a, "assists")
    ) / 3
    agree_bonus = (votes / len(values)) * 0.3
    confidence = min(1.0, avg_c + agree_bonus)
    return k, d, a, round(confidence, 3)


def validate_ign(raw: str) -> tuple[str, float]:
    """
    Validate and clean a player IGN.
    IGNs can contain letters, digits, spaces, Chinese chars, and common symbols.
    Do NOT aggressively strip — preserve legitimate characters.
    Only remove obvious OCR noise.
    """
    # Strip leading/trailing whitespace
    cleaned = raw.strip()
    # Remove known UI badge labels (but not the player's own name)
    cleaned = re.sub(r"(我方|敌方|最佳)\s*[-—·]?\s*(最佳)?", "", cleaned)
    cleaned = re.sub(r"\bMVP\b", "", cleaned, flags=re.IGNORECASE)
    # Collapse internal whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Remove leading/trailing punctuation that's unlikely to be part of an IGN
    cleaned = cleaned.strip(".,;!?\"'`~")

    if not cleaned or len(cleaned) < 1:
        return "Unknown", 0.0

    # Confidence based on length and character composition
    has_alphanum = bool(re.search(r"[a-zA-Z0-9\u4e00-\u9fff]", cleaned))
    if not has_alphanum:
        return cleaned, 0.2

    # Short IGNs (1-2 chars) are plausible but lower confidence
    conf = 0.7 if len(cleaned) <= 2 else 0.9
    return cleaned, conf


def validate_match(
    t1_players: list,
    t2_players: list,
    t1_score: int,
    t2_score: int,
) -> tuple[float, list[str]]:
    """
    Validate the complete match result.
    Returns (overall_confidence, list_of_warnings).
    """
    warnings: list[str] = []
    penalties = 0.0

    if len(t1_players) != 5:
        warnings.append(f"Team 1 has {len(t1_players)} players (expected 5)")
        penalties += 0.4
    if len(t2_players) != 5:
        warnings.append(f"Team 2 has {len(t2_players)} players (expected 5)")
        penalties += 0.4

    if not (0 <= t1_score <= 13 and 0 <= t2_score <= 13):
        warnings.append(f"Implausible score: {t1_score}–{t2_score}")
        penalties += 0.3

    # Average confidence across all players' key fields
    all_players = list(t1_players) + list(t2_players)
    if all_players:
        avg_conf = sum(p.overall_confidence for p in all_players) / len(all_players)
    else:
        avg_conf = 0.0
        penalties += 0.5

    overall = max(0.0, avg_conf - penalties)
    return round(overall, 3), warnings
