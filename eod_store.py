"""Compact DuckDB price-store helpers.

The price fact table deliberately has no primary-key or secondary indexes.
DuckDB scans compressed columnar data efficiently, while the old compound
primary key consumed substantial space for data that is naturally processed
in daily batches.  ISIN and Series are symbol metadata, not daily facts.
"""

from __future__ import annotations

import logging

import pandas as pd

import config

logger = logging.getLogger(__name__)

RETAIN_CANDLES = config.EOD_RETAIN_CANDLES
PRICE_COLUMNS = ["Date", "Symbol", "Exchange", "Open", "High", "Low", "Close", "Volume"]
# Indian ISINs are ``IN`` + issuer type + 4-character issuer code + security
# type + serial + check digit.  A face-value split or consolidation keeps the
# first seven characters and only changes the serial.
ISIN_ISSUER_PREFIX_LENGTH = 7


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


def _discard_reused_ticker_history(conn) -> None:
    """Delete stored prices that belong to another company under a reused ticker.

    Two cases hand a ticker that already has price rows to a different ISIN:

    * a known ISIN is renamed onto the ticker (its own history lives under its
      old ticker and is moved there next), unless the current owner is being
      renamed away in the same batch (a same-day swap);
    * a never-seen ISIN from a different issuer starts trading under a ticker
      whose previous owner is no longer trading.

    Without this step the old owner's candles stay under the ticker and are
    labelled with the new owner's ISIN, so charts and screens silently mix two
    companies.  A split keeps the issuer prefix and therefore keeps history.
    """
    targets = conn.execute(
        f"""
        WITH batch AS (
            SELECT DISTINCT Exchange, ISIN, Symbol FROM incoming_prices
        ),
        renames AS (
            SELECT b.Exchange, b.ISIN, s.Symbol AS old_symbol, b.Symbol AS new_symbol
            FROM batch b
            JOIN securities s ON s.Exchange = b.Exchange AND s.ISIN = b.ISIN
            WHERE s.Symbol <> b.Symbol
        ),
        rename_targets AS (
            SELECT r.Exchange, r.new_symbol AS Symbol
            FROM renames r
            WHERE NOT EXISTS (
                SELECT 1 FROM renames source
                WHERE source.Exchange = r.Exchange
                  AND source.old_symbol = r.new_symbol
            )
        ),
        new_issuer_targets AS (
            SELECT b.Exchange, b.Symbol
            FROM batch b
            JOIN securities s ON s.Exchange = b.Exchange AND s.Symbol = b.Symbol
            WHERE s.ISIN <> b.ISIN
              AND LEFT(s.ISIN, {ISIN_ISSUER_PREFIX_LENGTH})
                  <> LEFT(b.ISIN, {ISIN_ISSUER_PREFIX_LENGTH})
              AND NOT EXISTS (
                  SELECT 1 FROM securities known
                  WHERE known.Exchange = b.Exchange AND known.ISIN = b.ISIN
              )
              AND NOT EXISTS (
                  SELECT 1 FROM batch active
                  WHERE active.Exchange = s.Exchange AND active.ISIN = s.ISIN
              )
        )
        SELECT t.Exchange, t.Symbol, COUNT(p.Date) AS stale_rows
        FROM (
            SELECT Exchange, Symbol FROM rename_targets
            UNION
            SELECT Exchange, Symbol FROM new_issuer_targets
        ) t
        JOIN eod_prices p ON p.Exchange = t.Exchange AND p.Symbol = t.Symbol
        GROUP BY t.Exchange, t.Symbol
        ORDER BY t.Exchange, t.Symbol
        """
    ).fetchall()
    if not targets:
        return

    target_frame = pd.DataFrame(targets, columns=["Exchange", "Symbol", "stale_rows"])
    conn.register("reused_tickers", target_frame)
    try:
        conn.execute(
            """
            DELETE FROM eod_prices
            WHERE rowid IN (
                SELECT p.rowid
                FROM eod_prices p
                JOIN reused_tickers t
                  ON t.Exchange = p.Exchange AND t.Symbol = p.Symbol
            )
            """
        )
    finally:
        conn.unregister("reused_tickers")
    for exchange, symbol, stale_rows in targets:
        logger.warning(
            f"{exchange} {symbol}: ticker now belongs to another ISIN; "
            f"discarded {stale_rows} price rows of its previous owner."
        )


def _deduplicate_symbols(conn, exchange: str, symbols: pd.Series) -> None:
    """Collapse same-symbol/date duplicates to one row.

    Reused-ticker history is discarded before renames, so this only resolves
    duplicate rows inside one batch.  Prefer the more liquid row so the
    compact store remains deterministic.
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


def upsert_daily_prices(
    conn,
    rows: pd.DataFrame,
    retain_candles: int | None = RETAIN_CANDLES,
) -> None:
    """Upsert one exchange's daily rows, optionally applying an explicit cap."""
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
        _discard_reused_ticker_history(conn)
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
        if retain_candles is not None:
            _trim_symbols(conn, exchange, incoming["Symbol"], retain_candles)
    finally:
        conn.unregister("incoming_prices")


def replace_symbol_prices(
    conn,
    exchange: str,
    symbol: str,
    frame: pd.DataFrame,
    retain_candles: int | None = RETAIN_CANDLES,
) -> None:
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
        if retain_candles is not None:
            _trim_symbols(
                conn,
                exchange,
                pd.Series([normalized_symbol]),
                retain_candles,
            )
    finally:
        conn.unregister("replacement_prices")
