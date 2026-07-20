"""Regression tests for the EOD data-maintenance boundary.

These tests use temporary files and mocked NSE responses.  They do not call
the live NSE service or modify the project's real database.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd

from nse_eod_downloader import eod_downloader as eod
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


def test_split_and_bonus_adjustment_only_change_history(tmp_path):
    dates = _dates()
    index = pd.date_range("2024-05-01", periods=3, freq="D")
    prices = pd.DataFrame(
        {"Open": [100] * 3, "High": [100] * 3, "Low": [100] * 3, "Close": [100] * 3},
        index=index,
    )
    prices.index.name = "Date"
    prices.to_csv(tmp_path / "abc.csv")

    eod._adjust_nse_stocks(
        {
            "equityActions": [{
                "symbol": "ABC",
                "subject": "Face Value Split (Sub-Division) - From Rs 5/- Per Share To Rs 2/- Per Share",
                "exDate": "02-May-2024",
                "series": "EQ",
            }]
        },
        tmp_path,
        dates,
        _logger(),
    )
    adjusted = pd.read_csv(tmp_path / "abc.csv", index_col="Date")
    assert adjusted.loc["2024-05-01", "Close"] == 40
    assert adjusted.loc["2024-05-02", "Close"] == 100

    prices.to_csv(tmp_path / "bonus.csv")
    eod._adjust_nse_stocks(
        {
            "equityActions": [{
                "symbol": "BONUS",
                "subject": "Bonus 1:1",
                "exDate": "02-May-2024",
                "series": "EQ",
            }]
        },
        tmp_path,
        dates,
        _logger(),
    )
    bonus = pd.read_csv(tmp_path / "bonus.csv", index_col="Date")
    assert bonus.loc["2024-05-01", "Close"] == 50
    assert bonus.loc["2024-05-02", "Close"] == 100


def test_symbol_rename_preserves_history(tmp_path):
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
    (tmp_path / "oldbob.csv").write_text(
        "Date,Open,High,Low,Close,Volume,Series,TOTAL_TRADES,QTY_PER_TRADE,DLV_QTY\n"
        "2024-05-01,100,100,90,95,900,EQ,90,10,400\n"
    )
    isin = pd.DataFrame({"SYMBOL": ["OLDBOB"]}, index=pd.Index(["ISIN1"], name="ISIN"))

    eod._update_nse_eod(bhav, delivery, tmp_path, metadata, isin, _dates("2024-05-03T00:00:00+05:30"), _logger())

    assert not (tmp_path / "oldbob.csv").exists()
    assert (tmp_path / "newbob.csv").exists()
    assert len(pd.read_csv(tmp_path / "newbob.csv")) == 2
    assert isin.loc["ISIN1", "SYMBOL"] == "NEWBOB"


def test_pending_delivery_updates_daily_file_and_cleans_download(tmp_path):
    daily = tmp_path / "abc.csv"
    pd.DataFrame([{
        "Date": "2024-05-02", "Open": 100, "High": 100, "Low": 90,
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


def test_index_update_normalizes_symbol_and_prevents_duplicate(tmp_path):
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
    eod._update_index_eod(report, tmp_path, dates, _logger(), rows)

    daily = tmp_path / "nifty 50-test.csv"
    assert daily.exists()
    assert len(daily.read_text().splitlines()) == 2
    assert len(rows) == 2


def test_sync_entrypoint_rejects_missing_metadata(tmp_path):
    logger = Mock()
    assert eod.sync_database(tmp_path, logger, tmp_path / "metadata") is False
    logger.error.assert_called_once()
