#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# numpy 2.2.6 (pinned in requirements.txt) has wheels for Python 3.10-3.13 only.
SUPPORTED_PYTHONS="3.10 3.11 3.12 3.13"
python_version_ok() {
  "$1" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)' 2>/dev/null
}
if [[ -n "${PYTHON_BIN:-}" ]]; then
  if ! python_version_ok "$PYTHON_BIN"; then
    echo "PYTHON_BIN=$PYTHON_BIN is not Python 3.10-3.13 ($("$PYTHON_BIN" --version 2>&1 || echo 'not runnable'))." >&2
    echo "The pinned numpy 2.2.6 has no wheels for other versions; point PYTHON_BIN at Python 3.11 or 3.12." >&2
    exit 1
  fi
else
  for candidate in python3.11 python3.12 python3.13 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_version_ok "$candidate"; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
  if [[ -z "${PYTHON_BIN:-}" ]]; then
    echo "No supported Python found (need one of: $SUPPORTED_PYTHONS; 3.11 or 3.12 recommended)." >&2
    echo "python3 here is: $(python3 --version 2>&1 || echo 'missing'). Install Python 3.11 or set PYTHON_BIN." >&2
    exit 1
  fi
fi
echo "Using $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "Unsupported architecture: $(uname -m). The prebuilt Rust engine requires Linux x86_64." >&2
  exit 1
fi

cd "$PROJECT_DIR"
"$PYTHON_BIN" -m venv .venv

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r requirements.txt

if [[ "${1:-}" != "--skip-rust" ]]; then
  echo "Installing the precompiled Rust engine (private repository access may require GITHUB_TOKEN or GH_TOKEN)..."
  "$VENV_PYTHON" install_rust_binary.py
fi

"$VENV_PYTHON" -c 'import config; config.ensure_dirs(); print(f"Data root: {config.DATA_ROOT}"); print(f"OpenTrader root: {config.OPENTRADER_ROOT}")'

echo "Setup complete."
echo "Copy the production Database folder under: $HOME/Mark EMA and HTF/Database"
echo "Store Screener.in credentials at: $HOME/Mark EMA and HTF/Screener login/screener.txt"
echo "Then run: ./run_linux.sh"
