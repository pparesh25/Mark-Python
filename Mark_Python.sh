#!/bin/bash
# Run the Mark Screener pipeline (run_screener.py) with this project's Python.
#
# The interpreter is chosen in this order:
#   1. $MARK_PYTHON, when set (it must be executable);
#   2. <project>/.venv/bin/python (made by setup_macos.sh / setup_linux.sh);
#   3. a conda environment named mark_screener under $HOME/miniforge3,
#      $HOME/miniconda3, $HOME/anaconda3 or /opt/homebrew/Caskroom/miniforge/base.
# launchd starts this with a minimal PATH, so `conda` is never looked up on PATH.

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONDA_ENV_NAME="mark_screener"
CONDA_ROOTS=(
    "$HOME/miniforge3"
    "$HOME/miniconda3"
    "$HOME/anaconda3"
    "/opt/homebrew/Caskroom/miniforge/base"
)

PYTHON=""
SOURCE=""
if [ -n "${MARK_PYTHON:-}" ]; then
    if [ ! -x "$MARK_PYTHON" ]; then
        echo "Mark_Python.sh: MARK_PYTHON is set but is not an executable file: $MARK_PYTHON" >&2
        exit 2
    fi
    PYTHON="$MARK_PYTHON"
    SOURCE="MARK_PYTHON"
elif [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
    SOURCE="project .venv"
else
    for root in "${CONDA_ROOTS[@]}"; do
        candidate="$root/envs/$CONDA_ENV_NAME/bin/python"
        if [ -x "$candidate" ]; then
            PYTHON="$candidate"
            SOURCE="conda env $CONDA_ENV_NAME under $root"
            break
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    {
        echo "Mark_Python.sh: no Python environment found for the Mark Screener. Looked for:"
        echo "  \$MARK_PYTHON (not set)"
        echo "  $PROJECT_DIR/.venv/bin/python"
        for root in "${CONDA_ROOTS[@]}"; do
            echo "  $root/envs/$CONDA_ENV_NAME/bin/python"
        done
        echo "Run ./setup_macos.sh, create the conda env '$CONDA_ENV_NAME', or set MARK_PYTHON."
    } >&2
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Mark_Python.sh: using $PYTHON ($SOURCE)"
cd "$PROJECT_DIR" || exit 1
exec "$PYTHON" run_screener.py "$@"
