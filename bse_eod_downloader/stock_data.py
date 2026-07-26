"""BSE stock EOD database maintenance helpers using DuckDB."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional
import pandas as pd
import duckdb
import config
from eod_store import upsert_daily_prices


def update_bse_eod(
    df_merged: pd.DataFrame,
    data_dir: Path,
    logger: logging.Logger,
    metadata_dir: Path,
    dates: Any,
) -> None:
    """Process the merged daily BSE DataFrame and insert data into DuckDB database."""
    isin_file = metadata_dir / "bse_isin.csv"
    if isin_file.is_file():
        isin = pd.read_csv(isin_file, index_col="ISIN")
    else:
        isin = pd.DataFrame(columns=["ISIN", "SYMBOL"]).set_index("ISIN")

    isin_updated = False

    # Convert isin DataFrame to dictionary for O(1) high-speed lookup and modification
    isin_dict = isin["SYMBOL"].to_dict()

    # Parse date exactly once for the entire day's run to avoid slow Pandas parse in the loop
    date_val = pd.to_datetime(dates.pandasDt).date()

    # Open a single DuckDB connection for this day's updates and run in a transaction
    conn = duckdb.connect(str(config.DUCKDB_FILE))
    conn.execute("BEGIN TRANSACTION")
    try:
        insert_rows = []
        # Use high-speed itertuples instead of iterrows
        for row in df_merged.itertuples():
            symbol = str(row.TckrSymb).strip()
            if not symbol or pd.isna(row.TckrSymb):
                continue

            isin_code = row.ISIN
            series = str(row.SctySrs).strip()

            # BSE SME groups are M and MT
            prefix = "_sme" if series in ("M", "MT") else ""
            symbol_name = f"{symbol.upper()}{prefix.upper()}"

            if pd.isna(isin_code):
                logger.warning(f"BSE Symbol {symbol} missing ISIN.")
                continue

            # Check for Symbol Rename using high-speed dictionary
            existing_symbol = isin_dict.get(isin_code)
            if existing_symbol is None:
                isin_updated = True
                isin_dict[isin_code] = symbol
                existing_symbol = symbol

            if symbol != existing_symbol:
                isin_updated = True
                old = existing_symbol.upper()
                new = symbol.upper()
                isin_dict[isin_code] = symbol
                
                # ``upsert_daily_prices`` moves the retained history using
                # the stable ISIN before replacing the symbol metadata.
                logger.warning(f"BSE Name Changed in DuckDB: {old}{prefix.upper()} to {new}{prefix.upper()}")

            # Prepare parameters to append
            def is_nan(val):
                return val is None or val == "" or (isinstance(val, float) and val != val)

            trades = getattr(row, "TOTAL_TRADES", "")
            if is_nan(trades):
                trades = ""
            else:
                trades = int(trades)

            delivered = getattr(row, "DLV_QTY", "")
            if is_nan(delivered):
                delivered = ""
            else:
                delivered = int(delivered)

            # Collect parameters for bulk insertion
            open_p = float(row.OpnPric) if not is_nan(row.OpnPric) else None
            high_p = float(row.HghPric) if not is_nan(row.HghPric) else None
            low_p = float(row.LwPric) if not is_nan(row.LwPric) else None
            close_p = float(row.ClsPric) if not is_nan(row.ClsPric) else None
            vol = int(row.TtlTradgVol) if not is_nan(row.TtlTradgVol) else 0
            
            total_trades = int(trades) if not is_nan(trades) else None
            qty_per_trade = round(vol / total_trades, 2) if total_trades and total_trades > 0 else None
            dlv_qty = int(delivered) if not is_nan(delivered) else None

            insert_rows.append((
                date_val, symbol_name, 'BSE', isin_code, open_p, high_p, low_p, close_p, vol, series, total_trades, qty_per_trade, dlv_qty
            ))

        if insert_rows:
            # Create a DataFrame from the collected rows and insert it in a single vectorized SQL query
            df_insert = pd.DataFrame(insert_rows, columns=[
                'Date', 'Symbol', 'Exchange', 'ISIN', 'Open', 'High', 'Low', 'Close', 'Volume', 'Series', 'Total_Trades', 'Qty_Per_Trade', 'Dlv_Qty'
            ])
            upsert_daily_prices(conn, df_insert)

        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        raise e
    finally:
        conn.close()

    if isin_updated:
        # Convert dictionary back to DataFrame and save
        isin_new = pd.DataFrame.from_dict(isin_dict, orient='index', columns=['SYMBOL'])
        isin_new.index.name = 'ISIN'
        isin_new.to_csv(isin_file)

    logger.info("BSE EOD sync complete")


def update_bse_symbol(
    symbol: str,
    isin_code: str,
    series: str,
    open_price: Any,
    high_price: Any,
    low_price: Any,
    close_price: Any,
    volume: Any,
    trades: Any,
    delivered: Any,
    date_val: Any,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> None:
    """Insert one stock row into DuckDB database, avoiding duplicate dates. (Kept for compatibility)"""
    should_close = False
    if conn is None:
        conn = duckdb.connect(str(config.DUCKDB_FILE))
        should_close = True
    
    try:
        def is_nan(val):
            return val is None or val == "" or (isinstance(val, float) and val != val)
        
        open_p = float(open_price) if not is_nan(open_price) else None
        high_p = float(high_price) if not is_nan(high_price) else None
        low_p = float(low_price) if not is_nan(low_price) else None
        close_p = float(close_price) if not is_nan(close_price) else None
        vol = int(volume) if not is_nan(volume) else 0
        
        total_trades = int(trades) if not is_nan(trades) else None
        qty_per_trade = round(vol / total_trades, 2) if total_trades and total_trades > 0 else None
        dlv_qty = int(delivered) if not is_nan(delivered) else None
        
        upsert_daily_prices(conn, pd.DataFrame([{
            "Date": date_val, "Symbol": symbol, "Exchange": "BSE", "ISIN": isin_code,
            "Open": open_p, "High": high_p, "Low": low_p, "Close": close_p,
            "Volume": vol, "Series": series,
        }]))
    finally:
        if should_close:
            conn.close()
