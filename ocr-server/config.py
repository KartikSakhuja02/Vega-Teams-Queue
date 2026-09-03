"""
ocr-server/config.py
---------------------
Central configuration. All settings read from environment variables
so nothing needs to be hard-coded or changed between environments.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # ── Model ────────────────────────────────────────────────────────────────
    model_name: str = os.getenv("MODEL_NAME", "Qwen/Qwen2.5-VL-7B-Instruct")
    # Where to cache model weights (use a RunPod Network Volume here)
    model_cache_dir: str = os.getenv("MODEL_CACHE_DIR", "/workspace/models")
    # HuggingFace cache root (also set to network volume to avoid re-download)
    hf_home: str = os.getenv("HF_HOME", "/workspace/hf_cache")

    # ── Inference ─────────────────────────────────────────────────────────────
    # Maximum number of tokens the model generates (JSON output)
    max_new_tokens: int = int(os.getenv("MAX_NEW_TOKENS", "2048"))
    # Temperature — keep low for structured extraction (less randomness)
    temperature: float = float(os.getenv("TEMPERATURE", "0.05"))
    # Attention implementation: "sdpa" (PyTorch native, no extra dep)
    # or "flash_attention_2" (faster but needs flash-attn compiled)
    attn_impl: str = os.getenv("ATTN_IMPL", "sdpa")
    # torch dtype: "bfloat16" (recommended for Ampere/Ada GPUs) or "float16"
    torch_dtype: str = os.getenv("TORCH_DTYPE", "bfloat16")

    # ── Image preprocessing ───────────────────────────────────────────────────
    # Crop scoreboard before sending to model (usually improves accuracy)
    crop_scoreboard: bool = os.getenv("CROP_SCOREBOARD", "true").lower() == "true"
    # Maximum image dimension after resize (preserves aspect ratio)
    max_image_px: int = int(os.getenv("MAX_IMAGE_PX", "1280"))
    # Minimum dimension — reject images smaller than this (likely invalid)
    min_image_px: int = int(os.getenv("MIN_IMAGE_PX", "400"))

    # ── Validation thresholds ─────────────────────────────────────────────────
    # Overall confidence below which we return needs_review=True
    min_confidence: float = float(os.getenv("MIN_CONFIDENCE", "0.60"))
    # Per-player confidence below which we flag that player's fields
    per_player_min: float = float(os.getenv("PER_PLAYER_MIN", "0.50"))

    # ── Debug ────────────────────────────────────────────────────────────────
    debug: bool = os.getenv("DEBUG_OCR", "false").lower() in ("1", "true", "yes")
    debug_dir: str = os.getenv("DEBUG_DIR", "/tmp/debug_ocr")


# Module-level singleton — import this everywhere
CFG = Config()

# Propagate HF_HOME so transformers finds the cached weights
os.environ["HF_HOME"] = CFG.hf_home
os.environ["TRANSFORMERS_CACHE"] = CFG.model_cache_dir
