"""BSE stock EOD database maintenance helpers using DuckDB."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import pandas as pd
import duckdb
import config


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

    # Sort to keep processing order stable
    df_merged = df_merged.sort_values(by="TckrSymb")

    for _, row in df_merged.iterrows():
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

        # Check for Symbol Rename using ISIN database
        if isin_code not in isin.index:
            isin_updated = True
            isin.at[isin_code, "SYMBOL"] = symbol

        if symbol != isin.at[isin_code, "SYMBOL"]:
            isin_updated = True
            old = isin.at[isin_code, "SYMBOL"].upper()
            new = symbol.upper()
            isin.at[isin_code, "SYMBOL"] = symbol
            
            # Execute rename query in DuckDB
            conn = duckdb.connect(str(config.DUCKDB_FILE))
            try:
                conn.execute(
                    "UPDATE eod_data SET Symbol = ? WHERE ISIN = ? AND Exchange = 'BSE'",
                    (f"{new}{prefix.upper()}", isin_code)
                )
            finally:
                conn.close()
            logger.warning(f"BSE Name Changed in DuckDB: {old}{prefix.upper()} to {new}{prefix.upper()}")

        # Prepare parameters to append
        trades = row.get("TOTAL_TRADES", "")
        if pd.isna(trades) or trades == "":
            trades = ""
        else:
            trades = int(trades)

        delivered = row.get("DLV_QTY", "")
        if pd.isna(delivered) or delivered == "":
            delivered = ""
        else:
            delivered = int(delivered)

        update_bse_symbol(
            symbol_name,
            isin_code,
            series,
            row.OpnPric,
            row.HghPric,
            row.LwPric,
            row.ClsPric,
            row.TtlTradgVol,
            trades,
            delivered,
            dates,
        )

    if isin_updated:
        isin.to_csv(isin_file)
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
    dates: Any,
) -> None:
    """Insert one stock row into DuckDB database, avoiding duplicate dates."""
    conn = duckdb.connect(str(config.DUCKDB_FILE))
    
    try:
        date_val = pd.to_datetime(dates.pandasDt).date()
        
        open_p = float(open_price) if not pd.isna(open_price) and open_price != "" else None
        high_p = float(high_price) if not pd.isna(high_price) and high_price != "" else None
        low_p = float(low_price) if not pd.isna(low_price) and low_price != "" else None
        close_p = float(close_price) if not pd.isna(close_price) and close_price != "" else None
        vol = int(volume) if not pd.isna(volume) and volume != "" else 0
        
        total_trades = int(trades) if not pd.isna(trades) and trades != "" else None
        qty_per_trade = round(vol / total_trades, 2) if total_trades and total_trades > 0 else None
        dlv_qty = int(delivered) if not pd.isna(delivered) and delivered != "" else None
        
        conn.execute("""
            INSERT OR IGNORE INTO eod_data 
            (Date, Symbol, Exchange, ISIN, Open, High, Low, Close, Volume, Series, Total_Trades, Qty_Per_Trade, Dlv_Qty)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date_val, symbol, 'BSE', isin_code, open_p, high_p, low_p, close_p, vol, series, total_trades, qty_per_trade, dlv_qty
        ))
    finally:
        conn.close()
