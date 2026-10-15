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


def test_a_known_special_session_is_not_skipped_and_learnt_sessions_are_kept():
    dates = _dates("2025-02-01T00:00:00+05:30")  # Saturday: Union Budget session
    dates.today = datetime(2025, 2, 2)
    assert eod._check_for_holidays(Mock(), dates, {"holidays": {}, "year": 2025}, _logger()) is False

    nse = Mock()
    nse.circulars.return_value = {"data": []}
    meta = {"special_sessions": ["2026-02-01T00:00:00"]}             # no special_sessions_last_update
    eod._check_special_sessions(nse, meta, _dates("2026-03-02T00:00:00+05:30"), _logger(), TZ_IN)
    assert meta["special_sessions"] == ["2026-02-01T00:00:00"]       # used to be reset to []


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


def test_unpublished_delivery_figure_is_stored_as_missing(tmp_path, monkeypatch):
    # NSE's 2026-09-11 delivery file carried " -" for DELIV_QTY and stopped
    # the whole day's sync.
    database = tmp_path / "prices.duckdb"
    conn = duckdb.connect(str(database))
    create_compact_schema(conn)
    conn.close()
    monkeypatch.setattr(config, "DUCKDB_FILE", database)
    bhav = tmp_path / "bhav.csv"
    delivery = tmp_path / "delivery.csv"
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    pd.DataFrame([
        {"ISIN": "ISIN1", "TckrSymb": "HASDLV", "SctySrs": "EQ",
         "OpnPric": 110, "HghPric": 120, "LwPric": 100, "ClsPric": 115,
         "TtlTradgVol": 1000},
        {"ISIN": "ISIN2", "TckrSymb": "NODLV", "SctySrs": "EQ",
         "OpnPric": 10, "HghPric": 12, "LwPric": 9, "ClsPric": 11,
         "TtlTradgVol": 400},
    ]).to_csv(bhav, index=False)
    pd.DataFrame([
        {"SYMBOL": "HASDLV", " SERIES": " EQ", " NO_OF_TRADES": 100,
         " DELIV_QTY": " 500"},
        {"SYMBOL": "NODLV", " SERIES": " EQ", " NO_OF_TRADES": 40,
         " DELIV_QTY": " -"},
    ]).to_csv(delivery, index=False)
    isin = pd.DataFrame(
        {"SYMBOL": ["HASDLV", "NODLV"]},
        index=pd.Index(["ISIN1", "ISIN2"], name="ISIN"),
    )

    text_rows = []
    eod._update_nse_eod(
        bhav, delivery, tmp_path / "text", metadata, isin,
        _dates("2024-05-03T00:00:00+05:30"), _logger(),
        bhavcopy_rows=text_rows,
    )

    conn = duckdb.connect(str(database), read_only=True)
    rows = conn.execute(
        "SELECT Symbol, Close FROM eod_data ORDER BY Symbol"
    ).fetchall()
    conn.close()
    assert rows == [("HASDLV", 115.0), ("NODLV", 11.0)]
    # The compact store keeps no delivery columns; the text row does.
    assert [row.strip().split(",")[-3:] for row in text_rows] == [
        ["100", "10.0", "500"],
        ["40", "10.0", "nan"],
    ]


def _store_with(tmp_path, monkeypatch, rows):
    """A temporary price store holding `rows` (Symbol, Volume) for NSE 2024-05-02, as the sync stored them."""
    database = tmp_path / "prices.duckdb"
    conn = duckdb.connect(str(database))
    create_compact_schema(conn)
    for n, (symbol, volume) in enumerate(rows):
        upsert_daily_prices(conn, pd.DataFrame([{
            "Date": "2024-05-02", "Symbol": symbol, "Exchange": "NSE", "ISIN": f"INE00000{n}011",
            "Open": 100, "High": 100, "Low": 90, "Close": 95, "Volume": volume, "Series": "EQ",
        }]))
    conn.close()
    monkeypatch.setattr(config, "DUCKDB_FILE", database)
    return database


def _stored_delivery(database):
    conn = duckdb.connect(str(database), read_only=True)
    try:
        return {s: (t, d) for s, t, d in conn.execute(
            "SELECT Symbol, Total_Trades, Dlv_Qty FROM raw_prices WHERE Date = '2024-05-02'").fetchall()}
    finally:
        conn.close()


