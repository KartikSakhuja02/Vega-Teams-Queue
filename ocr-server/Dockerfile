# ── Base image ────────────────────────────────────────────────────────────────
# Use RunPod's official PyTorch base: CUDA + Python + torch pre-installed.
# This saves 10+ minutes of build time vs. building from scratch.
FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

WORKDIR /app

# ── System dependencies ───────────────────────────────────────────────────────
# opencv-python-headless needs these at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY config.py preprocessing.py model.py parser.py validator.py confidence.py handler.py ./

# ── Environment ───────────────────────────────────────────────────────────────
# Model weights go on a RunPod Network Volume mounted at /workspace.
# Set HF_HOME to the same volume so HuggingFace caches there too.
ENV MODEL_CACHE_DIR=/workspace/models
ENV HF_HOME=/workspace/hf_cache
ENV PYTHONUNBUFFERED=1
ENV TOKENIZERS_PARALLELISM=false

# ── Health check ──────────────────────────────────────────────────────────────
# RunPod Serverless doesn't use HEALTHCHECK, but useful for GPU Pod testing
HEALTHCHECK --interval=30s --timeout=10s CMD python -c "import torch; print(torch.cuda.is_available())"

# ── Entry point ───────────────────────────────────────────────────────────────
CMD ["python", "-u", "handler.py"]
