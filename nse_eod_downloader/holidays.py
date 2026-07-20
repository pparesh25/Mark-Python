"""NSE holiday and special-session data helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def get_muhurat_holiday_info(holidays: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Find the Laxmi Pujan entry in an NSE holiday response."""

    for entries in holidays.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and "Laxmi Pujan" in entry.get("description", ""):
                return entry
    return {}


def get_holiday_list(nse: Any) -> dict[str, str]:
    """Fetch and normalize the capital-market holiday list from NSE."""

    data = nse.holidays(type=nse.HOLIDAY_TRADING)
    muhurat = get_muhurat_holiday_info(data)
    if muhurat:
        data.setdefault("CM", []).append(muhurat)
    return {
        entry["tradingDate"]: entry["description"]
        for entry in data.get("CM", [])
        if entry and entry.get("tradingDate") and entry.get("description")
    }


def is_special_session(current: datetime, special_sessions: list[str]) -> bool:
    """Return whether ``current`` is listed as an NSE special session."""

    return current in tuple(datetime.fromisoformat(value) for value in special_sessions)

