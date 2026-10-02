from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from eod_store import create_compact_schema
from screening_readiness import (
    LocalDataError,
    clear_session_attempts,
    find_missing_sessions,
    inspect_eod_readiness,
    latest_eligible_eod_date,
    presume_session_closed,
    rebase_sync_cursor,
)


def _metadata(tmp_path: Path, holidays=None) -> Path:
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "meta.json").write_text(
        '{"holidays": ' + str(holidays or {}).replace("'", '"') + ', "special_sessions": []}',
        encoding="utf-8",
    )
    return metadata


def _database(tmp_path: Path, nse_date: str, bse_date: str) -> Path:
    database = tmp_path / "prices.duckdb"
    conn = duckdb.connect(str(database))
    try:
        create_compact_schema(conn)
        conn.execute("INSERT INTO securities (Exchange, Symbol, ISIN, Series) VALUES ('NSE', 'NSETEST', 'INE000A', 'EQ'), ('BSE', 'BSETEST', 'INE000B', 'A')")
        conn.execute(
            "INSERT INTO raw_prices (Date, Symbol, Exchange, Open, High, Low, Close, Volume) VALUES (?, 'NSETEST', 'NSE', 1, 1, 1, 1, 1), (?, 'BSETEST', 'BSE', 1, 1, 1, 1, 1)",
            [nse_date, bse_date],
        )
    finally:
        conn.close()
    return database


def test_latest_eligible_date_waits_until_six_pm_and_skips_weekend(tmp_path):
    metadata = _metadata(tmp_path)
    assert latest_eligible_eod_date(datetime(2026, 7, 27, 16, 0), metadata).isoformat() == "2026-07-24"
    assert latest_eligible_eod_date(datetime(2026, 7, 27, 18, 0), metadata).isoformat() == "2026-07-27"
    assert latest_eligible_eod_date(datetime(2026, 7, 26, 12, 0), metadata).isoformat() == "2026-07-24"


def test_latest_eligible_date_uses_local_holiday_calendar(tmp_path):
    metadata = _metadata(tmp_path, {"28-Jul-2026": "Market Holiday"})
    # Tuesday is a holiday, so the pre-6 PM target is Monday rather than a
    # network call for Tuesday's unavailable EOD.
    assert latest_eligible_eod_date(datetime(2026, 7, 28, 16, 0), metadata).isoformat() == "2026-07-27"


def test_readiness_catches_up_prior_sessions_before_six_pm(tmp_path):
    metadata = _metadata(tmp_path)
    database = _database(tmp_path, "2026-07-23", "2026-07-23")
    status = inspect_eod_readiness(database, metadata, datetime(2026, 7, 27, 16, 0))
    assert status.target_date.isoformat() == "2026-07-24"
    assert status.needs_update is True


def test_readiness_skips_network_when_previous_session_is_present(tmp_path):
    metadata = _metadata(tmp_path)
    database = _database(tmp_path, "2026-07-24", "2026-07-24")
    status = inspect_eod_readiness(database, metadata, datetime(2026, 7, 27, 16, 0))
    assert status.needs_update is False


def test_readiness_rejects_missing_or_invalid_local_database(tmp_path):
    with pytest.raises(LocalDataError):
        inspect_eod_readiness(tmp_path / "missing.duckdb", _metadata(tmp_path), datetime(2026, 7, 27, 16, 0))


def test_rebase_sync_cursor_uses_actual_database_date(tmp_path):
    metadata = _metadata(tmp_path)
    meta_file = metadata / "meta.json"
    meta_file.write_text('{"lastUpdate": "2026-07-26T00:00:00+05:30"}', encoding="utf-8")

    assert rebase_sync_cursor(meta_file, datetime(2026, 7, 23).date()) is True
    assert "2026-07-23T00:00:00+05:30" in meta_file.read_text(encoding="utf-8")
    assert rebase_sync_cursor(meta_file, datetime(2026, 7, 23).date()) is False


def test_failed_session_is_retried_before_it_is_presumed_closed():
    meta = {}
    session = datetime(2026, 7, 1).date()
    today = datetime(2026, 7, 6).date()

    assert presume_session_closed(meta, session, today) is False
    assert presume_session_closed(meta, session, today) is False
    assert meta["missingSessionAttempts"] == {"2026-07-01": 2}
    assert presume_session_closed(meta, session, today) is True
    assert meta["missingSessionAttempts"] == {}
    assert meta["skippedSessions"] == ["2026-07-01"]


def test_recent_failed_session_is_never_presumed_closed():
    meta = {}
    session = datetime(2026, 7, 1).date()
    for _ in range(5):
        assert presume_session_closed(meta, session, session) is False
    clear_session_attempts(meta, session)
    assert meta["missingSessionAttempts"] == {}


def test_missing_sessions_are_reported_per_exchange(tmp_path):
    metadata = _metadata(tmp_path, {"29-Jul-2026": "Market Holiday"})
    database = tmp_path / "prices.duckdb"
    conn = duckdb.connect(str(database))
    try:
        create_compact_schema(conn)
        conn.execute("INSERT INTO securities (Exchange, Symbol, ISIN, Series) VALUES ('NSE', 'NSETEST', 'INE000A', 'EQ'), ('BSE', 'BSETEST', 'INE000B', 'A')")
        # Mon 27 .. Fri 31 July; Wednesday 29 is a holiday.  NSE lost the
        # 28th, BSE lost the 30th, and both lost nothing else.
        for day in ("2026-07-27", "2026-07-30", "2026-07-31"):
            conn.execute("INSERT INTO raw_prices (Date, Symbol, Exchange, Open, High, Low, Close, Volume) VALUES (?, 'NSETEST', 'NSE', 1, 1, 1, 1, 1)", [day])
        for day in ("2026-07-27", "2026-07-28", "2026-07-31"):
            conn.execute("INSERT INTO raw_prices (Date, Symbol, Exchange, Open, High, Low, Close, Volume) VALUES (?, 'BSETEST', 'BSE', 1, 1, 1, 1, 1)", [day])
    finally:
        conn.close()

    missing = find_missing_sessions(database, metadata)

    assert {exchange: [day.isoformat() for day in days] for exchange, days in missing.items()} == {
        "NSE": ["2026-07-28"],
        "BSE": ["2026-07-30"],
    }
