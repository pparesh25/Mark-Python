"""Compact DuckDB price-store helpers.

The price fact table deliberately has no primary-key or secondary indexes.
DuckDB scans compressed columnar data efficiently, while the old compound
primary key consumed substantial space for data that is naturally processed
in daily batches.  ISIN and Series are symbol metadata, not daily facts.
"""

from __future__ import annotations

import pandas as pd

import config

RETAIN_CANDLES = config.EOD_RETAIN_CANDLES
PRICE_COLUMNS = ["Date", "Symbol", "Exchange", "Open", "High", "Low", "Close", "Volume"]


def create_compact_schema(conn) -> None:
    """Create the compact tables and read-compatible ``eod_data`` view."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS securities (
            Exchange VARCHAR NOT NULL,
            Symbol VARCHAR NOT NULL,
            ISIN VARCHAR NOT NULL,
            Series VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eod_prices (
            Date DATE NOT NULL,
            Symbol VARCHAR NOT NULL,
            Exchange VARCHAR NOT NULL,
            Open DOUBLE,
            High DOUBLE,
            Low DOUBLE,
            Close DOUBLE,
            Volume BIGINT
        )
        """
    )
    conn.execute(
        """
        CREATE OR REPLACE VIEW eod_data AS
        SELECT p.Date, p.Symbol, p.Exchange, s.ISIN,
               p.Open, p.High, p.Low, p.Close, p.Volume, s.Series,
               CAST(NULL AS INTEGER) AS Total_Trades,
               CAST(NULL AS DOUBLE) AS Qty_Per_Trade,
               CAST(NULL AS BIGINT) AS Dlv_Qty
        FROM eod_prices p
        JOIN securities s
          ON s.Exchange = p.Exchange AND s.Symbol = p.Symbol
        """
    )


def _trim_symbols(conn, exchange: str, symbols: pd.Series, retain_candles: int) -> None:
    symbol_frame = pd.DataFrame({"Symbol": symbols.dropna().astype(str).unique()})
    if symbol_frame.empty:
        return
    conn.register("retention_symbols", symbol_frame)
    try:
        conn.execute(
            """
            DELETE FROM eod_prices
            WHERE rowid IN (
                SELECT rowid
                FROM (
                    SELECT p.rowid,
                           ROW_NUMBER() OVER (
                               PARTITION BY p.Symbol ORDER BY p.Date DESC
                           ) AS retention_rank
                    FROM eod_prices p
                    JOIN retention_symbols rs ON rs.Symbol = p.Symbol
                    WHERE p.Exchange = ?
                )
                WHERE retention_rank > ?
            )
            """,
            [exchange, retain_candles],
        )
    finally:
        conn.unregister("retention_symbols")


def _deduplicate_symbols(conn, exchange: str, symbols: pd.Series) -> None:
    """Collapse rename collisions to one row per symbol/date.

    A renamed ISIN can move old history onto a ticker that already has price
    rows.  Prefer the more liquid row for an overlapping date so the compact
    store remains deterministic and its text backup can mirror it exactly.
    """
    symbol_frame = pd.DataFrame({"Symbol": symbols.dropna().astype(str).unique()})
    if symbol_frame.empty:
        return
    conn.register("deduplicate_symbols", symbol_frame)
    try:
        conn.execute(
            """
            DELETE FROM eod_prices
            WHERE rowid IN (
                SELECT rowid
                FROM (
                    SELECT p.rowid,
                           ROW_NUMBER() OVER (
                               PARTITION BY p.Symbol, p.Date
                               ORDER BY COALESCE(p.Volume, 0) DESC, p.rowid DESC
                           ) AS duplicate_rank
                    FROM eod_prices p
                    JOIN deduplicate_symbols ds ON ds.Symbol = p.Symbol
                    WHERE p.Exchange = ?
                )
                WHERE duplicate_rank > 1
            )
            """,
            [exchange],
        )
    finally:
        conn.unregister("deduplicate_symbols")


