"""DuckDB price store: raw prices as published, the exchanges' actions, and the adjusted prices screened.

Tables (schema version 3):

    securities          Exchange, Symbol, ISIN, Series, Code   one row a symbol: today's ISIN and series,
                        BSE scrip code
    raw_prices          Date, Symbol, Exchange, Open, High, Low, Close, Volume, Total_Trades, Dlv_Qty
                        exactly as the exchange published the day -- never adjusted in place; every other
                        price table is derived from it, and checked and repaired against it
    exchange_actions    the exchanges' corporate-action lists, as fetched (action_lists.py)
    action_list_history Exchange, From_Date, To_Date, Fetched_On: the last time each exchange's whole
                        retained list was fetched (asked again after 30 days)
    price_adjustments   every decision and flag and why (price_adjustments.py), read by the review list
    adjustment_ledger   Exchange, Symbol, Date, Factor, Event, Event_Key, Gap, Listed_Kind, Listed_Factor,
                        Decided_At, Rule_Version: the factors applied, one a symbol-day (primary key; a
                        factor must be finite and above 0); Event_Key names the source event
    adjustment_periods  Exchange, Symbol, From_Date, Adj: the product of a symbol's later ledger factors,
                        valid from From_Date to the next period
    gap_history         Exchange, Symbol, Date, Gap: every sure gap seen, kept when its raw row ages out,
                        so the gap-noise rule counts over the whole listing
    eod_prices          TABLE: raw_prices x the period's Adj (prices multiplied and rounded to 4 places,
                        volume and delivery divided) -- what version 2 computed in a view, stored
    symbol_state        Exchange, Symbol, Rows, First_Date, Last_Date, Digest, Inputs_Digest,
                        Pruned_Before: what the last maintenance decided each symbol from
    maintenance_state   Key, Value: rule and DuckDB versions, stored range, last full verify
    eod_data            VIEW the screener reads, the same 13 columns as before

Why (2026-10): version 1 divided the stored prices in place when an action
came, with no record on NSE's side of what had been applied, so a replayed day
divided the history again (POCL, three times) and a missed action was never
caught up. Version 2 kept the raw prices and recomputed every adjusted price on
every run (12-24 s). Version 3 keeps its principle -- an adjusted price is always
raw x the product of the symbol's ledger factors after that day, never derived
from another adjusted price -- but stores the result, and the maintenance
rewrites only the symbols whose decisions can have changed
(adjustment_ledger.py). A replay can neither undo an adjustment nor apply one
twice, and a weekly full check compares everything with a full rebuild.

The store keeps itself consistent between maintenance runs: an upsert writes
the adjusted rows of the day it stores (with the symbol's current factors), and
a rename moves the symbol's derived rows with its raw rows; a ticker handed to
another issuer loses them with its raw rows.

A version 1 file (no raw_prices) is not upgraded in place -- its prices are
already adjusted, some wrongly; rebuild_eod_database.py makes a new file from
the exchanges' own files and keeps the old one as a backup. A version 2 file
is migrated to version 3 by its first maintenance run (adjustment_ledger.migrate),
which keeps a copy beside it. A store with an `eod_prices` table and no
raw_prices (the market-data lake's export, screened with --skip-sync) is read
as it is and never written.
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3
PRICE_COLUMNS = ["Date", "Symbol", "Exchange", "Open", "High", "Low", "Close", "Volume"]
RAW_COLUMNS = PRICE_COLUMNS + ["Total_Trades", "Dlv_Qty"]
# The version 3 tables derived from raw_prices that hold rows per (Exchange, Symbol): they follow a
# rename and are dropped with a reused ticker's history.
DERIVED_TABLES = ("eod_prices", "adjustment_ledger", "adjustment_periods", "price_adjustments", "gap_history")
# Indian ISINs are ``IN`` + issuer type + 4-character issuer code + security
# type + serial + check digit.  A face-value split or consolidation keeps the
# first seven characters and only changes the serial.
ISIN_ISSUER_PREFIX_LENGTH = 7


def decision_hash(alias: str = "") -> str:
    """SQL hash of a raw row's day, open and close -- all a decision reads; symbol_state.Digest sums it."""
    return f"hash({alias}Date, {alias}Open, {alias}Close)"


def row_hash(alias: str = "") -> str:
    """SQL hash of every column of a raw row the store keeps; symbol_state.Row_Digest sums it, so any change
    to a stored raw row (a late delivery figure, a corrected volume) rewrites that symbol's adjusted rows."""
    columns = ", ".join(f"{alias}{c}" for c in ("Date", "Open", "High", "Low", "Close", "Volume", "Total_Trades",
                                                 "Dlv_Qty"))
    return f"hash({columns})"


