"""Capital-market daily-file and ISIN/symbol maintenance using DuckDB."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import duckdb
import config
from eod_store import upsert_daily_prices

STOCK_SERIES = ("EQ", "BE", "BZ", "SM", "ST")


def update_nse_eod(
    bhav_file: Path,
    delivery_file: Optional[Path],
    _data_dir: Path,
    metadata_dir: Path,
    isin: pd.DataFrame,
    dates: Any,
    logger: Any,
    bhavcopy_rows: Optional[list[str]] = None,
) -> None:
    """Persist one NSE equity bhavcopy and update DuckDB database."""

    logger.info("Starting Capital Market Data Sync (DuckDB)")
    isin_file = metadata_dir / "isin.csv"
    isin_updated = False

    frame = pd.read_csv(bhav_file, index_col="ISIN")
    frame = frame.loc[frame["SctySrs"].isin(STOCK_SERIES)]

    # Pre-build delivery lookup dictionary to avoid slow Pandas lookups in loop
    delivery_dict = {}
    if delivery_file:
        delivery_df = pd.read_csv(delivery_file)
        # Clean column names by stripping spaces
        delivery_df.columns = delivery_df.columns.str.strip()
        # NSE answers a request for a day without a session with ANOTHER day's
        # file (sec_bhavdata_full_27092026, a Sunday, held 2026-09-26's rows; and
        # 2019-09-30 held 2019-06-27's): its delivery must not land on this day.
        if "DATE1" in delivery_df.columns:
            inside = set(pd.to_datetime(delivery_df["DATE1"].astype(str).str.strip(), format="%d-%b-%Y",
                                        errors="coerce").dt.date.dropna())
            if inside and inside != {pd.to_datetime(dates.pandasDt).date()}:
                logger.warning(f"NSE delivery file for {dates.pandasDt} holds {sorted(inside)[:2]}; not used.")
                delivery_df = delivery_df.iloc[0:0]
        delivery_df = delivery_df[delivery_df["SERIES"].str.strip().isin(STOCK_SERIES)]
        # NSE writes " -" where a figure is not published; read it as missing
        for column in ("NO_OF_TRADES", "DELIV_QTY"):
            if column in delivery_df.columns:
                delivery_df[column] = pd.to_numeric(delivery_df[column], errors="coerce")
        for r in delivery_df.itertuples():
            sym = str(r.SYMBOL).strip().upper()
            trades_val = getattr(r, "NO_OF_TRADES", np.nan)
            deliv_val = getattr(r, "DELIV_QTY", np.nan)
            delivery_dict[sym] = (trades_val, deliv_val)

    # Convert isin DataFrame to dictionary for O(1) high-speed lookup and modification
    isin_dict = isin["SYMBOL"].to_dict()

    # Parse date exactly once for the entire day's run to avoid slow Pandas parse in the loop
    date_val = pd.to_datetime(dates.pandasDt).date()

    # Open a single DuckDB connection for this day's updates and run in a transaction
    conn = duckdb.connect(str(config.DUCKDB_FILE))
    conn.execute("BEGIN TRANSACTION")
    try:
        insert_rows = []
        for row in frame.itertuples():
            if "-RE" in row.TckrSymb:
                continue

            # High-speed delivery lookup
            trades_deliv = delivery_dict.get(row.TckrSymb.upper())
            if trades_deliv is not None:
                raw_trades, raw_delivered = trades_deliv
                trades = raw_trades
                delivered = row.TtlTradgVol if row.SctySrs in ("BE", "BZ") else raw_delivered
            else:
                trades = delivered = np.nan

            prefix = "_sme" if row.SctySrs in ("SM", "ST") else ""
            symbol = f"{row.TckrSymb.upper()}{prefix.upper()}"

            if pd.isna(row.Index):
                raise ValueError(f"{row.TckrSymb} missing ISIN number.")

            # High-speed ISIN rename and mapping checks
            existing_symbol = isin_dict.get(row.Index)
            if existing_symbol is None:
                isin_updated = True
                isin_dict[row.Index] = row.TckrSymb
                existing_symbol = row.TckrSymb

            if row.TckrSymb != existing_symbol:
                isin_updated = True
                old = existing_symbol.upper()
                new = row.TckrSymb.upper()
                isin_dict[row.Index] = row.TckrSymb
                
                # ``upsert_daily_prices`` moves the retained history using
                # the stable ISIN before replacing the symbol metadata.
                logger.warning(f"Name Changed in DuckDB: {old} to {new}")

            # Collect parameters to append
            def is_nan(val):
                return val is None or val == "" or (isinstance(val, float) and val != val)
            
            open_p = float(row.OpnPric) if not is_nan(row.OpnPric) else None
            high_p = float(row.HghPric) if not is_nan(row.HghPric) else None
            low_p = float(row.LwPric) if not is_nan(row.LwPric) else None
            close_p = float(row.ClsPric) if not is_nan(row.ClsPric) else None
            vol = int(row.TtlTradgVol) if not is_nan(row.TtlTradgVol) else 0
            
            total_trades = int(trades) if not is_nan(trades) else None
            qty_per_trade = round(vol / total_trades, 2) if total_trades and total_trades > 0 else None
            dlv_qty = int(delivered) if not is_nan(delivered) else None

            insert_rows.append((
                date_val, symbol, 'NSE', row.Index, open_p, high_p, low_p, close_p, vol, row.SctySrs, total_trades, qty_per_trade, dlv_qty
            ))

            if bhavcopy_rows is not None:
                avg_trades = "" if trades == "" or pd.isna(trades) else round(row.TtlTradgVol / trades, 2)
                # A coerced column is float; the text copy keeps whole numbers
                trades_text = trades if total_trades is None else total_trades
                delivered_text = delivered if dlv_qty is None else dlv_qty
                bhavcopy_rows.append(
                    f"{symbol},{dates.pandasDt},{row.OpnPric},{row.HghPric},{row.LwPric},{row.ClsPric},{row.TtlTradgVol},{row.SctySrs},{trades_text},{avg_trades},{delivered_text}\n"
                )

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

    logger.info("EOD sync complete")


def update_nse_symbol(
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
            "Date": date_val, "Symbol": symbol, "Exchange": "NSE", "ISIN": isin_code,
            "Open": open_p, "High": high_p, "Low": low_p, "Close": close_p,
            "Volume": vol, "Series": series,
        }]))
    finally:
        if should_close:
            conn.close()
