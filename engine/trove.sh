#!/bin/bash
# Run the Spool engine headless (no Docker): venv + deps + JSON API on :8899.
set -e
cd "$(dirname "$0")"

# Pick a Python ≥ 3.11 (trove needs 3.11+; yt-dlp master drops 3.9).
PYTHON_BIN=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver=$("$cand" -c 'import sys; print(sys.version_info[0]*100 + sys.version_info[1])' 2>/dev/null || echo 0)
    if [ "$ver" -ge 311 ]; then PYTHON_BIN="$cand"; break; fi
  fi
done

# Check prerequisites
missing=""
[ -z "$PYTHON_BIN" ] && missing="$missing python@3.12"
command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
if [ -n "$missing" ]; then
  echo "Missing required tools:$missing"
  if command -v brew >/dev/null 2>&1; then echo "Install with:  brew install$missing"
  elif command -v apt >/dev/null 2>&1; then echo "Install with:  sudo apt install$missing"
  else echo "Please install:$missing"; fi
  exit 1
fi

# Python venv (rebuilt if it points at an older interpreter)
if [ -d "venv" ]; then
  cur=$(./venv/bin/python -c 'import sys; print(sys.version_info[0]*100 + sys.version_info[1])' 2>/dev/null || echo 0)
  if [ "$cur" -lt 311 ]; then
    echo "Existing venv is on Python <3.11; rebuilding with $PYTHON_BIN..."
    rm -rf venv
  fi
fi
if [ ! -d "venv" ]; then
  echo "Setting up virtual environment with $PYTHON_BIN..."
  "$PYTHON_BIN" -m venv venv
fi
# shellcheck source=/dev/null
source venv/bin/activate
pip install -q -U pip wheel >/dev/null
# Full engine deps: Flask, whisper.cpp (pywhispercpp), the diarization stack, MCP,
# and yt-dlp from master. Idempotent — heavy only on first run.
pip install -q -r requirements.txt >/dev/null

PORT="${PORT:-8899}"
HOST="${HOST:-127.0.0.1}"
export PORT HOST
echo ""
echo "  Spool engine (headless) → http://$HOST:$PORT/api/v1/health"
echo ""
exec python3 app.py