def test_pending_delivery_updates_daily_file_and_cleans_download(tmp_path, monkeypatch):
    database = _store_with(tmp_path, monkeypatch, [("ABC", 900)])
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
    assert _stored_delivery(database) == {"ABC": (100, 500)}       # the store the screener reads has it too


def test_pending_delivery_keeps_an_unpublished_figure_empty(tmp_path, monkeypatch):
    # NSE writes " -" where a figure is not published; float() on that string
    # used to abort the whole late-delivery update for the day.
    database = _store_with(tmp_path, monkeypatch, [("ABC", 900), ("NODLV", 400)])
    daily = tmp_path / "20240502_BENNY_NSE_EOD.txt"
    pd.DataFrame([
        {
            "Symbol": "ABC", "Date": "2024-05-02", "Open": 100, "High": 100,
            "Low": 90, "Close": 95, "Volume": 900, "Series": "EQ",
            "TOTAL_TRADES": "", "QTY_PER_TRADE": "", "DLV_QTY": "",
        },
        {
            "Symbol": "NODLV", "Date": "2024-05-02", "Open": 10, "High": 12,
            "Low": 9, "Close": 11, "Volume": 400, "Series": "EQ",
            "TOTAL_TRADES": "", "QTY_PER_TRADE": "", "DLV_QTY": "",
        },
    ]).to_csv(daily, index=False)
    report = tmp_path / "delivery.csv"
    pd.DataFrame([
        {"SYMBOL": "ABC", " SERIES": " EQ", " NO_OF_TRADES": 100, " DELIV_QTY": 500},
        {"SYMBOL": "NODLV", " SERIES": " EQ", " NO_OF_TRADES": " -", " DELIV_QTY": " -"},
    ]).to_csv(report, index=False)
    nse = Mock()
    nse.deliveryBhavcopy.return_value = report

    assert eod._update_pending_delivery_data(
        nse, "2024-05-02T00:00:00+05:30", tmp_path, _logger(), TZ_IN
    ) is True

    result = pd.read_csv(daily).set_index("Symbol")
    assert int(result.loc["ABC", "TOTAL_TRADES"]) == 100
    assert int(result.loc["ABC", "DLV_QTY"]) == 500
    # The unpublished row is recorded as missing instead of losing the day.
    assert pd.isna(result.loc["NODLV", "TOTAL_TRADES"])
    assert pd.isna(result.loc["NODLV", "QTY_PER_TRADE"])
    assert pd.isna(result.loc["NODLV", "DLV_QTY"])
    assert not report.exists()
    assert _stored_delivery(database) == {"ABC": (100, 500), "NODLV": (None, None)}


def test_a_late_delivery_report_reaches_raw_prices_without_a_text_copy(tmp_path, monkeypatch):
    # The sync stored the day without delivery; the report came the next day.
    # It used to rewrite only the optional text file, so the store kept NULLs
    # forever while the date left DLV_PENDING_DATES.
    database = _store_with(tmp_path, monkeypatch, [("ABC", 900), ("TFT", 700), ("SMEX_SME", 3000), ("NOTINFILE", 50)])
    report = tmp_path / "delivery.csv"
    pd.DataFrame([
        {"SYMBOL": "ABC", " SERIES": " EQ", " DATE1": " 02-May-2024", " NO_OF_TRADES": 100, " DELIV_QTY": 500},
        {"SYMBOL": "TFT", " SERIES": " BE", " DATE1": " 02-May-2024", " NO_OF_TRADES": 30, " DELIV_QTY": " -"},
        {"SYMBOL": "SMEX", " SERIES": " SM", " DATE1": " 02-May-2024", " NO_OF_TRADES": 12, " DELIV_QTY": 2400},
        {"SYMBOL": "BOND1", " SERIES": " N1", " DATE1": " 02-May-2024", " NO_OF_TRADES": 2, " DELIV_QTY": 10},
    ]).to_csv(report, index=False)
    nse = Mock()
    nse.deliveryBhavcopy.return_value = report

    assert eod._update_pending_delivery_data(nse, "2024-05-02T00:00:00+05:30", None, _logger(), TZ_IN) is True
    assert _stored_delivery(database) == {"ABC": (100, 500), "TFT": (30, 700),     # trade-for-trade: volume
                                          "SMEX_SME": (12, 2400), "NOTINFILE": (None, None)}
    assert not report.exists()


