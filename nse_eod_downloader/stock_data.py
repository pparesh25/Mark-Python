"""Capital-market daily-file and ISIN/symbol maintenance using DuckDB."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import duckdb
import config

STOCK_SERIES = ("EQ", "BE", "BZ", "SM", "ST")


def update_nse_eod(
    bhav_file: Path,
    delivery_file: Optional[Path],
    data_dir: Path,
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
    bhav_folder = data_dir / "nseBhav" / str(dates.dt.year)
    bhav_folder.mkdir(parents=True, exist_ok=True)
    frame.to_csv(bhav_folder / bhav_file.name)
    frame = frame.loc[frame["SctySrs"].isin(STOCK_SERIES)]

    if delivery_file:
        delivery = pd.read_csv(delivery_file, index_col="SYMBOL")
        delivery_folder = data_dir / "nseDelivery" / str(dates.dt.year)
        delivery_folder.mkdir(parents=True, exist_ok=True)
        delivery.to_csv(delivery_folder / delivery_file.name)
        delivery = delivery.loc[delivery[" SERIES"].isin(tuple(f" {s}" for s in STOCK_SERIES))]
    else:
        delivery = None

    for row in frame.itertuples():
        if "-RE" in row.TckrSymb:
            continue

        if delivery is not None:
            if row.TckrSymb in delivery.index:
                trades, delivered = delivery.loc[row.TckrSymb, [" NO_OF_TRADES", " DELIV_QTY"]]
                delivered = row.TtlTradgVol if row.SctySrs in ("BE", "BZ") else int(delivered)
            else:
                trades = delivered = np.nan
        else:
            trades = delivered = ""

        prefix = "_sme" if row.SctySrs in ("SM", "ST") else ""
        symbol = f"{row.TckrSymb.upper()}{prefix.upper()}"

        if pd.isna(row.Index):
            raise ValueError(f"{row.TckrSymb} missing ISIN number.")

        if row.Index not in isin.index:
            isin_updated = True
            isin.at[row.Index, "SYMBOL"] = row.TckrSymb

        if row.TckrSymb != isin.at[row.Index, "SYMBOL"]:
            isin_updated = True
            old = isin.at[row.Index, "SYMBOL"].upper()
            new = row.TckrSymb.upper()
            isin.at[row.Index, "SYMBOL"] = row.TckrSymb
            
            # Execute rename query in DuckDB
            conn = duckdb.connect(str(config.DUCKDB_FILE))
            try:
                conn.execute(
                    "UPDATE eod_data SET Symbol = ? WHERE ISIN = ? AND Exchange = 'NSE'",
                    (new, row.Index)
                )
            finally:
                conn.close()
            logger.warning(f"Name Changed in DuckDB: {old} to {new}")

        update_nse_symbol(
            symbol,
            row.Index, # ISIN
            row.SctySrs,
            row.OpnPric,
            row.HghPric,
            row.LwPric,
            row.ClsPric,
            row.TtlTradgVol,
            trades,
            delivered,
            dates,
        )

        if bhavcopy_rows is not None:
            avg_trades = "" if trades == "" or pd.isna(trades) else round(row.TtlTradgVol / trades, 2)
            bhavcopy_rows.append(
                f"{symbol},{dates.pandasDt},{row.OpnPric},{row.HghPric},{row.LwPric},{row.ClsPric},{row.TtlTradgVol},{row.SctySrs},{trades},{avg_trades},{delivered}\n"
            )

    if isin_updated:
        isin.to_csv(isin_file)
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
            date_val, symbol, 'NSE', isin_code, open_p, high_p, low_p, close_p, vol, series, total_trades, qty_per_trade, dlv_qty
        ))
    finally:
        conn.close()
