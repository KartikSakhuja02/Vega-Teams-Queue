"""
benchmark.py
-------------
Compare VLM pipeline vs local Tesseract pipeline on labeled screenshots.

Usage:
    python benchmark.py \\
        --screenshots-dir ./test_screenshots \\
        --ground-truth    ./ground_truth.json \\
        --mode            both                 \\
        --output          benchmark_results.json

Ground truth JSON format:
{
  "screenshot_filename.png": {
    "team1_score": 13,
    "team2_score": 9,
    "map": "莲华古城",
    "players": [
      {
        "name": "otisFPS", "team": 1, "is_mvp": true, "mvp_type": "Team MVP",
        "acs": 300, "kills": 14, "deaths": 14, "assists": 8,
        "damage": 2524, "first_bloods": 1, "plants": 6, "defuses": 0
      },
      ... (10 players)
    ]
  }
}

Metrics reported:
  - Per-field accuracy (name, acs, kills, deaths, assists, damage, fb, plants, defuses, mvp)
  - Full-player accuracy (all fields correct for that player)
  - Full-match accuracy (all 10 players + score correct)
  - Average latency per screenshot
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ── Field comparison ──────────────────────────────────────────────────────────

def _match_int(pred: Optional[int], gt: Optional[int], tolerance: int = 0) -> bool:
    if gt is None:
        return True   # ground truth missing — skip
    if pred is None:
        return False
    return abs(pred - gt) <= tolerance


def _match_str(pred: Optional[str], gt: Optional[str]) -> bool:
    if gt is None:
        return True
    if pred is None:
        return False
    return pred.strip().lower() == gt.strip().lower()


def score_player(pred: dict, gt: dict) -> dict[str, bool]:
    """Return per-field correctness for one player."""
    return {
        "name":          _match_str(pred.get("name"), gt.get("name")),
        "acs":           _match_int(pred.get("acs"), gt.get("acs")),
        "kills":         _match_int(pred.get("kills"), gt.get("kills")),
        "deaths":        _match_int(pred.get("deaths"), gt.get("deaths")),
        "assists":       _match_int(pred.get("assists"), gt.get("assists")),
        "damage":        _match_int(pred.get("damage"), gt.get("damage"), tolerance=50),
        "first_bloods":  _match_int(pred.get("first_bloods"), gt.get("first_bloods")),
        "plants":        _match_int(pred.get("plants"), gt.get("plants")),
        "defuses":       _match_int(pred.get("defuses"), gt.get("defuses")),
        "is_mvp":        pred.get("is_mvp") == gt.get("is_mvp"),
    }


def score_match(pred_result, gt: dict) -> dict:
    """Score a full match result against ground truth."""
    # Convert MatchOCRResult or dict to dict of players
    if hasattr(pred_result, "all_players"):
        players_pred = [
            {
                "name": p.ign, "team": 1 if "1" in p.team else 2,
                "is_mvp": p.is_mvp, "acs": p.acs, "kills": p.kills,
                "deaths": p.deaths, "assists": p.assists, "damage": p.damage,
                "first_bloods": p.first_bloods, "plants": p.plants, "defuses": p.defuses,
            }
            for p in pred_result.all_players
        ]
        pred_score1 = pred_result.team1_score
        pred_score2 = pred_result.team2_score
    else:
        players_pred = pred_result.get("players") or []
        pred_score1  = pred_result.get("team1_score")
        pred_score2  = pred_result.get("team2_score")

    gt_players = gt.get("players") or []
    n = min(len(players_pred), len(gt_players), 10)

    player_scores = [score_player(players_pred[i], gt_players[i]) for i in range(n)]

    # Aggregate per-field accuracy
    fields = ["name", "acs", "kills", "deaths", "assists", "damage", "first_bloods", "plants", "defuses", "is_mvp"]
    field_acc = {f: sum(ps[f] for ps in player_scores) / max(1, n) for f in fields}

    # Full-player: all fields correct
    full_player = sum(all(ps.values()) for ps in player_scores) / max(1, n)

    # Score accuracy
    score_correct = (
        _match_int(pred_score1, gt.get("team1_score"))
        and _match_int(pred_score2, gt.get("team2_score"))
    )

    # Full-match: all players correct + score correct
    full_match = (full_player == 1.0) and score_correct

    return {
        "field_accuracy": field_acc,
        "full_player_accuracy": round(full_player, 3),
        "score_correct": score_correct,
        "full_match_correct": full_match,
        "n_players_predicted": len(players_pred),
        "n_players_gt": len(gt_players),
    }


# ── Pipeline runners ──────────────────────────────────────────────────────────

def run_tesseract(image_bytes: bytes):
    """Run local Tesseract pipeline synchronously."""
    from utils.ocr.pipeline import run_pipeline
    return run_pipeline(image_bytes)


async def run_vlm(image_bytes: bytes):
    """Run RunPod VLM pipeline."""
    from utils.ocr_client import extract_scoreboard
    return await extract_scoreboard(image_bytes)


# ── Main benchmark loop ───────────────────────────────────────────────────────

def run_benchmark(
    screenshots_dir: str,
    ground_truth_path: str,
    mode: str = "both",
    output_path: Optional[str] = None,
):
    screenshots_dir = Path(screenshots_dir)
    with open(ground_truth_path, encoding="utf-8") as f:
        ground_truth = json.load(f)

    results = {"tesseract": {}, "vlm": {}}
    all_files = sorted(ground_truth.keys())
    n = len(all_files)

    print(f"\nBenchmark: {n} screenshots, mode={mode}\n{'─'*60}")

    for i, filename in enumerate(all_files, 1):
        gt = ground_truth[filename]
        img_path = screenshots_dir / filename
        if not img_path.exists():
            print(f"[{i}/{n}] SKIP {filename} — file not found")
            continue

        image_bytes = img_path.read_bytes()
        print(f"[{i}/{n}] {filename}")

        if mode in ("tesseract", "both"):
            t0 = time.perf_counter()
            try:
                pred = run_tesseract(image_bytes)
                elapsed = (time.perf_counter() - t0) * 1000
                match_score = score_match(pred, gt)
                match_score["latency_ms"] = round(elapsed, 1)
                match_score["error"] = None
                print(f"  Tesseract: full_match={match_score['full_match_correct']}  {elapsed:.0f}ms")
            except Exception as e:
                match_score = {"error": str(e), "full_match_correct": False}
                print(f"  Tesseract: ERROR — {e}")
            results["tesseract"][filename] = match_score

        if mode in ("vlm", "both"):
            t0 = time.perf_counter()
            try:
                pred = asyncio.run(run_vlm(image_bytes))
                elapsed = (time.perf_counter() - t0) * 1000
                match_score = score_match(pred, gt)
                match_score["latency_ms"] = round(elapsed, 1)
                match_score["error"] = None
                print(f"  VLM:       full_match={match_score['full_match_correct']}  {elapsed:.0f}ms")
            except Exception as e:
                match_score = {"error": str(e), "full_match_correct": False}
                print(f"  VLM:       ERROR — {e}")
            results["vlm"][filename] = match_score

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"{'Metric':<25} {'Tesseract':>12} {'VLM':>12}")
    print(f"{'─'*60}")

    fields = ["name", "acs", "kills", "deaths", "assists", "damage", "first_bloods", "plants", "defuses", "is_mvp"]
    for mode_key, label in [("tesseract", "Tesseract"), ("vlm", "VLM")]:
        # Already printed inline; just compute aggregate here
        pass

    def _avg_field(engine_results: dict, field: str) -> float:
        vals = [
            r["field_accuracy"][field]
            for r in engine_results.values()
            if "field_accuracy" in r
        ]
        return sum(vals) / len(vals) if vals else 0.0

    def _full_match_acc(engine_results: dict) -> float:
        vals = [r.get("full_match_correct", False) for r in engine_results.values()]
        return sum(vals) / len(vals) if vals else 0.0

    def _avg_latency(engine_results: dict) -> float:
        vals = [r["latency_ms"] for r in engine_results.values() if "latency_ms" in r]
        return sum(vals) / len(vals) if vals else 0.0

    for field in fields:
        t_acc = _avg_field(results["tesseract"], field) * 100 if mode in ("tesseract", "both") else float("nan")
        v_acc = _avg_field(results["vlm"],       field) * 100 if mode in ("vlm",       "both") else float("nan")
        print(f"  {field:<23} {t_acc:>11.1f}%  {v_acc:>11.1f}%")

    print(f"{'─'*60}")
    t_fm  = _full_match_acc(results["tesseract"]) * 100 if mode in ("tesseract", "both") else float("nan")
    v_fm  = _full_match_acc(results["vlm"])       * 100 if mode in ("vlm",       "both") else float("nan")
    t_lat = _avg_latency(results["tesseract"])          if mode in ("tesseract", "both") else float("nan")
    v_lat = _avg_latency(results["vlm"])                if mode in ("vlm",       "both") else float("nan")
    print(f"  {'FULL MATCH accuracy':<23} {t_fm:>11.1f}%  {v_fm:>11.1f}%")
    print(f"  {'Avg latency (ms)':<23} {t_lat:>11.0f}   {v_lat:>11.0f}")
    print(f"{'═'*60}\n")

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"Full results saved to {output_path}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark VLM vs Tesseract OCR")
    parser.add_argument("--screenshots-dir", required=True, help="Directory of test screenshots")
    parser.add_argument("--ground-truth",    required=True, help="JSON file with ground-truth labels")
    parser.add_argument("--mode",            default="both", choices=["tesseract", "vlm", "both"])
    parser.add_argument("--output",          default=None,   help="Path to save full results JSON")
    args = parser.parse_args()

    run_benchmark(
        screenshots_dir=args.screenshots_dir,
        ground_truth_path=args.ground_truth,
        mode=args.mode,
        output_path=args.output,
    )
