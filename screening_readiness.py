"""Decide whether a screener run needs an EOD download.

The decision is deliberately local: it reads the stored holiday calendar and
the actual dates in DuckDB, so an offline weekend run never constructs an NSE
or BSE network client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Mapping

import duckdb


EOD_READY_TIME = time(18, 0)
REQUIRED_EXCHANGES = ("NSE", "BSE")


class LocalDataError(RuntimeError):
    """The local price database is not safe to use for screening."""


@dataclass(frozen=True)
class EodReadiness:
    target_date: date
    as_of_dates: Mapping[str, date]

    @property
    def needs_update(self) -> bool:
        return any(self.as_of_dates[exchange] < self.target_date for exchange in REQUIRED_EXCHANGES)


def _read_calendar(metadata_dir: Path) -> tuple[dict[str, str], set[date]]:
    """Read the cached NSE calendar without contacting the exchange."""
    meta_file = metadata_dir / "meta.json"
    if not meta_file.exists():
        return {}, set()
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, set()

    holidays = meta.get("holidays", {})
    if not isinstance(holidays, dict):
        holidays = {}
    special_sessions: set[date] = set()
    for value in meta.get("special_sessions", []):
        try:
            special_sessions.add(datetime.fromisoformat(value).date())
        except (TypeError, ValueError):
            continue
    return holidays, special_sessions


def _is_trading_day(day: date, holidays: Mapping[str, str], special_sessions: set[date]) -> bool:
    if day in special_sessions:
        return True
    holiday = holidays.get(day.strftime("%d-%b-%Y"), "")
    # Muhurat trading is stored by NSE as a holiday, but is still a session.
    if "laxmi pujan" in str(holiday).lower():
        return True
    return day.weekday() < 5 and not holiday


def latest_eligible_eod_date(now: datetime, metadata_dir: Path) -> date:
    """Return the latest session whose complete EOD may be downloaded now.

    On a trading day before 6 PM, today's close/index data is deliberately not
    eligible.  Any missing earlier session remains eligible for catch-up.
    """
    holidays, special_sessions = _read_calendar(metadata_dir)
    candidate = now.date()
    if _is_trading_day(candidate, holidays, special_sessions) and now.time() < EOD_READY_TIME:
        candidate -= timedelta(days=1)
    while not _is_trading_day(candidate, holidays, special_sessions):
        candidate -= timedelta(days=1)
    return candidate


def inspect_eod_readiness(database: Path, metadata_dir: Path, now: datetime) -> EodReadiness:
    """Validate local price data and compare it with the eligible EOD date."""
    if not database.exists():
        raise LocalDataError(f"DuckDB file is missing: {database}")

    try:
        conn = duckdb.connect(str(database), read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT Exchange, MAX(Date)
                FROM eod_data
                WHERE Exchange IN ('NSE', 'BSE')
                  AND NULLIF(TRIM(COALESCE(ISIN, '')), '') IS NOT NULL
                GROUP BY Exchange
                """
            ).fetchall()
        finally:
            conn.close()
    except duckdb.Error as error:
        raise LocalDataError(f"DuckDB is unreadable or has an invalid schema: {error}") from error

    as_of_dates = {exchange: value for exchange, value in rows if value is not None}
    missing = [exchange for exchange in REQUIRED_EXCHANGES if exchange not in as_of_dates]
    if missing:
        raise LocalDataError(f"DuckDB has no usable equity data for: {', '.join(missing)}")

    return EodReadiness(
        target_date=latest_eligible_eod_date(now, metadata_dir),
        as_of_dates=as_of_dates,
    )


def rebase_sync_cursor(metadata_file: Path, actual_as_of: date) -> bool:
    """Move an over-advanced downloader cursor back to the real DB date.

    Weekend/holiday processing can advance ``lastUpdate`` without adding an
    equity row.  If a prior download failed, the next sync must restart from
    the actual local date or it would silently skip the missing session.
    """
    if not metadata_file.exists():
        return False
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        cursor = datetime.fromisoformat(metadata["lastUpdate"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if cursor.date() <= actual_as_of:
        return False

    metadata["lastUpdate"] = datetime.combine(actual_as_of, time.min, cursor.tzinfo).isoformat()
    metadata_file.write_text(json.dumps(metadata, indent=3), encoding="utf-8")
    return True
