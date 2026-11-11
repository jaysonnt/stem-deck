#!/usr/bin/env bash
# Install the stem splitter (Demucs) into pianokb/.venv.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
VENV="$(dirname "$HERE")/.venv"

if ! command -v uv >/dev/null; then
  echo "uv is required: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

uv venv --python 3.14 "$VENV"
# CPU torch keeps the install small; htdemucs_6s separates a song in a couple
# of minutes on a desktop CPU.
uv pip install --python "$VENV/bin/python" \
  --index-strategy unsafe-best-match \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  demucs numpy yt-dlp

"$VENV/bin/python" -c "import demucs, torch; print('splitter ready, torch', torch.__version__)"
