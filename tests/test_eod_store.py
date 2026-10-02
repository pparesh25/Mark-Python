from __future__ import annotations

import duckdb
import pandas as pd

from eod_store import create_compact_schema, upsert_daily_prices


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


def test_a_rename_moves_the_raw_history_to_the_new_ticker(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    try:
        create_compact_schema(conn)
        upsert_daily_prices(conn, _prices("OLD", "INE000A", pd.date_range("2025-01-01", periods=5)))
        upsert_daily_prices(conn, _prices("NEW", "INE000A", pd.date_range("2025-01-06", periods=1)))

        assert conn.execute("SELECT COUNT(*) FROM raw_prices").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM eod_data").fetchone()[0] == 6
        assert conn.execute("SELECT DISTINCT Symbol FROM eod_data").fetchall() == [("NEW",)]
        assert conn.execute("SELECT ISIN, Series FROM eod_data LIMIT 1").fetchone() == ("INE000A", "EQ")
        assert conn.execute("SELECT Total_Trades FROM eod_data LIMIT 1").fetchone() == (None,)
    finally:
        conn.close()


def test_daily_upsert_keeps_full_history_by_default(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    try:
        create_compact_schema(conn)
        upsert_daily_prices(
            conn,
            _prices(
                "FULL",
                "INE_FULL",
                pd.date_range("2024-01-01", periods=600),
            ),
        )
        upsert_daily_prices(
            conn,
            _prices(
                "FULL",
                "INE_FULL",
                pd.date_range("2025-08-23", periods=1),
            ),
        )

        assert conn.execute(
            "SELECT COUNT(*) FROM eod_data WHERE Symbol = 'FULL'"
        ).fetchone()[0] == 601
    finally:
        conn.close()


def test_isin_change_for_same_symbol_does_not_multiply_price_rows(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    try:
        create_compact_schema(conn)
        upsert_daily_prices(
            conn,
            _prices("SPLITCO", "INE123A01016", pd.date_range("2025-01-01", periods=2)),
        )
        # A face-value split keeps the issuer prefix and only changes the serial.
        upsert_daily_prices(
            conn,
            _prices("SPLITCO", "INE123A01024", pd.date_range("2025-01-03", periods=1)),
        )

        assert conn.execute("SELECT COUNT(*) FROM raw_prices").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM eod_data").fetchone()[0] == 3
        assert conn.execute(
            "SELECT DISTINCT ISIN FROM eod_data"
        ).fetchall() == [("INE123A01024",)]
        assert conn.execute(
            "SELECT COUNT(*) FROM securities WHERE Symbol = 'SPLITCO'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def _history(conn, symbol):
    return conn.execute(
        """
        SELECT Date, Close, Volume, ISIN
        FROM eod_data
        WHERE Exchange = 'NSE' AND Symbol = ?
        ORDER BY Date
        """,
        [symbol],
    ).fetchall()


def test_rename_onto_retired_ticker_discards_the_previous_owner(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    try:
        create_compact_schema(conn)
        stable = _prices("OLD", "INE111A01011", pd.date_range("2025-01-01", periods=2))
        stable["Volume"] = 100
        retired = _prices("NEW", "INE999Z01019", pd.date_range("2024-12-30", periods=4))
        retired["Close"] = 1000.0
        retired["Volume"] = 200
        upsert_daily_prices(conn, stable)
        upsert_daily_prices(conn, retired)

        upsert_daily_prices(
            conn,
            _prices("NEW", "INE111A01011", pd.date_range("2025-01-03", periods=1)),
        )

        date = lambda text: pd.Timestamp(text).date()
        assert _history(conn, "NEW") == [
            (date("2025-01-01"), 1.0, 100, "INE111A01011"),
            (date("2025-01-02"), 2.0, 100, "INE111A01011"),
            (date("2025-01-03"), 1.0, 100, "INE111A01011"),
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM eod_prices WHERE Close = 1000.0"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_new_issuer_on_reused_ticker_starts_with_clean_history(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    try:
        create_compact_schema(conn)
        upsert_daily_prices(
            conn,
            _prices("REUSED", "INE555B01012", pd.date_range("2025-01-01", periods=3)),
        )
        upsert_daily_prices(
            conn,
            _prices("REUSED", "INE777C01014", pd.date_range("2025-03-03", periods=1)),
        )

        assert _history(conn, "REUSED") == [
            (pd.Timestamp("2025-03-03").date(), 1.0, 100, "INE777C01014"),
        ]
    finally:
        conn.close()


def test_same_day_ticker_swap_keeps_both_histories(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    try:
        create_compact_schema(conn)
        first = _prices("AAA", "INE100A01010", pd.date_range("2025-01-01", periods=2))
        second = _prices("BBB", "INE200B01010", pd.date_range("2025-01-01", periods=2))
        second["Close"] = 500.0
        upsert_daily_prices(conn, pd.concat([first, second], ignore_index=True))

        swap = pd.concat([
            _prices("BBB", "INE100A01010", pd.date_range("2025-01-03", periods=1)),
            _prices("AAA", "INE200B01010", pd.date_range("2025-01-03", periods=1)).assign(Close=510.0),
        ], ignore_index=True)
        upsert_daily_prices(conn, swap)

        assert [row[1] for row in _history(conn, "BBB")] == [1.0, 2.0, 1.0]
        assert [row[1] for row in _history(conn, "AAA")] == [500.0, 500.0, 510.0]
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


def test_compaction_copies_every_table_and_keeps_two_years(tmp_path):
    from compact_price_database import install_compact_database

    source = tmp_path / "prices.duckdb"
    conn = duckdb.connect(str(source))
    create_compact_schema(conn)
    upsert_daily_prices(conn, _prices("OLDCO", "INE000C", pd.date_range("2023-01-02", periods=3)))
    upsert_daily_prices(conn, _prices("OLDCO", "INE000C", pd.date_range("2026-01-02", periods=3)))
    conn.execute("INSERT INTO exchange_actions (Exchange, Symbol, Purpose) VALUES ('NSE', 'OLDCO', 'Bonus 1:1')")
    conn.close()

    backup = install_compact_database(source, years=2)

    conn = duckdb.connect(str(source), read_only=True)
    try:
        assert conn.execute("SELECT min(Date), count(*) FROM eod_data").fetchone() == (pd.Timestamp("2026-01-02").date(), 3)
        assert conn.execute("SELECT count(*) FROM exchange_actions").fetchone()[0] == 1
    finally:
        conn.close()
    assert backup.exists()
