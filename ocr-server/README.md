# Vega Scrims — RunPod VLM OCR Server

Runs **Qwen2.5-VL-7B-Instruct** on a RunPod L4 GPU to extract Valorant
scoreboard data from screenshots.

---

## Architecture

```
Railway Bot → HTTPS → RunPod Serverless → L4 GPU → Qwen2.5-VL-7B → JSON
                                              ↑
                               Network Volume (/workspace/models)
```

---

## 1. Prerequisites

- RunPod account at https://runpod.io
- Docker installed locally
- Docker Hub or GHCR account for pushing the image

---

## 2. Create a RunPod Network Volume

The model weights (~14 GB) are stored on a persistent Network Volume
so each worker does not re-download them on every cold start.

1. RunPod Console → **Storage** → **+ Network Volume**
2. Name: `vega-models`
3. Size: `30 GB` (leaves room for HF cache)
4. Region: choose the same region as your serverless endpoint
5. Click **Create**

### Download the model to the volume

1. Create a **one-time GPU Pod** (cheapest: A10 or RTX 3090 1× GPU)
2. Attach the **vega-models** network volume at `/workspace`
3. Connect via SSH or Web Terminal
4. Run:

```bash
pip install huggingface_hub
python - <<'EOF'
from huggingface_hub import snapshot_download
snapshot_download(
    "Qwen/Qwen2.5-VL-7B-Instruct",
    local_dir="/workspace/models/Qwen2.5-VL-7B-Instruct",
    ignore_patterns=["*.gguf", "*.bin"],   # use .safetensors only
)
print("Done.")
EOF
```

5. **Stop and delete** the GPU Pod (you're only billed while it runs).
   The Network Volume persists.

---

## 3. Build & Push the Docker Image

```bash
cd ocr-server

# Build (replace with your Docker Hub username)
docker build -t yourdockerhub/vega-ocr-server:latest .

# Push
docker push yourdockerhub/vega-ocr-server:latest
```

> **Note**: The Docker image is small (~4 GB) because the model is on the
> Network Volume, not baked into the image.

---

## 4. Create the Serverless Endpoint

1. RunPod Console → **Serverless** → **+ New Endpoint**
2. **Docker image**: `yourdockerhub/vega-ocr-server:latest`
3. **GPU**: `NVIDIA L4` (24 GB VRAM) — primary recommendation
   - Alternative: `RTX 4090` (24 GB, ~$0.74/hr, faster)
4. **Min workers**: `0` (scale to zero when idle — cheapest)
5. **Max workers**: `3` (or however many concurrent matches you expect)
6. **Container disk**: `20 GB`
7. **Network volume**: attach `vega-models` at `/workspace`
8. **Environment variables** (set these in the endpoint config):

```
MODEL_NAME=Qwen/Qwen2.5-VL-7B-Instruct
MODEL_CACHE_DIR=/workspace/models/Qwen2.5-VL-7B-Instruct
HF_HOME=/workspace/hf_cache
MAX_NEW_TOKENS=2048
TEMPERATURE=0.05
ATTN_IMPL=sdpa
TORCH_DTYPE=bfloat16
DEBUG_OCR=false
```

9. Click **Deploy**

---

## 5. Get Your Endpoint ID & API Key

- **Endpoint ID**: shown on the endpoint card (e.g., `abc123xyz`)
- **API Key**: Profile → **API Keys** → **+ Create API Key**

---

## 6. Configure Railway

In Railway → your bot service → **Variables**, add:

```
RUNPOD_API_KEY=rpa_your_key_here
RUNPOD_ENDPOINT_ID=abc123xyz
RUNPOD_TIMEOUT_S=120
```

The bot will automatically use RunPod when these are set.
If RunPod is unavailable, it falls back to local Tesseract.

---

## 7. Test the Endpoint

### Direct API test (curl):

```bash
# Set your credentials
export RUNPOD_API_KEY=rpa_your_key
export ENDPOINT_ID=your_endpoint_id

# Encode a test screenshot
IMAGE_B64=$(base64 -w 0 /path/to/scoreboard.png)

# Submit job
RESPONSE=$(curl -s -X POST \
  "https://api.runpod.ai/v2/$ENDPOINT_ID/run" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"input\": {\"image\": \"$IMAGE_B64\"}}")

echo $RESPONSE
JOB_ID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Poll for result
curl -s \
  "https://api.runpod.ai/v2/$ENDPOINT_ID/status/$JOB_ID" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" | python3 -m json.tool
```

### Python test:

```python
import asyncio, os
os.environ["RUNPOD_API_KEY"] = "rpa_your_key"
os.environ["RUNPOD_ENDPOINT_ID"] = "your_endpoint_id"

from utils.ocr_client import extract_scoreboard

with open("scoreboard.png", "rb") as f:
    image_bytes = f.read()

result = asyncio.run(extract_scoreboard(image_bytes))
print(f"Confidence: {result.confidence:.2f}")
print(f"Score: {result.team1_score} – {result.team2_score}")
for p in result.all_players:
    print(f"  {p.ign}: {p.acs} ACS, {p.kills}/{p.deaths}/{p.assists}")
```

---

## 8. Benchmark (compare VLM vs Tesseract)

```bash
# Create ground truth for your test screenshots
# See benchmark.py for the JSON schema

python benchmark.py \
  --screenshots-dir ./test_screenshots \
  --ground-truth    ./ground_truth.json \
  --mode            both \
  --output          benchmark_results.json
```

---

## 9. Expected Performance

| Metric | Value |
|---|---|
| GPU | L4 (24 GB) |
| Model | Qwen2.5-VL-7B-Instruct (BF16) |
| VRAM usage | ~15–17 GB |
| Warm inference | 5–12 s per screenshot |
| Cold start (model load) | 15–30 s from Network Volume |
| Cost per screenshot | ~$0.005–0.01 |
| Cost at 50 screenshots/day | ~$0.10–0.50/day |

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `CUDA out of memory` | Switch to `TORCH_DTYPE=float16` or use a 4-bit quantized model |
| `ModuleNotFoundError: qwen_vl_utils` | Check requirements.txt is installed correctly |
| 30-second timeout on first request | Normal cold start — model loading from Network Volume |
| `Both detection methods failed` | Pre-processing fallback — full image sent to model, result may be lower accuracy |
| All fields null in output | Increase `MAX_NEW_TOKENS` or check the prompt |

---

## 11. Known Limitations

- Cold start: 15–45 seconds when no warm worker exists
- The model may miss very small text (player names < 8px tall)
- Heavily compressed JPEG screenshots (quality < 60%) may reduce accuracy
- The model has not been fine-tuned specifically for this task — prompt engineering is the main tuning lever
- Multi-pass inference adds latency; only 2 passes are implemented
