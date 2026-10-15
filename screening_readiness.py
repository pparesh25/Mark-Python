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
# A session whose EOD file cannot be downloaded is retried on later runs.  It
# is presumed to be an unlisted market holiday only after this many failed
# runs, and only once it is this many calendar days old.
MISSING_SESSION_ATTEMPTS = 3
MISSING_SESSION_MIN_AGE_DAYS = 3
MISSING_SESSION_KEY = "missingSessionAttempts"
SKIPPED_SESSION_KEY = "skippedSessions"
# Sessions outside the normal Monday-Friday calendar, or on a day NSE's list
# names a holiday, that both exchanges traded.  meta.json learns special
# sessions only from NSE circulars published after the downloader first ran,
# and keeps the holiday list of the current year only, so a rebuild of two
# years never knew 2025-02-01 (Union Budget, a Saturday) and skipped it.
# Muhurat sessions on a weekday are listed too, for years whose holiday list
# (which marks them "Laxmi Pujan") is no longer cached.
KNOWN_SPECIAL_SESSIONS = frozenset({
    date(2024, 11, 1),      # Muhurat trading (Diwali), Friday
    date(2025, 2, 1),       # Union Budget, Saturday
    date(2025, 10, 21),     # Muhurat trading (Diwali), Tuesday
    date(2026, 2, 1),       # Union Budget, Sunday
})


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
    """Read the cached NSE calendar without contacting the exchange.

    The special sessions are meta.json's and KNOWN_SPECIAL_SESSIONS.
    """
    meta_file = metadata_dir / "meta.json"
    if not meta_file.exists():
        return {}, set(KNOWN_SPECIAL_SESSIONS)
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, set(KNOWN_SPECIAL_SESSIONS)

    holidays = meta.get("holidays", {})
    if not isinstance(holidays, dict):
        holidays = {}
    special_sessions: set[date] = set(KNOWN_SPECIAL_SESSIONS)
    for value in meta.get("special_sessions", []):
        try:
            special_sessions.add(datetime.fromisoformat(value).date())
        except (TypeError, ValueError):
            continue
    return holidays, special_sessions


def is_muhurat(day: date, holidays: Mapping[str, str]) -> bool:
    """Muhurat trading is stored by NSE as a holiday ("Laxmi Pujan"), but is still a session."""
    return "laxmi pujan" in str(holidays.get(day.strftime("%d-%b-%Y"), "")).lower()


def is_special_session_day(day: date, holidays: Mapping[str, str], special_sessions) -> bool:
    """A session on a weekend or listed holiday: meta's special sessions, the known ones, or Muhurat."""
    return day in special_sessions or day in KNOWN_SPECIAL_SESSIONS or is_muhurat(day, holidays)


def _is_trading_day(day: date, holidays: Mapping[str, str], special_sessions: set[date]) -> bool:
    if is_special_session_day(day, holidays, special_sessions):
        return True
    holiday = holidays.get(day.strftime("%d-%b-%Y"), "")
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


def presume_session_closed(meta: dict, session: date, today: date) -> bool:
    """Record one failed EOD download and decide whether to skip the session.

    Callers must keep their sync cursor on the failed session when this
    returns False, so the next run retries it instead of leaving a silent gap.
    It returns True only for a session that failed on several runs and is
    several days old, which in practice means an unlisted market holiday.
    """
    attempts = meta.setdefault(MISSING_SESSION_KEY, {})
    key = session.isoformat()
    attempts[key] = int(attempts.get(key, 0)) + 1
    if (
        attempts[key] < MISSING_SESSION_ATTEMPTS
        or (today - session).days < MISSING_SESSION_MIN_AGE_DAYS
    ):
        return False
    attempts.pop(key)
    skipped = meta.setdefault(SKIPPED_SESSION_KEY, [])
    if key not in skipped:
        skipped.append(key)
    return True


def clear_session_attempts(meta: dict, session: date) -> None:
    """Forget earlier download failures once a session has been stored."""
    attempts = meta.get(MISSING_SESSION_KEY)
    if isinstance(attempts, dict):
        attempts.pop(session.isoformat(), None)


def find_missing_sessions(
    database: Path,
    metadata_dir: Path,
    lookback_days: int | None = 120,
) -> dict[str, list[date]]:
    """List trading sessions absent from each exchange's price data.

    `lookback_days` counts back from the store's last day; None checks the
    whole stored range.  A session is expected when either exchange stored it,
    when it is a known special session (meta.json's or KNOWN_SPECIAL_SESSIONS),
    or when the cached NSE calendar says it was a trading day (only for years
    the cached holiday list covers).  The check never touches the network.
    """
    holidays, special_sessions = _read_calendar(metadata_dir)
    calendar_years = set()
    for key in holidays:
        try:
            calendar_years.add(datetime.strptime(key, "%d-%b-%Y").year)
        except ValueError:
            continue

    try:
        conn = duckdb.connect(str(database), read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT Exchange, Date
                FROM eod_data
                WHERE Exchange IN ('NSE', 'BSE')
                  AND (CAST(? AS INTEGER) IS NULL
                       OR Date >= (SELECT MAX(Date) FROM eod_data) - CAST(? AS INTEGER))
                """,
                [lookback_days, lookback_days],
            ).fetchall()
        finally:
            conn.close()
    except duckdb.Error as error:
        raise LocalDataError(f"DuckDB is unreadable or has an invalid schema: {error}") from error

    present: dict[str, set[date]] = {exchange: set() for exchange in REQUIRED_EXCHANGES}
    for exchange, day in rows:
        present[exchange].add(day)
    if not all(present.values()):
        return {}

    expected = set().union(*present.values())
    first_day = min(min(days) for days in present.values())
    last_day = max(max(days) for days in present.values())
    day = first_day
    while day <= last_day:
        if (is_special_session_day(day, holidays, special_sessions)
                or (day.year in calendar_years and _is_trading_day(day, holidays, special_sessions))):
            expected.add(day)
        day += timedelta(days=1)

    missing = {}
    for exchange, days in present.items():
        gaps = sorted(
            day for day in expected
            if min(days) <= day <= max(days) and day not in days
        )
        if gaps:
            missing[exchange] = gaps
    return missing


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
