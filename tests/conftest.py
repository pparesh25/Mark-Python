"""Make top-level project modules importable during local pytest runs."""

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402  (needs the path above)


@pytest.fixture(autouse=True)
def _never_the_live_store(tmp_path, monkeypatch):
    """Point the price store at a temporary file, so no test can open the user's live DuckDB."""
    monkeypatch.setattr(config, "DUCKDB_FILE", tmp_path / "guard" / "NSE_BSE_EOD.duckdb")
