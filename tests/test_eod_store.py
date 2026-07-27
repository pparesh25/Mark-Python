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


def test_isin_change_for_same_symbol_does_not_multiply_price_rows(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    try:
        create_compact_schema(conn)
        upsert_daily_prices(
            conn,
            _prices("SPLITCO", "INE_OLD", pd.date_range("2025-01-01", periods=2)),
        )
        upsert_daily_prices(
            conn,
            _prices("SPLITCO", "INE_NEW", pd.date_range("2025-01-03", periods=1)),
        )

        assert conn.execute("SELECT COUNT(*) FROM eod_prices").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM eod_data").fetchone()[0] == 3
        assert conn.execute(
            "SELECT DISTINCT ISIN FROM eod_data"
        ).fetchall() == [("INE_NEW",)]
        assert conn.execute(
            "SELECT COUNT(*) FROM securities WHERE Symbol = 'SPLITCO'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_symbol_rename_collision_keeps_one_liquid_row_per_date(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    try:
        create_compact_schema(conn)
        old_history = _prices(
            "OLD", "INE_STABLE", pd.date_range("2025-01-01", periods=2)
        )
        old_history["Volume"] = 100
        occupied_ticker = _prices(
            "NEW", "INE_RETIRED", pd.date_range("2025-01-01", periods=2)
        )
        occupied_ticker["Volume"] = 200
        upsert_daily_prices(conn, old_history)
        upsert_daily_prices(conn, occupied_ticker)

        renamed_day = _prices(
            "NEW", "INE_STABLE", pd.date_range("2025-01-03", periods=1)
        )
        upsert_daily_prices(conn, renamed_day)

        rows = conn.execute(
            """
            SELECT Date, Volume
            FROM eod_data
            WHERE Exchange = 'NSE' AND Symbol = 'NEW'
            ORDER BY Date
            """
        ).fetchall()
        assert rows == [
            (pd.Timestamp("2025-01-01").date(), 200),
            (pd.Timestamp("2025-01-02").date(), 200),
            (pd.Timestamp("2025-01-03").date(), 100),
        ]
        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT Date
                FROM eod_prices
                GROUP BY Exchange, Symbol, Date
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_rerun_updates_existing_date_without_adding_a_row(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    try:
        create_compact_schema(conn)
        first = _prices("ABC", "INE000A", pd.date_range("2025-01-01", periods=1))
        corrected = first.copy()
        corrected.loc[0, ["Open", "High", "Low", "Close", "Volume"]] = [
            10, 12, 9, 11, 250
        ]

        upsert_daily_prices(conn, first)
        upsert_daily_prices(conn, corrected)

        assert conn.execute(
            """
            SELECT COUNT(*), MIN(Open), MIN(High), MIN(Low), MIN(Close), MIN(Volume)
            FROM eod_prices
            WHERE Exchange = 'NSE' AND Symbol = 'ABC'
            """
        ).fetchone() == (1, 10, 12, 9, 11, 250)
    finally:
        conn.close()
