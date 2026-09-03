"""
ocr-server/handler.py
----------------------
RunPod Serverless entry point.

Input (job["input"]):
    {
        "image": "<base64-encoded image bytes>"
    }

Output on success:
    {
        "success": true,
        "result": { ... scoreboard data ... },
        "processing_ms": 1234
    }

Output on failure:
    {
        "success": false,
        "error": "<human-readable error message>",
        "error_type": "<PreprocessingError|ModelError|ValidationError|...>"
    }

Multi-pass inference:
    Pass 1: Full scoreboard crop → model → validate
    Pass 2: If confidence < 0.60, retry with the full (uncropped) image
    (More passes = more latency; only 2 passes implemented to keep cost down)

Model loading:
    The model is loaded at module import time (before the first request).
    RunPod keeps the worker alive between requests, so this is correct.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import traceback
from pathlib import Path
from typing import Optional

import runpod

from config import CFG
from preprocessing import PreprocessingError, prepare_image, detect_scoreboard_crop, resize_for_model
from model import get_model
from parser import parse_model_output
from validator import validate
from confidence import compute_confidence, annotate_result

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.DEBUG if CFG.debug else logging.INFO,
)
log = logging.getLogger(__name__)

# ── Pre-load the model at worker startup ──────────────────────────────────────
# This runs ONCE when the worker process starts, not on every request.
# RunPod workers stay alive between requests, so the model stays in VRAM.
log.info("Worker starting — pre-loading model…")
try:
    _model = get_model()
    log.info("Model ready.")
except Exception as _e:
    log.critical("FATAL: Model failed to load: %s", _e)
    raise


# ── Debug helpers ─────────────────────────────────────────────────────────────

def _save_debug(request_id: str, artifacts: dict) -> None:
    if not CFG.debug:
        return
    import cv2
    debug_path = Path(CFG.debug_dir) / request_id
    debug_path.mkdir(parents=True, exist_ok=True)
    for name, data in artifacts.items():
        path = debug_path / name
        if isinstance(data, (dict, list)):
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        elif hasattr(data, "shape"):   # numpy array (image)
            cv2.imwrite(str(path), data)
    log.debug("Debug artifacts saved to %s", debug_path)


# ── Single inference pass ─────────────────────────────────────────────────────

def _run_pass(pil_image) -> tuple[dict, list[str], float]:
    """Run one model inference pass. Returns (parsed, warnings, validation_score)."""
    raw = _model.extract_scoreboard(pil_image)
    parsed = parse_model_output(raw)
    validated, warnings, val_score = validate(parsed)
    return validated, warnings, val_score


# ── Main handler ──────────────────────────────────────────────────────────────

def handler(job: dict) -> dict:
    """
    RunPod Serverless handler function.

    Called once per request. The model (_model) is already loaded.
    """
    t_start = time.perf_counter()
    request_id = job.get("id", "unknown")
    job_input  = job.get("input") or {}

    log.info("Request %s received", request_id)

    # ── 1. Decode input ───────────────────────────────────────────────────────
    image_b64 = job_input.get("image")
    if not image_b64:
        return {"success": False, "error": "Missing 'image' field in input", "error_type": "InputError"}

    try:
        image_bytes = base64.b64decode(image_b64)
    except Exception as e:
        return {"success": False, "error": f"Base64 decode failed: {e}", "error_type": "InputError"}

    # ── 2. Preprocess ─────────────────────────────────────────────────────────
    try:
        bgr_for_debug, pil_cropped = prepare_image(image_bytes)
        # Also prepare full (uncropped) image for Pass 2 fallback
        import numpy as np
        import cv2
        arr = np.frombuffer(image_bytes, np.uint8)
        bgr_full = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        bgr_full_resized = resize_for_model(bgr_full, CFG.max_image_px)
        from PIL import Image as PILImage
        pil_full = PILImage.fromarray(cv2.cvtColor(bgr_full_resized, cv2.COLOR_BGR2RGB))
    except PreprocessingError as e:
        return {"success": False, "error": str(e), "error_type": "PreprocessingError"}
    except Exception as e:
        log.exception("Unexpected preprocessing error")
        return {"success": False, "error": f"Preprocessing failed: {e}", "error_type": "PreprocessingError"}

    # ── 3. Pass 1: Cropped image ──────────────────────────────────────────────
    try:
        validated, warnings, val_score = _run_pass(pil_cropped)
        confidence, label = compute_confidence(val_score, validated)
        log.info("Pass 1: confidence=%.2f (%s), warnings=%d", confidence, label, len(warnings))

        debug_artifacts: dict = {}
        if CFG.debug:
            debug_artifacts["pass1_parsed.json"] = validated
            debug_artifacts["pass1_warnings.json"] = warnings
            debug_artifacts["scoreboard_crop.png"] = bgr_for_debug
    except Exception as e:
        log.exception("Model inference failed on Pass 1")
        return {"success": False, "error": f"Model inference failed: {e}", "error_type": "ModelError"}

    # ── 4. Pass 2: Full image (only if Pass 1 is LOW confidence) ─────────────
    if label == "LOW":
        log.info("Pass 1 LOW confidence — retrying with full image (Pass 2)…")
        try:
            validated2, warnings2, val_score2 = _run_pass(pil_full)
            confidence2, label2 = compute_confidence(val_score2, validated2)
            log.info("Pass 2: confidence=%.2f (%s)", confidence2, label2)

            if confidence2 > confidence:
                log.info("Pass 2 better — using Pass 2 result")
                validated, warnings, val_score, confidence, label = (
                    validated2, warnings2, val_score2, confidence2, label2
                )
                if CFG.debug:
                    debug_artifacts["pass2_parsed.json"] = validated2
        except Exception as e:
            log.warning("Pass 2 failed: %s — keeping Pass 1 result", e)

    # ── 5. Annotate result ────────────────────────────────────────────────────
    result = annotate_result(validated, confidence, label)
    result["validation_warnings"] = warnings

    # ── 6. Debug output ───────────────────────────────────────────────────────
    if CFG.debug:
        debug_artifacts["final_result.json"] = result
        _save_debug(request_id, debug_artifacts)

    elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
    result["processing_ms"] = elapsed_ms

    log.info(
        "Request %s done: conf=%.2f (%s), needs_review=%s, %.0f ms",
        request_id, confidence, label, result["needs_review"], elapsed_ms,
    )
    return {"success": True, "result": result}


# ── RunPod entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
