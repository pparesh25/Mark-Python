#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Python environment not found. Run ./setup_linux.sh first." >&2
  exit 1
fi

cd "$PROJECT_DIR"
exec "$VENV_PYTHON" run_screener.py "$@"
