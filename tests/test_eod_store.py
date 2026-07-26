from __future__ import annotations

import duckdb
import pandas as pd

from eod_store import create_compact_schema, replace_symbol_prices, upsert_daily_prices


def _prices(symbol: str, isin: str, dates: pd.DatetimeIndex) -> pd.DataFrame:
    values = range(1, len(dates) + 1)
    return pd.DataFrame(
        {
            "Date": dates,
            "Symbol": symbol,
            "Exchange": "NSE",
            "ISIN": isin,
            "Open": values,
            "High": [value + 1 for value in values],
            "Low": [value - 1 for value in values],
            "Close": values,
            "Volume": [100] * len(dates),
            "Series": "EQ",
        }
    )


def test_compact_store_retains_rows_and_keeps_legacy_read_shape(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    try:
        create_compact_schema(conn)
        upsert_daily_prices(conn, _prices("OLD", "INE000A", pd.date_range("2025-01-01", periods=5)), retain_candles=3)
        upsert_daily_prices(conn, _prices("NEW", "INE000A", pd.date_range("2025-01-06", periods=1)), retain_candles=3)

        assert conn.execute("SELECT COUNT(*) FROM eod_prices").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM eod_data").fetchone()[0] == 3
        assert conn.execute("SELECT DISTINCT Symbol FROM eod_data").fetchall() == [("NEW",)]
        assert conn.execute("SELECT ISIN, Series FROM eod_data LIMIT 1").fetchone() == ("INE000A", "EQ")
        assert conn.execute("SELECT Total_Trades FROM eod_data LIMIT 1").fetchone() == (None,)
    finally:
        conn.close()


def test_replace_adjusted_prices_preserves_compact_schema(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    try:
        create_compact_schema(conn)
        source = _prices("ABC", "INE000B", pd.date_range("2025-01-01", periods=3))
        upsert_daily_prices(conn, source)
        adjusted = source.loc[:, ["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
        adjusted["Close"] *= 2
        replace_symbol_prices(conn, "NSE", "ABC", adjusted)

        assert conn.execute("SELECT COUNT(*) FROM eod_prices WHERE Symbol = 'ABC'").fetchone()[0] == 3
        assert conn.execute("SELECT MAX(Close) FROM eod_data WHERE Symbol = 'ABC'").fetchone()[0] == 6
    finally:
        conn.close()
