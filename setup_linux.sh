#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "Unsupported architecture: $(uname -m). The prebuilt Rust engine requires Linux x86_64." >&2
  exit 1
fi

cd "$PROJECT_DIR"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'
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