def relations(conn) -> dict:
    """{table or view name: 'BASE TABLE' | 'VIEW'} of the main schema."""
    return dict(conn.execute(
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_catalog = current_database()").fetchall())


def schema_version(conn) -> int:
    """3: adjusted prices stored with a ledger; 2: views over raw prices; 1: the old in-place-adjusted store
    (or an external eod_prices table without raw prices); 0: an empty file."""
    found = relations(conn)
    if "raw_prices" in found:
        stored = found.get("eod_prices") == "BASE TABLE" and "adjustment_ledger" in found
        return 3 if stored else 2
    return 1 if "eod_prices" in found else 0


def eod_select(source: str = "raw_prices", periods: str = "adjustment_periods") -> str:
    """The adjusted rows of `source` (raw_prices or a subquery of it): the version 2 view's own text.

    ASOF picks, for each raw row, the symbol's latest period starting on or
    before the day; a symbol without a factor has no period and is read as it
    is. Version 3 stores exactly this, so eod_data keeps the same values, types
    and rounding and released engines read it unchanged. `periods` may name
    another table of periods (the full verify's).
    """
    return f"""
        SELECT r.Date, r.Symbol, r.Exchange,
               round(r.Open * COALESCE(a.Adj, 1.0), 4) AS Open,
               round(r.High * COALESCE(a.Adj, 1.0), 4) AS High,
               round(r.Low * COALESCE(a.Adj, 1.0), 4) AS Low,
               round(r.Close * COALESCE(a.Adj, 1.0), 4) AS Close,
               CAST(round(r.Volume / COALESCE(a.Adj, 1.0)) AS BIGINT) AS Volume,
               r.Total_Trades,
               CAST(round(r.Dlv_Qty / COALESCE(a.Adj, 1.0)) AS BIGINT) AS Dlv_Qty
        FROM {source} r
        ASOF LEFT JOIN {periods} a
          ON a.Exchange = r.Exchange AND a.Symbol = r.Symbol AND r.Date >= a.From_Date
        """


EOD_DATA_VIEW = """
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


def create_schema(conn, version: int | None = None) -> None:
    """Create the tables and the screener's ``eod_data`` view.

    An empty file becomes `version` (default SCHEMA_VERSION); an existing
    version 2 or 3 file keeps its version unless `version` is 3 (the migration,
    which then fills eod_prices). A version 1 file raises.
    """
    current = schema_version(conn)
    if current == 1:
        raise RuntimeError("This is a version 1 price store; run rebuild_eod_database.py to replace it.")
    target = version or current or SCHEMA_VERSION
    if current == 3 and target != 3:
        raise RuntimeError("A version 3 price store cannot be turned back into version 2.")
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
        CREATE TABLE IF NOT EXISTS action_list_history (
            Exchange VARCHAR NOT NULL,
            From_Date DATE,
            To_Date DATE,
            Fetched_On DATE
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
    if target == 2:
        conn.execute(f"CREATE OR REPLACE VIEW eod_prices AS {eod_select()}")
        conn.execute(EOD_DATA_VIEW)
        return
    _create_version_3(conn)


def _create_version_3(conn) -> None:
    """The version 3 tables; a version 2 file's eod_prices view gives way to an (empty) table."""
    if relations(conn).get("eod_prices") == "VIEW":
        conn.execute("DROP VIEW IF EXISTS eod_data")
        conn.execute("DROP VIEW eod_prices")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eod_prices (
            Date DATE NOT NULL,
            Symbol VARCHAR NOT NULL,
            Exchange VARCHAR NOT NULL,
            Open DOUBLE, High DOUBLE, Low DOUBLE, Close DOUBLE,
            Volume BIGINT, Total_Trades BIGINT, Dlv_Qty BIGINT
        )
        """
    )
    # One applied factor a symbol-day; a factor of 0, below 0, NaN or infinite cannot be stored.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adjustment_ledger (
            Exchange VARCHAR NOT NULL,
            Symbol VARCHAR NOT NULL,
            Date DATE NOT NULL,
            Factor DOUBLE NOT NULL CHECK (Factor > 0 AND isfinite(Factor)),
            Event VARCHAR NOT NULL,
            Event_Key VARCHAR NOT NULL,
            Gap DOUBLE,
            Listed_Kind VARCHAR,
            Listed_Factor DOUBLE,
            Decided_At TIMESTAMP,
            Rule_Version INTEGER,
            PRIMARY KEY (Exchange, Symbol, Date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gap_history (
            Exchange VARCHAR NOT NULL,
            Symbol VARCHAR NOT NULL,
            Date DATE NOT NULL,
            Gap DOUBLE,
            PRIMARY KEY (Exchange, Symbol, Date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_state (
            Exchange VARCHAR NOT NULL,
            Symbol VARCHAR NOT NULL,
            Rows BIGINT,
            First_Date DATE,
            Last_Date DATE,
            Digest HUGEINT,
            Inputs_Digest VARCHAR,
            Pruned_Before DATE,
            Row_Digest HUGEINT
        )
        """
    )
    # No primary key: nearly every row changes every day, and DuckDB does not give back the space of
    # deleted rows in a table with an index (20 daily runs left 196,000 dead rows, 12 MB). The rows are
    # written only from a GROUP BY (Exchange, Symbol).
    _drop_state_key(conn)
    conn.execute("ALTER TABLE symbol_state ADD COLUMN IF NOT EXISTS Row_Digest HUGEINT")    # a store from 6d39495
    conn.execute("CREATE TABLE IF NOT EXISTS maintenance_state (Key VARCHAR PRIMARY KEY, Value VARCHAR)")
    conn.execute(EOD_DATA_VIEW)


def _drop_state_key(conn) -> None:
    """Recreate a symbol_state made with a primary key (stores from 6d39495) without it."""
    keyed = conn.execute("SELECT count(*) FROM duckdb_constraints() WHERE table_name = 'symbol_state' "
                         "AND constraint_type = 'PRIMARY KEY' AND database_name = current_database()").fetchone()[0]
    if keyed:
        conn.execute("CREATE TABLE symbol_state_new AS SELECT * FROM symbol_state")
        conn.execute("DROP TABLE symbol_state")
        conn.execute("ALTER TABLE symbol_state_new RENAME TO symbol_state")


STATE_COLUMNS = ["Exchange", "Symbol", "Rows", "First_Date", "Last_Date", "Digest", "Row_Digest", "Inputs_Digest",
                 "Pruned_Before"]


# The old name, kept for callers of the compact store.
create_compact_schema = create_schema

_SCOPES = {
    # scope columns -> (how raw/eod rows match the scope row k)
    ("Exchange", "Symbol"): "k.Exchange = {t}.Exchange AND k.Symbol = {t}.Symbol",
    ("Exchange", "Symbol", "After"): ("k.Exchange = {t}.Exchange AND k.Symbol = {t}.Symbol "
                                      "AND (k.After IS NULL OR {t}.Date > k.After)"),
    ("Exchange", "Symbol", "Date"): "k.Exchange = {t}.Exchange AND k.Symbol = {t}.Symbol AND k.Date = {t}.Date",
}


def rematerialize(conn, scope: str | None = None, columns: tuple = ("Exchange", "Symbol")) -> int:
    """Bring eod_prices rows in step with raw_prices and adjustment_periods (version 3).

    `scope` names a registered frame or temp table with `columns`: (Exchange,
    Symbol) covers every row of those symbols, (Exchange, Symbol, After) their
    rows after After (all when NULL), (Exchange, Symbol, Date) those keys; None
    fills an empty table (or replaces every row). Within a scope only the rows
    that differ from raw x periods are deleted and written again, and rows
    raw_prices no longer holds are deleted: a row already right is never
    rewritten, so a day's rows are written once (DuckDB keeps a rewritten row's
    old space until it reuses it, and the file grew by each day's rows twice).
    """
    if scope is None:
        # In day order, as raw_prices is: retention then deletes whole row groups at the head, which
        # DuckDB gives back. The ASOF join's own (symbol) order spread every day's deletes over all row
        # groups, none of which ever emptied -- the file grew by about a session's rows a day.
        conn.execute("DELETE FROM eod_prices")
        conn.execute(f"INSERT INTO eod_prices SELECT * FROM ({eod_select()}) ORDER BY Date, Exchange, Symbol")
        return conn.execute("SELECT count(*) FROM eod_prices").fetchone()[0]
    match = _SCOPES[tuple(columns)]
    source = f"(SELECT p.* FROM raw_prices p JOIN {scope} k ON {match.format(t='p')})"
    conn.execute(f"CREATE OR REPLACE TEMP TABLE wanted_rows AS {eod_select(source)}")
    conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE stale_keys AS
        SELECT DISTINCT Exchange, Symbol, Date FROM (
            (SELECT e.* FROM eod_prices e JOIN {scope} k ON {match.format(t='e')} EXCEPT ALL SELECT * FROM wanted_rows)
            UNION ALL
            (SELECT * FROM wanted_rows EXCEPT ALL SELECT e.* FROM eod_prices e JOIN {scope} k ON {match.format(t='e')}))
        """)
    stale = conn.execute("SELECT count(*) FROM stale_keys").fetchone()[0]
    if stale:
        conn.execute("DELETE FROM eod_prices e USING stale_keys s "
                     "WHERE e.Exchange = s.Exchange AND e.Symbol = s.Symbol AND e.Date = s.Date")
        conn.execute("INSERT INTO eod_prices SELECT w.* FROM wanted_rows w JOIN stale_keys s "
                     "ON w.Exchange = s.Exchange AND w.Symbol = s.Symbol AND w.Date = s.Date "
                     "ORDER BY w.Date, w.Exchange, w.Symbol")
    conn.execute("DROP TABLE wanted_rows")
    conn.execute("DROP TABLE stale_keys")
    return stale


def delete_derived(conn, targets: str) -> None:
    """Remove every derived row of the (Exchange, Symbol) rows of `targets` (version 3)."""
    for table in DERIVED_TABLES + ("symbol_state",):
        conn.execute(f"DELETE FROM {table} t USING {targets} k WHERE t.Exchange = k.Exchange AND t.Symbol = k.Symbol")


def _discard_reused_ticker_history(conn, stored: bool) -> None:
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
    In a version 3 store the previous owner's factors, adjusted rows, gap
    history and state go with its prices (`stored`).
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
        if stored:
            delete_derived(conn, "reused_tickers")
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


_RENAMES = """
    (SELECT DISTINCT i.Exchange, s.Symbol AS Old_Symbol, i.Symbol AS New_Symbol
       FROM (SELECT DISTINCT Exchange, ISIN, Symbol FROM incoming_prices) i
       JOIN securities s ON s.Exchange = i.Exchange AND s.ISIN = i.ISIN
      WHERE s.Symbol <> i.Symbol)
"""


def upsert_daily_prices(conn, rows: pd.DataFrame) -> None:
    """Store one exchange's day as published: renames follow the ISIN, a re-sent day replaces its rows.

    Replacing a day is safe: the row is the exchange's own figure, and the
    adjusted prices are derived from it, so a replay can neither undo an
    adjustment nor apply one twice. In a version 3 store the derived rows of a
    renamed symbol move with it, and the adjusted rows of the stored days are
    written at once with the symbol's current factors; the maintenance then
    decides again every symbol whose decisions this can change.
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
    stored = schema_version(conn) == 3

    conn.register("incoming_prices", incoming)
    try:
        _discard_reused_ticker_history(conn, stored)
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
        if stored and conn.execute(f"SELECT count(*) FROM {_RENAMES}").fetchone()[0]:
            # The factors, adjusted rows and gap history are the same series under its new name. The
            # state of both names is dropped, so the maintenance decides them again (an NSE list matched
            # by name, an override written for one of the names).
            for table in DERIVED_TABLES:
                conn.execute(f"UPDATE {table} t SET Symbol = r.New_Symbol FROM {_RENAMES} r "
                             "WHERE t.Exchange = r.Exchange AND t.Symbol = r.Old_Symbol")
            conn.execute(f"DELETE FROM symbol_state t USING {_RENAMES} r WHERE t.Exchange = r.Exchange "
                         "AND (t.Symbol = r.Old_Symbol OR t.Symbol = r.New_Symbol)")
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
        if stored:
            conn.execute(
                """
                CREATE OR REPLACE TEMP TABLE upserted_days AS
                SELECT DISTINCT i.Exchange, s.Symbol, CAST(i.Date AS DATE) AS Date
                FROM incoming_prices i JOIN securities s ON s.Exchange = i.Exchange AND s.ISIN = i.ISIN
                """
            )
            rematerialize(conn, "upserted_days", ("Exchange", "Symbol", "Date"))
            conn.execute("DROP TABLE upserted_days")
    finally:
        conn.unregister("incoming_prices")


def apply_retention(conn, years: float) -> int:
    """Drop raw rows older than `years` before the store's last day; returns the rows dropped.

    In a version 3 store the adjusted rows go with them, and each symbol's
    state forgets the dropped rows and remembers the cutoff (Pruned_Before):
    the maintenance then decides again only the symbols whose decisions near
    the new first day can change (adjustment_ledger.py). The gap history and
    the ledger keep their rows until then. All of it is one transaction (the
    caller's, when one is open): a crash between the state update and the
    deletes made every touched symbol look rewritten.
    """
    last = conn.execute("SELECT max(Date) FROM raw_prices").fetchone()[0]
    if last is None:
        return 0
    cutoff = (pd.Timestamp(last) - pd.Timedelta(days=round(365.25 * years))).date()
    dropped = conn.execute("SELECT count(*) FROM raw_prices WHERE Date < ?", [cutoff]).fetchone()[0]
    if not dropped:
        return 0
    with transaction(conn):
        if schema_version(conn) == 3:
            forget_rows_before(conn, cutoff)
            conn.execute("DELETE FROM eod_prices WHERE Date < ?", [cutoff])
        conn.execute("DELETE FROM raw_prices WHERE Date < ?", [cutoff])
    return int(dropped)


def in_transaction(conn) -> bool:
    """True inside an explicit transaction: there txid_current() stays the same from one statement to the next.

    (A BEGIN to find out would not do: in DuckDB a nested BEGIN fails and aborts the caller's transaction.)
    """
    first = conn.execute("SELECT txid_current()").fetchone()[0]
    return conn.execute("SELECT txid_current()").fetchone()[0] == first


@contextmanager
def transaction(conn):
    """BEGIN ... COMMIT around the block (ROLLBACK on an error); inside the caller's open transaction just the
    block, which commits or rolls back with it."""
    if in_transaction(conn):
        yield
        return
    conn.execute("BEGIN TRANSACTION")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def forget_rows_before(conn, cutoff, source: str = "raw_prices") -> None:
    """Take the rows of `source` before `cutoff` out of symbol_state and mark the symbols Pruned_Before.

    Called before those rows are deleted (apply_retention) or left out of a copy
    (compact_price_database): the next maintenance then sees the remaining rows as
    unchanged and checks only what the shorter history can change.
    """
    conn.execute(
        f"""
        UPDATE symbol_state s
           SET Rows = s.Rows - d.Rows, Digest = s.Digest - d.Digest, Row_Digest = s.Row_Digest - d.Row_Digest,
               Pruned_Before = greatest(COALESCE(s.Pruned_Before, ?::DATE), ?::DATE)
          FROM (SELECT p.Exchange, p.Symbol, count(*) AS Rows, sum({decision_hash('p.')}) AS Digest,
                       sum({row_hash('p.')}) AS Row_Digest
                  FROM {source} p JOIN symbol_state k ON k.Exchange = p.Exchange AND k.Symbol = p.Symbol
                 WHERE p.Date < ? AND p.Date <= k.Last_Date
                 GROUP BY p.Exchange, p.Symbol) d
         WHERE s.Exchange = d.Exchange AND s.Symbol = d.Symbol
        """,
        [cutoff, cutoff, cutoff],
    )


def main(argv: list[str] | None = None) -> int:
    """python -m eod_store --downgrade-to-v2 [PATH]: make a version 3 store usable by code before version 3."""
    parser = argparse.ArgumentParser(prog="python -m eod_store", description=main.__doc__)
    parser.add_argument("--downgrade-to-v2", nargs="?", const="", metavar="PATH", default=None,
                        help="the store to downgrade (default: the data root's NSE_BSE_EOD.duckdb)")
    parser.add_argument("--data-root", type=Path, help="override the default ~/Mark EMA and HTF data root")
    args = parser.parse_args(argv)
    if args.downgrade_to_v2 is None:
        parser.print_help()
        return 2
    import adjustment_ledger
    import config
    import eod_maintenance

    if args.data_root:
        config.configure_data_root(args.data_root)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("downgrade")
    path = Path(args.downgrade_to_v2) if args.downgrade_to_v2 else config.DUCKDB_FILE
    try:
        ok = adjustment_ledger.downgrade_to_v2(path, eod_maintenance.overrides_file(), eod_maintenance.review_file(),
                                               logger=log)
    except Exception as error:  # noqa: BLE001 - reported as the exit code, with the file's true state
        version = eod_maintenance._version_of(path)
        if version == "2":
            log.warning(f"The downgrade reported {error}, but {path} is a version 2 store now.")
            return 0
        log.error(f"Downgrade failed ({error}); {path} is still version {version}.")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
