#!/bin/bash
# One-time setup: builds a Python 3.12 environment inside this folder and installs everything.
set -e
cd "$(dirname "$0")"

if [ -x .venv/bin/python ] || [ -x .conda-env/bin/python ]; then
  echo "Environment already exists; updating packages."
elif command -v uv >/dev/null 2>&1; then
  echo "Creating environment with uv..."
  uv venv --python 3.12 .venv
elif command -v python3.12 >/dev/null 2>&1; then
  echo "Creating environment with python3.12..."
  python3.12 -m venv .venv
elif command -v conda >/dev/null 2>&1; then
  echo "Creating environment with conda (inside this folder)..."
  conda create -y -p ./.conda-env python=3.12
else
  echo "Need Python 3.12. Install it with:  brew install python@3.12   (or install uv / Anaconda) and re-run."
  exit 1
fi

if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=.conda-env/bin/python; fi
if command -v uv >/dev/null 2>&1 && [ -d .venv ]; then
  uv pip install --python "$PY" -r requirements.txt
else
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt
fi

[ -f .env ] || cp .env.example .env
chmod 600 .env
chmod +x run.sh ta
mkdir -p data logs reports
echo
echo "Setup done. Next:  ./ta doctor"