def upsert_daily_prices(conn, rows: pd.DataFrame, retain_candles: int = RETAIN_CANDLES) -> None:
    """Upsert one exchange's daily rows and retain only its newest candles."""
    required = set(PRICE_COLUMNS + ["ISIN", "Series"])
    missing = required.difference(rows.columns)
    if missing:
        raise ValueError(f"Price rows missing columns: {sorted(missing)}")

    incoming = rows.loc[:, PRICE_COLUMNS + ["ISIN", "Series"]].copy()
    incoming = incoming.dropna(subset=["Date", "Symbol", "Exchange", "ISIN"])
    if incoming.empty:
        return
    incoming["Symbol"] = incoming["Symbol"].astype(str).str.strip().str.upper()
    incoming["Exchange"] = incoming["Exchange"].astype(str).str.strip().str.upper()
    incoming["ISIN"] = incoming["ISIN"].astype(str).str.strip().str.upper()
    incoming["Series"] = incoming["Series"].astype(str).str.strip()
    incoming["Date"] = pd.to_datetime(incoming["Date"]).dt.date

    exchanges = incoming["Exchange"].unique()
    if len(exchanges) != 1:
        raise ValueError("Each upsert batch must contain exactly one exchange")
    exchange = exchanges[0]

    conn.register("incoming_prices", incoming)
    try:
        # Rename retained price history by stable ISIN before updating metadata.
        conn.execute(
            """
            UPDATE eod_prices p
               SET Symbol = i.Symbol
              FROM (
                    SELECT DISTINCT Exchange, ISIN, Symbol
                    FROM incoming_prices
                   ) i
              JOIN securities s
                ON s.Exchange = i.Exchange AND s.ISIN = i.ISIN
             WHERE p.Exchange = s.Exchange
               AND p.Symbol = s.Symbol
               AND s.Symbol <> i.Symbol
            """
        )
        conn.execute(
            """
            UPDATE securities s
               SET Symbol = i.Symbol,
                   Series = i.Series
              FROM (
                    SELECT DISTINCT Exchange, ISIN, Symbol, Series
                    FROM incoming_prices
                   ) i
             WHERE s.Exchange = i.Exchange AND s.ISIN = i.ISIN
            """
        )
        # Splits can change an ISIN without changing the ticker.  The fact
        # table is keyed by ticker, so retain exactly the current ISIN metadata
        # for every symbol present in this batch.  Keeping both the old and new
        # ISIN would multiply every price row through the eod_data join.
        conn.execute(
            """
            DELETE FROM securities s
             USING (
                    SELECT DISTINCT Exchange, Symbol, ISIN
                    FROM incoming_prices
                   ) i
             WHERE s.Exchange = i.Exchange
               AND s.Symbol = i.Symbol
               AND s.ISIN <> i.ISIN
            """
        )
        conn.execute(
            """
            INSERT INTO securities (Exchange, Symbol, ISIN, Series)
            SELECT i.Exchange, i.Symbol, i.ISIN, i.Series
            FROM (
                SELECT DISTINCT Exchange, ISIN, Symbol, Series
                FROM incoming_prices
            ) i
            WHERE NOT EXISTS (
                SELECT 1
                FROM securities s
                WHERE s.Exchange = i.Exchange
                  AND (s.ISIN = i.ISIN OR s.Symbol = i.Symbol)
            )
            """
        )
        conn.execute(
            """
            UPDATE eod_prices p
               SET Open = i.Open,
                   High = i.High,
                   Low = i.Low,
                   Close = i.Close,
                   Volume = i.Volume
              FROM incoming_prices i
              JOIN securities s
                ON s.Exchange = i.Exchange AND s.ISIN = i.ISIN
             WHERE p.Date = i.Date
               AND p.Symbol = s.Symbol
               AND p.Exchange = i.Exchange
            """
        )
        conn.execute(
            """
            INSERT INTO eod_prices (Date, Symbol, Exchange, Open, High, Low, Close, Volume)
            SELECT i.Date, s.Symbol, i.Exchange,
                   i.Open, i.High, i.Low, i.Close, i.Volume
            FROM incoming_prices i
            JOIN securities s
              ON s.Exchange = i.Exchange AND s.ISIN = i.ISIN
            WHERE NOT EXISTS (
                SELECT 1
                FROM eod_prices p
                WHERE p.Date = i.Date
                  AND p.Symbol = s.Symbol
                  AND p.Exchange = i.Exchange
            )
            """
        )
        _deduplicate_symbols(conn, exchange, incoming["Symbol"])
        _trim_symbols(conn, exchange, incoming["Symbol"], retain_candles)
    finally:
        conn.unregister("incoming_prices")


def replace_symbol_prices(conn, exchange: str, symbol: str, frame: pd.DataFrame,
                          retain_candles: int = RETAIN_CANDLES) -> None:
    """Replace one symbol's retained OHLCV history after a corporate action."""
    required = {"Date", "Open", "High", "Low", "Close", "Volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Adjusted price frame missing columns: {sorted(missing)}")

    normalized_symbol = symbol.strip().upper()
    exchange = exchange.strip().upper()
    prices = frame.loc[:, ["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
    prices["Date"] = pd.to_datetime(prices["Date"]).dt.date
    prices["Symbol"] = normalized_symbol
    prices["Exchange"] = exchange
    prices = prices.loc[:, PRICE_COLUMNS]

    conn.register("replacement_prices", prices)
    try:
        conn.execute(
            "DELETE FROM eod_prices WHERE Exchange = ? AND Symbol = ?",
            [exchange, normalized_symbol],
        )
        conn.execute(
            """
            INSERT INTO eod_prices (Date, Symbol, Exchange, Open, High, Low, Close, Volume)
            SELECT Date, Symbol, Exchange, Open, High, Low, Close, Volume
            FROM replacement_prices
            """
        )
        _trim_symbols(conn, exchange, pd.Series([normalized_symbol]), retain_candles)
    finally:
        conn.unregister("replacement_prices")
