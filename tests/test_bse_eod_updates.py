import json
from datetime import date, datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

import config
from bse_eod_downloader import eod_downloader
from bse_eod_downloader.stock_data import update_bse_eod
from eod_store import create_compact_schema, upsert_daily_prices


def _dates(value: str):
    dates = eod_downloader.Dates(value, ZoneInfo("Asia/Kolkata"))
    dates.dt = datetime.fromisoformat(value)
    dates.pandasDt = dates.dt.strftime("%Y-%m-%d")
    return dates


def _daily_rows(symbol="PRIMARY", isin="INE_SAME", close=105.0):
    return pd.DataFrame({
        "TckrSymb": [symbol],
        "ISIN": [isin],
        "SctySrs": ["A"],
        "OpnPric": [100.0],
        "HghPric": [110.0],
        "LwPric": [90.0],
        "ClsPric": [close],
        "TtlTradgVol": [100],
        "TOTAL_TRADES": [10],
        "DLV_QTY": [80],
    })


def _seed_database(database, rows):
    conn = duckdb.connect(str(database))
    create_compact_schema(conn)
    upsert_daily_prices(conn, rows)
    conn.close()


def test_bse_update_prefers_primary_ticker_without_writing_text(
    tmp_path, monkeypatch
):
    database = tmp_path / "prices.duckdb"
    _seed_database(database, pd.DataFrame(columns=[
        "Date", "Symbol", "Exchange", "ISIN", "Open", "High", "Low",
        "Close", "Volume", "Series",
    ]))
    monkeypatch.setattr(config, "DUCKDB_FILE", database)
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    rows = pd.concat([
        _daily_rows("PRIMARY#", close=999.0).assign(TtlTradgVol=50_000),
        _daily_rows("PRIMARY", close=105.0),
    ], ignore_index=True)

    update_bse_eod(
        rows, tmp_path / "text", Mock(), metadata,
        _dates("2026-07-01T00:00:00+05:30"),
    )

    conn = duckdb.connect(str(database), read_only=True)
    result = conn.execute("SELECT Symbol, Close FROM eod_data").fetchall()
    conn.close()
    assert result == [("PRIMARY", 105.0)]
    assert not (tmp_path / "text").exists()


def test_bse_update_keeps_older_rows_beyond_600_only_in_duckdb(
    tmp_path, monkeypatch
):
    database = tmp_path / "prices.duckdb"
    dates = pd.date_range("2024-01-01", periods=600)
    seed = pd.DataFrame({
        "Date": dates,
        "Symbol": "KEEP600",
        "Exchange": "BSE",
        "ISIN": "INE_600",
        "Open": 100.0,
        "High": 110.0,
        "Low": 90.0,
        "Close": 105.0,
        "Volume": 100,
        "Series": "A",
    })
    _seed_database(database, seed)
    monkeypatch.setattr(config, "DUCKDB_FILE", database)
    text_dir = tmp_path / "text"
    metadata = tmp_path / "metadata"
    metadata.mkdir()

    update_bse_eod(
        _daily_rows("KEEP600", "INE_600", 120.0),
        text_dir,
        Mock(),
        metadata,
        _dates("2025-08-23T00:00:00+05:30"),
    )

    conn = duckdb.connect(str(database), read_only=True)
    count, first_date, last_date = conn.execute(
        """
        SELECT COUNT(*), MIN(Date), MAX(Date)
        FROM eod_data WHERE Symbol = 'KEEP600'
        """
    ).fetchone()
    conn.close()
    assert count == 601
    assert str(first_date) == "2024-01-01"
    assert str(last_date) == "2025-08-23"
    assert not text_dir.exists()


def test_corporate_action_ledger_prevents_double_duckdb_adjustment(
    tmp_path, monkeypatch
):
    database = tmp_path / "prices.duckdb"
    prices = pd.DataFrame({
        "Date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
        "Symbol": "SPLITCO",
        "Exchange": "BSE",
        "ISIN": "INE_TEST",
        "Open": [100.0, 110.0],
        "High": [100.0, 110.0],
        "Low": [100.0, 110.0],
        "Close": [100.0, 110.0],
        "Volume": 100,
        "Series": "A",
    })
    _seed_database(database, prices)
    monkeypatch.setattr(config, "DUCKDB_FILE", database)
    text_dir = tmp_path / "text"
    dates = _dates("2026-07-03T00:00:00+05:30")

    first = eod_downloader._make_adjustment(
        "SPLITCO", 2.0, text_dir, dates, Mock(), "same-action"
    )
    second = eod_downloader._make_adjustment(
        "SPLITCO", 2.0, text_dir, dates, Mock(), "same-action"
    )

    conn = duckdb.connect(str(database), read_only=True)
    closes = [
        row[0]
        for row in conn.execute(
            "SELECT Close FROM eod_data ORDER BY Date"
        ).fetchall()
    ]
    ledger_rows = conn.execute(
        "SELECT COUNT(*) FROM bse_corporate_action_ledger"
    ).fetchone()[0]
    conn.close()
    assert first == ("applied", "SPLITCO", 2)
    assert second == ("already_applied", "SPLITCO", 2)
    assert closes == [50.0, 55.0]
    assert ledger_rows == 1
    assert not text_dir.exists()


