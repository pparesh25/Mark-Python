"""Regression tests for the EOD data-maintenance boundary.

These tests use temporary files and mocked NSE responses.  They do not call
the live NSE service or modify the project's real database.
"""

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

import config
from eod_store import create_compact_schema, upsert_daily_prices
from nse_eod_downloader import eod_downloader as eod
from nse_eod_downloader.sync import SyncDependencies, sync_database
from nse_eod_downloader.corporate_actions import (
    parse_bonus_factor,
    parse_split_factor,
)


TZ_IN = ZoneInfo("Asia/Kolkata")


def _logger():
    logger = Mock()
    return logger


def _dates(value="2024-05-02T00:00:00+05:30"):
    dates = eod.Dates(value, TZ_IN)
    dates.today = datetime.fromisoformat(value).replace(tzinfo=None)
    dates.dt = datetime.fromisoformat(value)
    dates.pandasDt = dates.dt.strftime("%Y-%m-%d")
    return dates


def test_corporate_action_parsers():
    assert parse_split_factor(
        "Face Value Split (Sub-Division) - From Rs 5/- Per Share To Rs 2/- Per Share"
    ) == 2.5
    assert parse_bonus_factor("Bonus 1:1") == 2
    assert parse_split_factor("not a split") is None
    assert parse_bonus_factor("Bonus 1:0") is None


def test_holiday_list_and_muhurat():
    nse = Mock(HOLIDAY_TRADING="trading")
    nse.holidays.return_value = {
        "CM": [{"tradingDate": "2024-11-01", "description": "Diwali Laxmi Pujan"}]
    }

    assert eod._get_holiday_list(nse, _logger()) == {
        "2024-11-01": "Diwali Laxmi Pujan"
    }
    assert eod._get_muhurat_holiday_info(
        {"CM": [{"description": "Diwali Laxmi Pujan"}]}
    )["description"] == "Diwali Laxmi Pujan"


def test_holiday_decisions_cover_weekend_weekday_and_special_session():
    dates = _dates("2024-01-06T00:00:00+05:30")  # Saturday
    dates.today = datetime(2024, 1, 7)
    assert eod._check_for_holidays(
        Mock(), dates, {"holidays": {}, "year": 2024}, _logger()
    ) is True

    dates.dt = datetime(2024, 1, 2, tzinfo=TZ_IN)
    assert eod._check_for_holidays(
        Mock(), dates, {"holidays": {}, "year": 2024}, _logger()
    ) is False

    dates.dt = datetime(2024, 3, 16, tzinfo=TZ_IN)
    assert eod._check_for_holidays(
        Mock(),
        dates,
        {"holidays": {}, "year": 2024, "special_sessions": ["2024-03-16T00:00:00"]},
        _logger(),
    ) is False


def test_actions_are_fetched_then_cached():
    nse = Mock()
    nse.actions.return_value = []
    dates = _dates("2024-01-01T00:00:00+05:30")
    meta = {}

    eod._validate_nse_actions_file(nse, dates, meta, _logger(), TZ_IN)
    assert nse.actions.call_count == 3
    assert all(key in meta for key in ("equityActions", "smeActions", "mfActions"))

    nse.reset_mock()
    eod._validate_nse_actions_file(nse, dates, meta, _logger(), TZ_IN)
    nse.actions.assert_not_called()


def test_symbol_rename_preserves_duckdb_history_without_text_copy(
    tmp_path, monkeypatch
):
    database = tmp_path / "prices.duckdb"
    conn = duckdb.connect(str(database))
    create_compact_schema(conn)
    upsert_daily_prices(conn, pd.DataFrame([{
        "Date": "2024-05-01", "Symbol": "OLDBOB", "Exchange": "NSE",
        "ISIN": "ISIN1", "Open": 100, "High": 100, "Low": 90,
        "Close": 95, "Volume": 900, "Series": "EQ",
    }]))
    conn.close()
    monkeypatch.setattr(config, "DUCKDB_FILE", database)
    bhav = tmp_path / "bhav.csv"
    delivery = tmp_path / "delivery.csv"
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    pd.DataFrame([{
        "ISIN": "ISIN1", "TckrSymb": "NEWBOB", "SctySrs": "EQ",
        "OpnPric": 110, "HghPric": 120, "LwPric": 100, "ClsPric": 115,
        "TtlTradgVol": 1000,
    }]).to_csv(bhav, index=False)
    pd.DataFrame([{
        "SYMBOL": "NEWBOB", " SERIES": " EQ", " NO_OF_TRADES": 100,
        " DELIV_QTY": 500,
    }]).to_csv(delivery, index=False)
    isin = pd.DataFrame({"SYMBOL": ["OLDBOB"]}, index=pd.Index(["ISIN1"], name="ISIN"))
    text_dir = tmp_path / "text"

    eod._update_nse_eod(
        bhav, delivery, text_dir, metadata, isin,
        _dates("2024-05-03T00:00:00+05:30"), _logger(),
    )

    conn = duckdb.connect(str(database), read_only=True)
    rows = conn.execute(
        "SELECT Symbol, Date, Close FROM eod_data ORDER BY Date"
    ).fetchall()
    conn.close()
    assert rows == [
        ("NEWBOB", datetime(2024, 5, 1).date(), 95.0),
        ("NEWBOB", datetime(2024, 5, 3).date(), 115.0),
    ]
    assert not text_dir.exists()
    assert pd.read_csv(metadata / "isin.csv", index_col="ISIN").loc[
        "ISIN1", "SYMBOL"
    ] == "NEWBOB"