def test_a_late_delivery_report_that_updates_no_row_stays_pending_until_given_up(tmp_path, monkeypatch):
    yesterday = (datetime.now(TZ_IN) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    database = tmp_path / "prices.duckdb"
    conn = duckdb.connect(str(database))
    create_compact_schema(conn)
    upsert_daily_prices(conn, pd.DataFrame([{"Date": yesterday.date(), "Symbol": "ABC", "Exchange": "NSE",
                                             "ISIN": "INE000000011", "Open": 1, "High": 1, "Low": 1, "Close": 1,
                                             "Volume": 900, "Series": "EQ"}]))
    conn.close()
    monkeypatch.setattr(config, "DUCKDB_FILE", database)

    def report_for(day_text, symbol="ABC"):
        report = tmp_path / "delivery.csv"
        pd.DataFrame([{"SYMBOL": symbol, " SERIES": " EQ", " DATE1": f" {day_text}", " NO_OF_TRADES": 100,
                       " DELIV_QTY": 500}]).to_csv(report, index=False)
        nse = Mock()
        nse.deliveryBhavcopy.return_value = report
        return nse

    logger = _logger()
    other_day = f"{yesterday - timedelta(days=3):%d-%b-%Y}"
    assert eod._update_pending_delivery_data(report_for(other_day), yesterday.isoformat(), None, logger, TZ_IN) is False
    assert "holds another day" in str(logger.info.call_args)
    assert eod._update_pending_delivery_data(report_for(f"{yesterday:%d-%b-%Y}", "NOTSTORED"), yesterday.isoformat(),
                                             None, logger, TZ_IN) is False
    assert "matched no stored NSE row" in str(logger.info.call_args)
    assert _stored_delivery_on(database, yesterday.date()) == {"ABC": (None, None)}
    # five days on it is given up, with a warning saying why
    old = (yesterday - timedelta(days=6)).isoformat()
    assert eod._update_pending_delivery_data(report_for(other_day), old, None, logger, TZ_IN) is True
    assert "Max attempts reached" in str(logger.warning.call_args)
    assert eod._update_pending_delivery_data(report_for(f"{yesterday:%d-%b-%Y}"), yesterday.isoformat(),
                                             None, logger, TZ_IN) is True
    assert _stored_delivery_on(database, yesterday.date()) == {"ABC": (100, 500)}


def _stored_delivery_on(database, day):
    conn = duckdb.connect(str(database), read_only=True)
    try:
        return {s: (t, d) for s, t, d in conn.execute(
            "SELECT Symbol, Total_Trades, Dlv_Qty FROM raw_prices WHERE Date = ?", [day]).fetchall()}
    finally:
        conn.close()


def test_a_late_delivery_report_the_store_could_not_take_stays_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DUCKDB_FILE", tmp_path / "missing-folder" / "prices.duckdb")
    report = tmp_path / "delivery.csv"
    pd.DataFrame([{"SYMBOL": "ABC", " SERIES": " EQ", " NO_OF_TRADES": 100, " DELIV_QTY": 500}]).to_csv(report, index=False)
    nse = Mock()
    nse.deliveryBhavcopy.return_value = report
    assert eod._update_pending_delivery_data(nse, "2024-05-02T00:00:00+05:30", None, _logger(), TZ_IN) is False

    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (tmp_path / "data").mkdir()
    meta_file = metadata / "meta.json"
    meta_file.write_text(json.dumps({"lastUpdate": "2024-05-03T00:00:00+05:30",
                                     "DLV_PENDING_DATES": ["2024-05-02T00:00:00+05:30"]}), encoding="utf-8")
    deps = _failing_sync_deps(Mock())
    deps = SyncDependencies(**{**deps.__dict__, "update_pending_delivery": lambda *args: False})
    sync_database(tmp_path / "data", _logger(), metadata, None, deps)
    assert json.loads(meta_file.read_text(encoding="utf-8"))["DLV_PENDING_DATES"] == ["2024-05-02T00:00:00+05:30"]


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
        check_reports_status=lambda *args: {},
        update_pending_delivery=lambda *args: False,
        update_stock=update_stock,
        update_index=Mock(),
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
