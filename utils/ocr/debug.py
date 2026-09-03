"""
utils/ocr/debug.py
-------------------
Debug mode: saves intermediate crops, preprocessed images, and a full
results JSON to a debug directory for every screenshot processed.

Enable by setting DEBUG_OCR=true in the environment, or by passing
debug_dir to the pipeline.

Output layout:
  <debug_dir>/
    original.png
    detected_rows.png       overlay showing detected row bands
    <rowN>_<field>.png      raw crop
    <rowN>_<field>_proc.png preprocessed crop
    results.json            full parsed + confidence data
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    import cv2
    import numpy as np
    _CV2 = True
except ImportError:
    _CV2 = False


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_original(debug_dir: Path, img: "np.ndarray") -> None:
    if not _CV2:
        return
    cv2.imwrite(str(debug_dir / "original.png"), img)


def save_detected_rows(
    debug_dir: Path,
    img: "np.ndarray",
    rows: list[tuple[int, int]],
    t1_count: int = 5,
) -> None:
    """Draw coloured overlays on a copy of the image showing detected rows."""
    if not _CV2:
        return
    vis = img.copy()
    for i, (y0, y1) in enumerate(rows):
        colour = (0, 180, 80) if i < t1_count else (60, 60, 200)
        cv2.rectangle(vis, (0, y0), (img.shape[1], y1), colour, 2)
        cv2.putText(vis, f"R{i+1}", (5, y0 + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, colour, 2)
    cv2.imwrite(str(debug_dir / "detected_rows.png"), vis)


def save_cell(
    debug_dir: Path,
    row_idx: int,
    field: str,
    raw_crop: "np.ndarray",
    proc_crop: "np.ndarray | None" = None,
) -> None:
    if not _CV2:
        return
    prefix = f"row{row_idx:02d}_{field}"
    cv2.imwrite(str(debug_dir / f"{prefix}.png"), raw_crop)
    if proc_crop is not None:
        cv2.imwrite(str(debug_dir / f"{prefix}_proc.png"), proc_crop)


def save_results(debug_dir: Path, data: dict[str, Any]) -> None:
    path = debug_dir / "results.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    log.info("Debug results written to %s", path)


def make_debug_dir(base: str = "debug_ocr") -> Path:
    """Create a timestamped debug directory."""
    import time
    ts = int(time.time())
    path = Path(base) / str(ts)
    return _ensure_dir(path)
