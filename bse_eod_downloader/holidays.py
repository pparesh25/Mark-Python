"""BSE holiday and special-session data helpers."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def load_holidays_from_meta(metadata_dir: Path) -> dict[str, str]:
    """Load holiday list from the shared NSE metadata/meta.json file."""
    meta_file = metadata_dir / "meta.json"
    if meta_file.exists():
        try:
            with open(meta_file, "r") as f:
                meta = json.load(f)
                return meta.get("holidays", {})
        except Exception:
            pass
    return {}


def load_special_sessions_from_meta(metadata_dir: Path) -> list[str]:
    """Load special sessions list from the shared NSE metadata/meta.json file."""
    meta_file = metadata_dir / "meta.json"
    if meta_file.exists():
        try:
            with open(meta_file, "r") as f:
                meta = json.load(f)
                return meta.get("special_sessions", [])
        except Exception:
            pass
    return []


def is_special_session(current: datetime, special_sessions: list[str]) -> bool:
    """Return whether current is listed as a special session."""
    return current in tuple(datetime.fromisoformat(value) for value in special_sessions)
