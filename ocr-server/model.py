"""
ocr-server/model.py
--------------------
VLM abstraction layer.

Architecture:
    VisionOCRModel          ← abstract base class
    └── Qwen25VLModel       ← Qwen2.5-VL-7B-Instruct implementation

Swapping models = implement VisionOCRModel + change MODEL_CLASS in handler.py.

Model loading design:
  - Module-level singleton via get_model()
  - Loaded ONCE when the RunPod worker starts
  - Reused across all requests (no reload per request)

Prompt design:
  - Instructs the model to return ONLY JSON
  - Describes the Valorant CN scoreboard layout precisely
  - Provides column name translations (Chinese → field name)
  - Explicitly says to use null rather than guess
"""
from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Optional

import torch
from PIL import Image

from config import CFG

log = logging.getLogger(__name__)

# ── Scoreboard extraction prompt ───────────────────────────────────────────────
# This prompt is the single most important tuning parameter.
# Be explicit about layout, column names, and output format.

SCOREBOARD_PROMPT = """\
You are analyzing a Valorant Mobile (CN version) custom match end-screen scoreboard.

Return ONLY a valid JSON object. No explanation, no preamble, no markdown fences.

Layout description:
- TOP CENTER: Score written as "N 获胜 M" → team1_score = N, team2_score = M
- TOP LEFT area: Map name after "赛事模式-" (e.g. "莲华古城", "深海明珠", "源工重镇", "亚海悬城", "微风岛屿")
- TOP LEFT area: Date "YYYY/MM/DD HH:MM" and duration "用时 MM:SS"
- TABLE has exactly 10 player rows:
    • First 5 rows have GREEN / TEAL background → team = 1
    • Last  5 rows have RED / MAROON background → team = 2
- Table columns left-to-right:
    队伍排名 (player name column; may include badge)
    平均战斗评分  →  acs         (integer)
    击败/敌阵/助攻 →  kills/deaths/assists  (format: "K/D/A")
    对局总伤害    →  damage      (integer)
    率先击败     →  first_bloods (integer, often 0–5)
    部署         →  plants       (integer, often 0–5)
    拆除         →  defuses      (integer, often 0–5)
- MVP badges (small coloured tag next to player name):
    "我方-最佳" or "我方最佳" → is_mvp=true, mvp_type="Team MVP"
    "敌方-最佳" or "敌方最佳" → is_mvp=true, mvp_type="Match MVP"

Required JSON schema (no extra keys, no trailing commas):
{
  "success": true,
  "team1_score": <integer or null>,
  "team2_score": <integer or null>,
  "map": "<map name only, not the full path string>",
  "match_date": "<YYYY/MM/DD HH:MM or null>",
  "duration": "<MM:SS or null>",
  "outcome": "Victory",
  "players": [
    {
      "name": "<exact player name, preserve Chinese and English characters>",
      "team": <1 or 2>,
      "is_mvp": <true or false>,
      "mvp_type": <"Team MVP" or "Match MVP" or null>,
      "acs": <integer or null>,
      "kills": <integer or null>,
      "deaths": <integer or null>,
      "assists": <integer or null>,
      "damage": <integer or null>,
      "first_bloods": <integer or null>,
      "plants": <integer or null>,
      "defuses": <integer or null>
    }
  ]
}

Rules (strictly follow):
1. The "players" array MUST contain exactly 10 objects.
2. Players 1–5 (index 0–4): team=1. Players 6–10 (index 5–9): team=2.
3. If you cannot read a field with confidence, write null — do NOT guess.
4. The K/D/A column contains three numbers separated by "/" — kills/deaths/assists.
5. Return ONLY the JSON object. Nothing before or after it.
"""


class VisionOCRModel(ABC):
    """Abstract base class for vision-language scoreboard extractors."""

    @abstractmethod
    def extract_scoreboard(self, pil_image: Image.Image) -> dict:
        """
        Run model inference on the scoreboard image.
        Returns a raw dict (may be invalid — caller validates).
        Raises RuntimeError on hard model failure.
        """

    @abstractmethod
    def is_ready(self) -> bool:
        """Return True once the model is fully loaded and ready."""


# ── Qwen2.5-VL-7B-Instruct implementation ─────────────────────────────────────

class Qwen25VLModel(VisionOCRModel):
    """
    Qwen2.5-VL-7B-Instruct via HuggingFace transformers.

    VRAM usage:
      - BF16 weights: ~14 GB
      - Image tensors: 1–3 GB depending on resolution
      - Total: ~15–17 GB → fits on L4 (24 GB) with headroom

    Recommended GPU: RunPod L4 (24 GB) or RTX 4090 (24 GB)
    """

    def __init__(self, model_name: Optional[str] = None):
        from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor

        name = model_name or CFG.model_name
        dtype = torch.bfloat16 if CFG.torch_dtype == "bfloat16" else torch.float16

        log.info("Loading %s (dtype=%s, attn=%s) …", name, CFG.torch_dtype, CFG.attn_impl)
        t0 = time.perf_counter()

        self._processor = AutoProcessor.from_pretrained(
            name,
            cache_dir=CFG.model_cache_dir,
        )
        self._model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            name,
            torch_dtype=dtype,
            attn_implementation=CFG.attn_impl,
            device_map="auto",
            cache_dir=CFG.model_cache_dir,
        )
        self._model.eval()
        self._ready = True

        elapsed = time.perf_counter() - t0
        log.info("Model loaded in %.1f s", elapsed)

    def is_ready(self) -> bool:
        return getattr(self, "_ready", False)

    def extract_scoreboard(self, pil_image: Image.Image) -> dict:
        """
        Run full inference on a PIL RGB image.
        Returns the parsed JSON dict.
        """
        try:
            from qwen_vl_utils import process_vision_info
        except ImportError:
            raise RuntimeError(
                "qwen-vl-utils not installed. Add it to requirements.txt."
            )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": pil_image},
                    {"type": "text", "text": SCOREBOARD_PROMPT},
                ],
            }
        ]

        text = self._processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        image_inputs, video_inputs = process_vision_info(messages)
        inputs = self._processor(
            text=[text],
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt",
        ).to(self._model.device)

        t0 = time.perf_counter()
        with torch.no_grad():
            generated_ids = self._model.generate(
                **inputs,
                max_new_tokens=CFG.max_new_tokens,
                temperature=CFG.temperature,
                do_sample=(CFG.temperature > 0),
                repetition_penalty=1.05,
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info("Model inference: %.0f ms", elapsed_ms)

        # Trim input tokens from output
        trimmed = generated_ids[:, inputs["input_ids"].shape[1]:]
        raw_text = self._processor.batch_decode(
            trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
        )[0].strip()

        log.debug("Raw model output (first 500 chars): %s", raw_text[:500])
        return _parse_json_from_output(raw_text)


def _parse_json_from_output(text: str) -> dict:
    """
    Extract the JSON object from the model's text output.
    Handles cases where the model adds preamble/suffix despite instructions.
    """
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Find the first { … } block
    brace_start = text.find("{")
    brace_end   = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Model output is not valid JSON: {text[:300]!r}")


# ── Global model singleton ─────────────────────────────────────────────────────

_INSTANCE: Optional[VisionOCRModel] = None


def get_model(model_class=Qwen25VLModel) -> VisionOCRModel:
    """
    Return the global model singleton.
    Loaded once on first call; subsequent calls return the cached instance.
    This is the correct pattern for RunPod Serverless workers.
    """
    global _INSTANCE
    if _INSTANCE is None:
        log.info("Initialising model singleton (%s)…", model_class.__name__)
        _INSTANCE = model_class()
    return _INSTANCE
