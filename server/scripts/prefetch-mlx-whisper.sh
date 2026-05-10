#!/bin/bash
# Prefetch the MLX-Whisper weights into the HuggingFace cache so the first
# transcription request doesn't pay the download cost. Idempotent — safe to
# re-run; resume_download=True picks up partial files.
#
# Pin the revision by exporting WHISPER_REVISION before running. Default is
# the model's main branch — fine for dev, pin for prod once T0.5 spike confirms
# the right model.
#
# Verifies the resulting model.safetensors is > 800 MB to catch corrupt/partial
# downloads; the turbo model is ~1.6 GB, fp16 is ~3.1 GB.

set -euo pipefail

WHISPER_REPO="${WHISPER_REPO:-mlx-community/whisper-large-v3-turbo}"
WHISPER_REVISION="${WHISPER_REVISION:-main}"

echo "Prefetching ${WHISPER_REPO}@${WHISPER_REVISION}..."

python3 - <<PYEOF
import os, sys
from huggingface_hub import snapshot_download

repo = os.environ.get("WHISPER_REPO", "mlx-community/whisper-large-v3-turbo")
revision = os.environ.get("WHISPER_REVISION", "main")

path = snapshot_download(
    repo_id=repo,
    revision=revision,
    resume_download=True,
)
print(f"Downloaded to: {path}")

mp = os.path.join(path, "model.safetensors")
if not os.path.exists(mp):
    # turbo and non-turbo variants both ship a model.safetensors at the root.
    # If absent, the snapshot is incomplete.
    print(f"ERROR: model.safetensors not found at {mp}", file=sys.stderr)
    sys.exit(2)

sz = os.path.getsize(mp)
print(f"Model size: {sz / (1024 * 1024):.1f} MB")
if sz < 800 * 1024 * 1024:
    print(f"ERROR: model.safetensors too small ({sz} bytes); likely corrupt", file=sys.stderr)
    sys.exit(3)

print("OK")
PYEOF

echo "Prefetch complete."