def test_pending_delivery_updates_daily_file_and_cleans_download(tmp_path):
    daily = tmp_path / "20240502_BENNY_NSE_EOD.txt"
    pd.DataFrame([{
        "Symbol": "ABC", "Date": "2024-05-02",
        "Open": 100, "High": 100, "Low": 90,
        "Close": 95, "Volume": 900, "Series": "EQ", "TOTAL_TRADES": "",
        "QTY_PER_TRADE": "", "DLV_QTY": "",
    }]).to_csv(daily, index=False)
    report = tmp_path / "delivery.csv"
    pd.DataFrame([{
        "SYMBOL": "ABC", " SERIES": " EQ", " NO_OF_TRADES": 100,
        " DELIV_QTY": 500,
    }]).to_csv(report, index=False)
    nse = Mock()
    nse.deliveryBhavcopy.return_value = report

    assert eod._update_pending_delivery_data(
        nse, "2024-05-02T00:00:00+05:30", tmp_path, _logger(), TZ_IN
    ) is True
    result = pd.read_csv(daily)
    assert int(result.loc[0, "TOTAL_TRADES"]) == 100
    assert int(result.loc[0, "DLV_QTY"]) == 500
    assert not report.exists()


def test_index_update_only_adds_normalized_daily_bhavcopy_row(tmp_path):
    report = tmp_path / "indices.csv"
    pd.DataFrame([{
        "Index Name": "NIFTY 50/TEST",
        "Open Index Value": "100",
        "High Index Value": "110",
        "Low Index Value": "90",
        "Closing Index Value": "105",
        "Volume": "1000",
        "P/E": "20",
    }]).to_csv(report, index=False)
    dates = _dates("2024-05-02T00:00:00+05:30")
    rows = []

    eod._update_index_eod(report, tmp_path, dates, _logger(), rows)

    daily = tmp_path / "nifty 50-test.csv"
    assert not daily.exists()
    assert len(rows) == 1
    assert rows[0].startswith("NIFTY 50-TEST,2024-05-02,")


def test_sync_entrypoint_rejects_missing_metadata(tmp_path):
    logger = Mock()
    assert eod.sync_database(tmp_path, logger, tmp_path / "metadata") is False
    logger.error.assert_called_once()


def test_sme_split_adjusts_the_stored_upper_case_ticker(tmp_path, monkeypatch):
    database = tmp_path / "prices.duckdb"
    conn = duckdb.connect(str(database))
    create_compact_schema(conn)
    upsert_daily_prices(conn, pd.DataFrame([
        {
            "Date": day, "Symbol": "SMECO_SME", "Exchange": "NSE",
            "ISIN": "INE321X01011", "Open": close, "High": close,
            "Low": close, "Close": close, "Volume": 100, "Series": "SM",
        }
        for day, close in (("2024-05-02", 100.0), ("2024-05-03", 20.0))
    ]))
    conn.close()
    monkeypatch.setattr(config, "DUCKDB_FILE", database)
    dates = _dates("2024-05-03T00:00:00+05:30")
    meta = {"smeActions": [{
        "symbol": "SMECO",
        "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
        "exDate": "03-May-2024",
        "series": "SM",
    }]}

    eod._adjust_nse_stocks(meta, tmp_path, dates, _logger())

    conn = duckdb.connect(str(database), read_only=True)
    closes = conn.execute(
        "SELECT Close FROM eod_data WHERE Symbol = 'SMECO_SME' ORDER BY Date"
    ).fetchall()
    conn.close()
    assert closes == [(20.0,), (20.0,)]


class _WeekOfDates:
    """Dates stub that walks 2024-05-02 .. 2024-05-03 with a fixed today."""

    def __init__(self, last_update):
        self.dt = self.lastUpdate = datetime.fromisoformat(last_update)
        self.today = datetime(2024, 5, 10)
        self.pandasDt = self.dt.strftime("%Y-%m-%d")

    def nextDate(self):
        self.dt = self.dt + timedelta(1)
        if self.dt.date() > datetime(2024, 5, 3).date():
            return False
        self.pandasDt = self.dt.strftime("%Y-%m-%d")
        return True


def _failing_sync_deps(update_stock):
    nse = Mock()
    nse.equityBhavcopy.side_effect = ConnectionError("HTTP 503")
    return SyncDependencies(
        nse_available=True,
        nse_factory=lambda temp_dir: nse,
        dates_factory=_WeekOfDates,
        check_special_sessions=lambda *args: False,
        check_for_holidays=lambda *args: False,
        validate_actions=lambda *args: None,
        check_reports_status=lambda *args: {},
        update_pending_delivery=lambda *args: False,
        update_stock=update_stock,
        update_index=Mock(),
        adjust_stocks=Mock(),
        date_encoder=eod.DateEncoder,
        sync_lock=threading.Lock(),
        tz_in=TZ_IN,
    )


def test_failed_bhavcopy_keeps_cursor_until_session_is_presumed_closed(tmp_path):
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (tmp_path / "data").mkdir()
    meta_file = metadata / "meta.json"
    meta_file.write_text(json.dumps({"lastUpdate": "2024-05-01T00:00:00+05:30"}), encoding="utf-8")
    update_stock = Mock()
    deps = _failing_sync_deps(update_stock)

    for _ in range(2):
        sync_database(tmp_path / "data", _logger(), metadata, None, deps)
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        assert meta["lastUpdate"] == "2024-05-01T00:00:00+05:30"
    assert meta["missingSessionAttempts"] == {"2024-05-02": 2}

    # The third failed run, a week later, presumes an unlisted holiday and
    # stops again at the next session instead of skipping it too.
    sync_database(tmp_path / "data", _logger(), metadata, None, deps)
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["lastUpdate"].startswith("2024-05-02")
    assert meta["skippedSessions"] == ["2024-05-02"]
    assert meta["missingSessionAttempts"] == {"2024-05-03": 1}
    update_stock.assert_not_called()
