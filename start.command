#!/usr/bin/env bash
# Double-click this file (or run it in a terminal) to start the backend and the frontend.
set -euo pipefail
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "Python was not found on PATH. Install Python 3.11 or newer, then run this file again."
  read -r -p "Press Enter to close." _
  exit 1
fi

exec "$PYTHON" dev.py "$@"
