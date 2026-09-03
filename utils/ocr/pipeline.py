"""
utils/ocr/pipeline.py
----------------------
Main OCR orchestration pipeline.

Flow:
  1. Decode + load image
  2. Detect table geometry (HSV-based)
  3. OCR score + metadata from header region
  4. For each of 10 player rows:
       a. Crop all cells
       b. Run multi-variant OCR (consensus for numbers, best-pick for names)
       c. Validate every field, assign confidence
       d. Targeted re-OCR for fields below RETRIGGER_THRESHOLD
  5. Match-level validation → overall confidence
  6. Set needs_review = True if confidence < AUTO_COMMIT_THRESHOLD

Architecture:
  Dynamic detection  →  if success, use detected geometry
                    ↘  if failure, use calibrated fallback proportions
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import numpy as np

from utils.ocr.models import FieldResult, MatchOCRResult, PlayerRowStats
from utils.ocr.detector import TABLE_COLS, N_PLAYERS_PER_TEAM, TableGeometry, detect_table_geometry
from utils.ocr.preprocessor import DIGIT_VARIANTS, KDA_VARIANTS, TEXT_VARIANTS, variant_a, variant_b, variant_d, variant_e
from utils.ocr.validator import (
    RETRIGGER_THRESHOLD,
    consensus_int,
    consensus_kda,
    validate_ign,
    validate_int,
    validate_match,
)
from utils.ocr.engines.tesseract_engine import (
    is_available as tess_available,
    ocr_int_consensus,
    ocr_ign,
    ocr_kda_consensus,
    ocr_meta_region,
    ocr_score_region,
)
import utils.ocr.debug as dbg

log = logging.getLogger(__name__)

# If overall confidence is below this, flag for human review
AUTO_COMMIT_THRESHOLD = 0.55

# ── Fallback row positions (% of image height) ─────────────────────────────
# Calibrated from 6 real Valorant Mobile screenshots with different layouts.
# The wider range (0.27–0.32 start) handles both compact and padded UIs.
# Team 1 rows come first (0–4), Team 2 rows second (5–9).
_FALLBACK_ROWS_PCT: list[tuple[float, float]] = [
    (0.272, 0.352), (0.352, 0.432), (0.432, 0.508), (0.508, 0.584), (0.584, 0.660),  # T1
    (0.665, 0.740), (0.740, 0.815), (0.815, 0.888), (0.888, 0.956), (0.956, 1.000),  # T2
]

# Fallback column positions (% of IMAGE width)
# Slightly wider crops than table-relative to compensate for unknown table x0.
_FALLBACK_COLS: dict[str, tuple[float, float]] = {
    "ign":     (0.155, 0.340),
    "acs":     (0.330, 0.428),
    "kda":     (0.414, 0.565),
    "dmg":     (0.550, 0.705),
    "fb":      (0.695, 0.782),
    "plants":  (0.770, 0.855),
    "defuses": (0.842, 0.925),
}


def _load(image_bytes: bytes) -> Optional[np.ndarray]:
    try:
        import cv2
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return img
    except Exception as e:
        log.error("Image load error: %s", e)
        return None


def _parse_score(txt: str) -> tuple[int, int, str]:
    # First look for the explicit "N 获胜 M" pattern
    m = re.search(r"(\d{1,2})\s*获胜\s*(\d{1,2})", txt)
    if m:
        return int(m.group(1)), int(m.group(2)), "Victory"
    # Fallback: two standalone 1-2 digit numbers (cap at 20 to avoid e.g. "27"→"2"+"7")
    nums = re.findall(r"\b(\d{1,2})\b", txt)
    valid = [int(n) for n in nums if 0 <= int(n) <= 20]
    if len(valid) >= 2:
        a, b = valid[0], valid[1]
        return a, b, ("Victory" if a >= b else "Defeat")
    return 0, 0, "Unknown"


def _parse_meta(txt: str) -> tuple[str, str, str]:
    map_name = match_date = duration = "Unknown"
    keywords = r"模式|明珠|深海|莲华|古城|Lotus|Pearl|Ascent|Haven|Split|Bind|Breeze|Fracture|Icebox|Sunset|Abyss|微风岛屿|亚海悬城|源工重镇"
    m = re.search(rf"([^\n\r]*(?:{keywords})[^\n\r]*)", txt)
    if m:
        map_name = m.group(1).strip()[:60]
    elif txt.strip():
        map_name = txt.splitlines()[0].strip()[:40] or "Unknown"
    dm = re.search(r"(\d{4}[/\-]\d{2}[/\-]\d{2}(?:\s+\d{2}:\d{2})?)", txt)
    if dm:
        match_date = dm.group(1)
    dr = re.search(r"用时\s*(\d{1,2}:\d{2})", txt) or re.search(r"\b(\d{2}:\d{2})\b", txt)
    if dr:
        duration = dr.group(1)
    return map_name, match_date, duration


def _crop_geom(img: np.ndarray, geom: TableGeometry, row_idx: int, col_key: str) -> np.ndarray:
    """Crop a cell using detected TableGeometry."""
    y0, y1 = geom.rows[row_idx]
    x0, x1 = geom.abs_col(col_key)
    h, w = img.shape[:2]
    return img[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]


def _crop_fallback(img: np.ndarray, row_idx: int, col_key: str) -> np.ndarray:
    """Crop a cell using calibrated fallback percentages."""
    h, w = img.shape[:2]
    y0f, y1f = _FALLBACK_ROWS_PCT[row_idx]
    x0f, x1f = _FALLBACK_COLS[col_key]
    return img[int(y0f * h):int(y1f * h), int(x0f * w):int(x1f * w)]


def _extra_variants(col_key: str) -> list:
    """Variants to add during targeted re-OCR (different from initial variant_d)."""
    return [variant_a, variant_c]   # 2 extras; total with initial = 3-way consensus


# Only re-OCR these fields (the most impactful for match stats; fb/plants/defuses
# are low-value enough that one pass is sufficient)
_REOCR_FIELDS = {"acs", "kda", "dmg", "ign"}


def _ocr_player_row(
    img: np.ndarray,
    row_idx: int,
    geom: Optional[TableGeometry],
    is_team1: bool,
    debug_dir: Optional[Path] = None,
) -> PlayerRowStats:
    """OCR one player row and return a validated PlayerRowStats."""

    def crop(key: str) -> np.ndarray:
        if geom is not None:
            return _crop_geom(img, geom, row_idx, key)
        return _crop_fallback(img, row_idx, key)

    team_label = "Team 1 (Green)" if is_team1 else "Team 2 (Red)"

    # ── IGN ──────────────────────────────────────────────────────────────────
    ign_crop = crop("ign")
    raw_ign  = ocr_ign(ign_crop, TEXT_VARIANTS, lang="chi_sim+eng")
    ign, ign_conf = validate_ign(raw_ign)

    # Detect MVP badge from IGN text
    is_mvp = False
    mvp_type = None
    full_txt = raw_ign.lower()
    if "我方" in raw_ign and "最佳" in raw_ign:
        is_mvp, mvp_type = True, "Team MVP"
    elif "敌方" in raw_ign and "最佳" in raw_ign:
        is_mvp, mvp_type = True, "Match MVP"
    elif re.search(r"mvp", full_txt):
        is_mvp, mvp_type = True, "Team MVP"

    # ── ACS ──────────────────────────────────────────────────────────────────
    acs_crop = crop("acs")
    acs_raws = ocr_int_consensus(acs_crop, [variant_d])   # 1 variant initially
    acs, acs_conf = consensus_int(acs_raws, "acs")
    if acs_conf < RETRIGGER_THRESHOLD and "acs" in _REOCR_FIELDS:
        extra_raws = ocr_int_consensus(acs_crop, _extra_variants("acs"))
        acs, acs_conf = consensus_int(acs_raws + extra_raws, "acs")

    # ── K/D/A ─────────────────────────────────────────────────────────────────
    kda_crop = crop("kda")
    kda_raws = ocr_kda_consensus(kda_crop, [variant_d])
    kills, deaths, assists, kda_conf = consensus_kda(kda_raws)
    if kda_conf < RETRIGGER_THRESHOLD and "kda" in _REOCR_FIELDS:
        extra_kda = ocr_kda_consensus(kda_crop, _extra_variants("kda"))
        kills, deaths, assists, kda_conf = consensus_kda(kda_raws + extra_kda)

    # ── DMG ───────────────────────────────────────────────────────────────────
    dmg_crop = crop("dmg")
    dmg_raws = ocr_int_consensus(dmg_crop, [variant_d])
    dmg, dmg_conf = consensus_int(dmg_raws, "dmg")
    if dmg_conf < RETRIGGER_THRESHOLD and "dmg" in _REOCR_FIELDS:
        extra_dmg = ocr_int_consensus(dmg_crop, _extra_variants("dmg"))
        dmg, dmg_conf = consensus_int(dmg_raws + extra_dmg, "dmg")

    # ── FB (single pass — low-impact stat) ───────────────────────────────────
    fb_raws = ocr_int_consensus(crop("fb"), [variant_d])
    fb, fb_conf = consensus_int(fb_raws, "fb")

    # ── Plants (single pass) ─────────────────────────────────────────────────
    pl_raws = ocr_int_consensus(crop("plants"), [variant_d])
    pl, pl_conf = consensus_int(pl_raws, "plants")

    # ── Defuses (single pass) ────────────────────────────────────────────────
    df_raws = ocr_int_consensus(crop("defuses"), [variant_d])
    df, df_conf = consensus_int(df_raws, "defuses")

    # ── Debug ─────────────────────────────────────────────────────────────────
    if debug_dir:
        for field, c in [("ign", ign_crop), ("acs", acs_crop), ("kda", kda_crop),
                         ("dmg", dmg_crop), ("fb", crop("fb")),
                         ("plants", crop("plants")), ("defuses", crop("defuses"))]:
            dbg.save_cell(debug_dir, row_idx, field, c)

    return PlayerRowStats(
        ign=ign, team=team_label, is_mvp=is_mvp, mvp_type=mvp_type,
        acs=acs, kills=kills, deaths=deaths, assists=assists,
        damage=dmg, first_bloods=fb, plants=pl, defuses=df,
        ign_conf=ign_conf, acs_conf=acs_conf, kda_conf=kda_conf,
        dmg_conf=dmg_conf, fb_conf=fb_conf, plants_conf=pl_conf,
        defuses_conf=df_conf,
    )


# ── Main pipeline entry ────────────────────────────────────────────────────────

def run_pipeline(image_bytes: bytes) -> MatchOCRResult:
    """
    Full OCR pipeline. Always runs fully local.
    Returns MatchOCRResult; sets needs_review=True if confidence is low.
    """
    if not tess_available():
        return MatchOCRResult(
            success=False,
            error="Tesseract not installed. Run: sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim",
        )

    t0 = time.perf_counter()

    img = _load(image_bytes)
    if img is None:
        return MatchOCRResult(success=False, error="Could not decode image.")

    import cv2
    h, w = img.shape[:2]

    # ── Debug setup ───────────────────────────────────────────────────────────
    debug_enabled = os.getenv("DEBUG_OCR", "").lower() in ("1", "true", "yes")
    debug_dir: Optional[Path] = None
    if debug_enabled:
        debug_dir = dbg.make_debug_dir()
        dbg.save_original(debug_dir, img)

    # ── 1. Table detection ────────────────────────────────────────────────────
    geom = detect_table_geometry(img)
    using_fallback = geom is None
    if using_fallback:
        log.warning("Table detection failed — using calibrated fallback coordinates.")
    else:
        if debug_dir:
            dbg.save_detected_rows(debug_dir, img, geom.rows)

    # ── 2. Score ──────────────────────────────────────────────────────────────
    score_crop = img[0:int(h * 0.22), int(w * 0.25):int(w * 0.75)]
    score_txt  = ocr_score_region(score_crop)
    t1_score, t2_score, outcome = _parse_score(score_txt)

    # ── 3. Metadata ───────────────────────────────────────────────────────────
    meta_crop = img[int(h * 0.05):int(h * 0.22), int(w * 0.05):int(w * 0.40)]
    meta_txt  = ocr_meta_region(meta_crop)
    map_name, match_date, duration = _parse_meta(meta_txt)

    # ── 4. Player rows ────────────────────────────────────────────────────────
    t1_players: list[PlayerRowStats] = []
    t2_players: list[PlayerRowStats] = []

    for row_idx in range(N_PLAYERS_PER_TEAM * 2):
        is_team1 = row_idx < N_PLAYERS_PER_TEAM
        stats = _ocr_player_row(img, row_idx, geom, is_team1, debug_dir=debug_dir)
        (t1_players if is_team1 else t2_players).append(stats)

    # ── 5. Match-level validation ─────────────────────────────────────────────
    overall_conf, warnings = validate_match(t1_players, t2_players, t1_score, t2_score)
    if warnings:
        for w in warnings:
            log.warning("Match validation: %s", w)

    needs_review = overall_conf < AUTO_COMMIT_THRESHOLD

    ms = round((time.perf_counter() - t0) * 1000, 1)

    result = MatchOCRResult(
        success=bool(t1_players or t2_players),
        engine=f"OpenCV+Tesseract ({'fallback-coords' if using_fallback else 'detected'})",
        processing_time_ms=ms,
        confidence=overall_conf,
        needs_review=needs_review,
        map_name=map_name,
        match_date=match_date,
        duration=duration,
        team1_score=t1_score,
        team2_score=t2_score,
        outcome=outcome,
        team1_players=t1_players,
        team2_players=t2_players,
    )

    if not result.success:
        result.error = "No players detected."

    # ── Debug output ──────────────────────────────────────────────────────────
    if debug_dir:
        import dataclasses
        dbg.save_results(debug_dir, {
            "score": f"{t1_score}–{t2_score}",
            "outcome": outcome,
            "map": map_name,
            "confidence": overall_conf,
            "needs_review": needs_review,
            "using_fallback": using_fallback,
            "processing_ms": ms,
            "warnings": warnings,
            "team1": [dataclasses.asdict(p) for p in t1_players],
            "team2": [dataclasses.asdict(p) for p in t2_players],
        })

    log.info(
        "OCR done: %d+%d players | conf=%.2f | review=%s | %.0fms | %s",
        len(t1_players), len(t2_players), overall_conf, needs_review, ms,
        "fallback" if using_fallback else "detected",
    )
    return result
