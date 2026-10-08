#!/usr/bin/env bash
# One-click launcher for macOS / Linux:  ./run.sh   (first time: chmod +x run.sh)
#   1) creates .venv and installs requirements (skipped when requirements.txt is unchanged)
#   2) seeds the demo database
#   3) starts the Streamlit dashboard
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "ERROR: $PY not found. Install Python 3.10+ first."; exit 1; }

# --- 1. virtual environment + dependencies ---------------------------------
if [ ! -d .venv ]; then
  echo ">> Creating virtual environment (.venv)"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Hash of requirements.txt lets repeat launches skip the (slow) pip step.
if command -v sha256sum >/dev/null 2>&1; then REQ_HASH=$(sha256sum requirements.txt | cut -d' ' -f1)
else REQ_HASH=$(shasum -a 256 requirements.txt | cut -d' ' -f1); fi
if [ ! -f .venv/.req_hash ] || [ "$(cat .venv/.req_hash)" != "$REQ_HASH" ]; then
  echo ">> Installing dependencies"
  python -m pip install --upgrade pip -q
  pip install -r requirements.txt -q
  echo "$REQ_HASH" > .venv/.req_hash
else
  echo ">> Dependencies up to date"
fi

# --- config ------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  echo ">> Created .env from .env.example - edit it to add GEMINI_API_KEY and ADMIN_PASSWORD."
fi
if ! grep -Eq '^GEMINI_API_KEY=.{10,}' .env || grep -q 'your-google-ai-studio-key' .env; then
  echo "!! No GEMINI_API_KEY set: Layer 1 demo works, live AI (Layer 2) will be disabled."
fi

# --- 2. seed demo data -------------------------------------------------------
echo ">> Seeding demo database"
python seed_data.py

# --- 3. launch ---------------------------------------------------------------
echo ">> Starting HLRN on http://localhost:8501"
exec streamlit run app.py "$@"
