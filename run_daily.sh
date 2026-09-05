#!/bin/bash
# Wrapper the scheduler calls each morning. Edit the path if you move the project.
set -euo pipefail
cd "$(dirname "$0")"

# Use the project venv if present, else system python3.
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="$(command -v python3)"
fi

mkdir -p data
"$PY" -m music_xp.main >> data/run.log 2>&1
