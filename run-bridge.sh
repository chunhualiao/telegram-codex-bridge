#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

if [[ -x /opt/homebrew/bin/python3 ]]; then
  PYTHON_BIN=/opt/homebrew/bin/python3
elif [[ -x /usr/local/bin/python3 ]]; then
  PYTHON_BIN=/usr/local/bin/python3
else
  PYTHON_BIN="$(command -v python3)"
fi

exec "$PYTHON_BIN" bridge.py
