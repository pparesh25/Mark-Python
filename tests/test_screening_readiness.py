from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from eod_store import create_compact_schema
from screening_readiness import (
    LocalDataError,
    inspect_eod_readiness,
    latest_eligible_eod_date,
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
        conn.execute("INSERT INTO securities VALUES ('NSE', 'NSETEST', 'INE000A', 'EQ'), ('BSE', 'BSETEST', 'INE000B', 'A')")
        conn.execute(
            "INSERT INTO eod_prices VALUES (?, 'NSETEST', 'NSE', 1, 1, 1, 1, 1), (?, 'BSETEST', 'BSE', 1, 1, 1, 1, 1)",
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
