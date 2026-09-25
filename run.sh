#!/bin/bash
# Entry point for the launchd schedules (stocks, crypto, weekly). Keeps the Mac awake while it runs.
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then PY=.venv/bin/python
elif [ -x .conda-env/bin/python ]; then PY=.conda-env/bin/python
else echo "No Python environment found; run ./setup.sh first" >&2; exit 1; fi
mkdir -p logs
[ $# -eq 0 ] && set -- run   # older schedules called this with no arguments
echo "=== $(date) · $* ==="
exec /usr/bin/caffeinate -i "$PY" -m agent "$@"
