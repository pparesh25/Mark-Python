"""DuckDB price store: raw prices as published, the exchanges' actions, and the adjusted prices screened.

Tables (schema version 2):

    securities         Exchange, Symbol, ISIN, Series, Code   one row a symbol: today's ISIN and series, BSE scrip code
    raw_prices         Date, Symbol, Exchange, Open, High, Low, Close, Volume, Total_Trades, Dlv_Qty
                       exactly as the exchange published the day -- never adjusted in place
    exchange_actions   the exchanges' corporate-action lists, as fetched (action_lists.py)
    price_adjustments  every factor decided and why (price_adjustments.py)
    adjustment_periods Exchange, Symbol, From_Date, Adj: the product of a symbol's later factors, valid
                       from From_Date to the next period -- rewritten by price_adjustments.rebuild
    eod_prices         VIEW: raw_prices x the period's Adj (prices multiplied, volume and delivery
                       divided), so a day just synced is there at once and nothing can go stale
    eod_data           VIEW the screener reads, the same 13 columns as before

Why (2026-10): version 1 divided the stored prices in place when an action
came, with no record on NSE's side of what had been applied, so a replayed day
divided the history again (POCL, three times) and a missed action was never
caught up. Now the raw prices never change and the adjusted ones are computed
from them on every run: an action cannot be applied twice, and a corrected
list corrects the prices on the next run.

A version 1 file (no raw_prices) is not upgraded in place -- its prices are
already adjusted, some wrongly; rebuild_eod_database.py makes a version 2 file
from the exchanges' own files and keeps the old one as a backup.
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
PRICE_COLUMNS = ["Date", "Symbol", "Exchange", "Open", "High", "Low", "Close", "Volume"]
RAW_COLUMNS = PRICE_COLUMNS + ["Total_Trades", "Dlv_Qty"]
# Indian ISINs are ``IN`` + issuer type + 4-character issuer code + security
# type + serial + check digit.  A face-value split or consolidation keeps the
# first seven characters and only changes the serial.
ISIN_ISSUER_PREFIX_LENGTH = 7


def schema_version(conn) -> int:
    """2 for this layout, 1 for the old in-place-adjusted store, 0 for an empty file."""
    tables = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if "raw_prices" in tables:
        return 2
    return 1 if "eod_prices" in tables else 0


def create_schema(conn) -> None:
    """Create the version 2 tables and the screener's ``eod_data`` view (on an empty or version 2 file)."""
    if schema_version(conn) == 1:
        raise RuntimeError("This is a version 1 price store; run rebuild_eod_database.py to replace it.")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS securities (
            Exchange VARCHAR NOT NULL,
            Symbol VARCHAR NOT NULL,
            ISIN VARCHAR NOT NULL,
            Series VARCHAR,
            Code VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_prices (
            Date DATE NOT NULL,
            Symbol VARCHAR NOT NULL,
            Exchange VARCHAR NOT NULL,
            Open DOUBLE, High DOUBLE, Low DOUBLE, Close DOUBLE,
            Volume BIGINT, Total_Trades BIGINT, Dlv_Qty BIGINT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adjustment_periods (
            Exchange VARCHAR NOT NULL,
            Symbol VARCHAR NOT NULL,
            From_Date DATE NOT NULL,
            Adj DOUBLE NOT NULL
        )
        """
    )
    # ASOF picks, for each raw row, the symbol's latest period starting on or before the day;
    # a symbol without a factor has no period and is read as it is.
    conn.execute(
        """
        CREATE OR REPLACE VIEW eod_prices AS
        SELECT r.Date, r.Symbol, r.Exchange,
               round(r.Open * COALESCE(a.Adj, 1.0), 4) AS Open,
               round(r.High * COALESCE(a.Adj, 1.0), 4) AS High,
               round(r.Low * COALESCE(a.Adj, 1.0), 4) AS Low,
               round(r.Close * COALESCE(a.Adj, 1.0), 4) AS Close,
               CAST(round(r.Volume / COALESCE(a.Adj, 1.0)) AS BIGINT) AS Volume,
               r.Total_Trades,
               CAST(round(r.Dlv_Qty / COALESCE(a.Adj, 1.0)) AS BIGINT) AS Dlv_Qty
        FROM raw_prices r
        ASOF LEFT JOIN adjustment_periods a
          ON a.Exchange = r.Exchange AND a.Symbol = r.Symbol AND r.Date >= a.From_Date
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exchange_actions (
            Exchange VARCHAR NOT NULL,
            Segment VARCHAR,
            Symbol VARCHAR,
            Code VARCHAR,
            ISIN VARCHAR,
            Company VARCHAR,
            Series VARCHAR,
            Ex_Date DATE,
            Purpose VARCHAR,
            Kind VARCHAR,
            Factor DOUBLE,
            Fetched_At TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_adjustments (
            Exchange VARCHAR, Symbol VARCHAR, Date DATE, Gap DOUBLE, Factor DOUBLE,
            Event VARCHAR, Flag VARCHAR, Listed_Kind VARCHAR, Listed_Factor DOUBLE
        )
        """
    )
    conn.execute(
        """
        CREATE OR REPLACE VIEW eod_data AS
        SELECT p.Date, p.Symbol, p.Exchange, s.ISIN,
               p.Open, p.High, p.Low, p.Close, p.Volume, s.Series,
               CAST(p.Total_Trades AS INTEGER) AS Total_Trades,
               CAST(p.Volume AS DOUBLE) / NULLIF(p.Total_Trades, 0) AS Qty_Per_Trade,
               p.Dlv_Qty
        FROM eod_prices p
        JOIN securities s
          ON s.Exchange = p.Exchange AND s.Symbol = p.Symbol
        """
    )


# The old name, kept for callers of the compact store.
create_compact_schema = create_schema


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
        JOIN raw_prices p ON p.Exchange = t.Exchange AND p.Symbol = t.Symbol
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
            DELETE FROM raw_prices
            WHERE rowid IN (
                SELECT p.rowid
                FROM raw_prices p
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
    """Collapse same-symbol/date duplicates to one row, the more liquid one."""
    symbol_frame = pd.DataFrame({"Symbol": symbols.dropna().astype(str).unique()})
    if symbol_frame.empty:
        return
    conn.register("deduplicate_symbols", symbol_frame)
    try:
        conn.execute(
            """
            DELETE FROM raw_prices
            WHERE rowid IN (
                SELECT rowid
                FROM (
                    SELECT p.rowid,
                           ROW_NUMBER() OVER (
                               PARTITION BY p.Symbol, p.Date
                               ORDER BY COALESCE(p.Volume, 0) DESC, p.rowid DESC
                           ) AS duplicate_rank
                    FROM raw_prices p
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


def upsert_daily_prices(conn, rows: pd.DataFrame) -> None:
    """Store one exchange's day as published: renames follow the ISIN, a re-sent day replaces its rows.

    Replacing a day is safe now: the row is the exchange's own figure, and the
    adjusted prices are recomputed from it, so a replay can neither undo an
    adjustment nor apply one twice.
    """
    required = set(PRICE_COLUMNS + ["ISIN", "Series"])
    missing = required.difference(rows.columns)
    if missing:
        raise ValueError(f"Price rows missing columns: {sorted(missing)}")

    incoming = rows.copy()
    for column in ("Total_Trades", "Dlv_Qty", "Code"):
        if column not in incoming.columns:
            incoming[column] = None
    incoming = incoming.loc[:, PRICE_COLUMNS + ["Total_Trades", "Dlv_Qty", "ISIN", "Series", "Code"]]
    incoming = incoming.dropna(subset=["Date", "Symbol", "Exchange", "ISIN"])
    if incoming.empty:
        return
    incoming["Symbol"] = incoming["Symbol"].astype(str).str.strip().str.upper()
    incoming["Exchange"] = incoming["Exchange"].astype(str).str.strip().str.upper()
    incoming["ISIN"] = incoming["ISIN"].astype(str).str.strip().str.upper()
    incoming["Series"] = incoming["Series"].astype(str).str.strip()
    incoming["Code"] = incoming["Code"].map(lambda c: None if c is None or pd.isna(c) else str(c).strip())
    incoming["Date"] = pd.to_datetime(incoming["Date"]).dt.date
    for column in ("Total_Trades", "Dlv_Qty"):
        incoming[column] = pd.to_numeric(incoming[column], errors="coerce").astype("Int64")

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
            UPDATE raw_prices p
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
                   Series = i.Series,
                   Code = COALESCE(i.Code, s.Code)
              FROM (
                    SELECT DISTINCT Exchange, ISIN, Symbol, Series, Code
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
            INSERT INTO securities (Exchange, Symbol, ISIN, Series, Code)
            SELECT i.Exchange, i.Symbol, i.ISIN, i.Series, i.Code
            FROM (
                SELECT DISTINCT Exchange, ISIN, Symbol, Series, Code
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
            DELETE FROM raw_prices p
             USING incoming_prices i, securities s
             WHERE s.Exchange = i.Exchange AND s.ISIN = i.ISIN
               AND p.Date = i.Date AND p.Symbol = s.Symbol AND p.Exchange = i.Exchange
            """
        )
        conn.execute(
            """
            INSERT INTO raw_prices (Date, Symbol, Exchange, Open, High, Low, Close, Volume, Total_Trades, Dlv_Qty)
            SELECT i.Date, s.Symbol, i.Exchange, i.Open, i.High, i.Low, i.Close, i.Volume, i.Total_Trades, i.Dlv_Qty
            FROM incoming_prices i
            JOIN securities s
              ON s.Exchange = i.Exchange AND s.ISIN = i.ISIN
            """
        )
        _deduplicate_symbols(conn, exchange, incoming["Symbol"])
    finally:
        conn.unregister("incoming_prices")


def apply_retention(conn, years: float) -> int:
    """Drop raw rows older than `years` before the store's last day; returns the rows dropped."""
    last = conn.execute("SELECT max(Date) FROM raw_prices").fetchone()[0]
    if last is None:
        return 0
    cutoff = pd.Timestamp(last) - pd.Timedelta(days=round(365.25 * years))
    dropped = conn.execute("SELECT count(*) FROM raw_prices WHERE Date < ?", [cutoff.date()]).fetchone()[0]
    if dropped:
        conn.execute("DELETE FROM raw_prices WHERE Date < ?", [cutoff.date()])
    return int(dropped)
