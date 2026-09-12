#!/bin/bash
set -e
cd "$(dirname "$0")"
SYSTEM_PYTHON="$(command -v python3 || command -v python || true)"
if [ -z "$SYSTEM_PYTHON" ]; then
  echo "Python 3 is required. Install Python 3 and run this file again."
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating a private Python environment for Exam Practice..."
  "$SYSTEM_PYTHON" -m venv .venv
fi

PYTHON_BIN=".venv/bin/python"
if ! "$PYTHON_BIN" -c "import flask, flaskwebgui, fitz, PIL" >/dev/null 2>&1; then
  echo "Installing Flask GUI dependencies into the private environment..."
  "$PYTHON_BIN" -m pip install -r requirements.txt
fi

exec "$PYTHON_BIN" launcher.py