class _OneSessionDates(eod_downloader.Dates):
    """Walk exactly one BSE session (Wednesday 2026-07-01)."""

    def nextDate(self):
        self.dt = self.dt + timedelta(1)
        if self.dt.date() > date(2026, 7, 1):
            return False
        self.pandasDt = self.dt.strftime("%Y-%m-%d")
        return True


class _Response:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


def _bhavcopy_bytes():
    return pd.DataFrame({
        "FinInstrmId": ["500001"], "TckrSymb": ["BSEONLY"], "ISIN": ["INE777B01010"],
        "SctySrs": ["A"], "OpnPric": [100.0], "HghPric": [110.0], "LwPric": [90.0],
        "ClsPric": [105.0], "TtlTradgVol": [1000], "TtlNbOfTxsExctd": [50],
    }).to_csv(index=False).encode("utf-8")


def _run_bse_sync(tmp_path, monkeypatch, responses):
    database = tmp_path / "prices.duckdb"
    _seed_database(database, pd.DataFrame(columns=[
        "Date", "Symbol", "Exchange", "ISIN", "Open", "High", "Low",
        "Close", "Volume", "Series",
    ]))
    monkeypatch.setattr(config, "DUCKDB_FILE", database)
    monkeypatch.setattr(eod_downloader, "Dates", _OneSessionDates)
    monkeypatch.setattr(eod_downloader, "_download_corporate_actions", lambda **kwargs: pd.DataFrame())

    class _Session:
        headers = {}

        def get(self, url, timeout):
            key = "deliverables" if "SCBSEALL" in url else "bhavcopy"
            return responses[key]

    monkeypatch.setattr(eod_downloader.requests, "Session", _Session)
    metadata = tmp_path / "metadata"
    metadata.mkdir(exist_ok=True)
    (metadata / "bse_meta.json").write_text(
        json.dumps({"lastUpdate": "2026-06-30T00:00:00+05:30"}), encoding="utf-8"
    )
    result = eod_downloader.sync_database(tmp_path / "text", Mock(), metadata, tmp_path / "daily")
    meta = json.loads((metadata / "bse_meta.json").read_text(encoding="utf-8"))
    conn = duckdb.connect(str(database), read_only=True)
    rows = conn.execute("SELECT Symbol, Date, Close FROM eod_data").fetchall()
    conn.close()
    return result, meta, rows


def test_missing_bse_bhavcopy_is_retried_instead_of_skipped(tmp_path, monkeypatch):
    result, meta, rows = _run_bse_sync(tmp_path, monkeypatch, {
        "bhavcopy": _Response(404),
        "deliverables": _Response(404),
    })

    assert result is False
    assert meta["lastUpdate"] == "2026-06-30T00:00:00+05:30"
    assert meta["missingSessionAttempts"] == {"2026-07-01": 1}
    assert rows == []


def test_missing_bse_deliverables_still_store_prices(tmp_path, monkeypatch):
    result, meta, rows = _run_bse_sync(tmp_path, monkeypatch, {
        "bhavcopy": _Response(200, _bhavcopy_bytes()),
        "deliverables": _Response(404),
    })

    assert result is True
    assert meta["lastUpdate"].startswith("2026-07-01")
    assert rows == [("BSEONLY", date(2026, 7, 1), 105.0)]


def test_bse_consolidation_raises_prior_prices(tmp_path, monkeypatch):
    database = tmp_path / "prices.duckdb"
    _seed_database(database, pd.DataFrame({
        "Date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
        "Symbol": "CONSOL", "Exchange": "BSE", "ISIN": "INE888C01012",
        "Open": 2.0, "High": 2.0, "Low": 2.0, "Close": 2.0,
        "Volume": 100, "Series": "A",
    }))
    monkeypatch.setattr(config, "DUCKDB_FILE", database)
    dates = _dates("2026-07-03T00:00:00+05:30")
    actions = pd.DataFrame({
        "Security Code": ["500002"], "Security Name": ["CONSOL"],
        "Purpose": ["Consolidation of Shares from Rs 1/- to Rs 10/-"],
        "Detail/Remarks": [""],
        "Ex_Date_Parsed": [dates.dt.replace(tzinfo=None)],
    })

    eod_downloader._adjust_bse_stocks(actions, tmp_path, dates, Mock())

    conn = duckdb.connect(str(database), read_only=True)
    closes = conn.execute("SELECT Close FROM eod_data ORDER BY Date").fetchall()
    conn.close()
    assert closes == [(20.0,), (20.0,)]
