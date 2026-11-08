#!/bin/bash
# Run the server test suite the way CI does: a small test venv (no MLX) with the
# Apple-Silicon-only modules stubbed, so it runs on any OS. Needs `uv` and `ffmpeg`.
# usage: scripts/test-server.sh [pytest args...]
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${WISPRALT_TEST_VENV:-$REPO/.build/venv-test}"
STUBS="$VENV/ci-stubs"

if [ ! -x "$VENV/bin/python" ]; then
  uv venv -q -p 3.11 "$VENV"
  VIRTUAL_ENV="$VENV" uv pip install -q \
    "pytest>=8.0" "pytest-asyncio>=0.23" "httpx>=0.27" "fastapi==0.115.0" "starlette<1.0" \
    "soundfile==0.12.1" "numpy>=2.0,<2.3" "librosa>=0.11.0,<0.12.0" "pydantic==2.9" \
    "pydantic-settings==2.5" "psutil>=6.0" "python-multipart==0.0.9" "jinja2>=3.1"
fi
if [ ! -f "$STUBS/mlx/core/__init__.py" ]; then
  mkdir -p "$STUBS/parakeet_mlx" "$STUBS/mlx/core"
  printf 'def from_pretrained(*a, **k): return object()\nclass DecodingConfig:\n    def __init__(self, *a, **k): pass\n' > "$STUBS/parakeet_mlx/__init__.py"
  printf 'def get_logmel(*a, **k): return None\n' > "$STUBS/parakeet_mlx/audio.py"
  : > "$STUBS/mlx/__init__.py"
  printf 'class _DT: pass\nbfloat16 = _DT(); float32 = _DT()\nclass _Arr: pass\ndef zeros(shape, dtype=None): return _Arr()\ndef array(x, dtype=None): return _Arr()\ndef eval(x): return None\ndef clear_cache(): return None\n' > "$STUBS/mlx/core/__init__.py"
fi

DATA="$(mktemp -d "${TMPDIR:-/tmp}/wispralt-test.XXXXXX")"
trap 'rm -rf "$DATA"' EXIT
export PYTHONPATH="$REPO/server/src:$STUBS" SERVER_URL="https://ci.example.invalid" DATA_DIR="$DATA"
cd "$REPO/server"
"$VENV/bin/python" -m pytest tests/ -q -p no:cacheprovider "$@"
