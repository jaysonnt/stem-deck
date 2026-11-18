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
# CUDA torch when an NVIDIA GPU is present: a 3-minute song splits in ~4 s on
# an RTX 5080 against ~23 s on a 16-core CPU. Demucs runs as a short-lived
# process per song, so the GPU memory is released the moment a split ends.
# PIANOKB_TORCH=cpu forces the small CPU build; PIANOKB_TORCH=cu132 (default
# with a GPU) picks the CUDA wheel index.
TORCH="${PIANOKB_TORCH:-}"
if [ -z "$TORCH" ]; then
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then TORCH=cu132; else TORCH=cpu; fi
fi
uv pip install --python "$VENV/bin/python" \
  --index-strategy unsafe-best-match \
  --extra-index-url "https://download.pytorch.org/whl/$TORCH" \
  demucs numpy yt-dlp

"$VENV/bin/python" -c "import demucs, torch; print('splitter ready, torch', torch.__version__, '· cuda' if torch.cuda.is_available() else '· cpu')"
