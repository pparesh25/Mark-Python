"""BSE stock EOD CSV file maintenance helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import pandas as pd

HEADER = b"Date,Open,High,Low,Close,Volume,Series,TOTAL_TRADES,QTY_PER_TRADE,DLV_QTY\n"


def update_bse_eod(
    df_merged: pd.DataFrame,
    data_dir: Path,
    logger: logging.Logger,
    metadata_dir: Path,
    dates: Any,
) -> None:
    """Process the merged daily BSE DataFrame and append data to individual stock CSVs."""
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
        symbol_file = data_dir / f"{symbol.lower()}{prefix}.csv"

        if pd.isna(isin_code):
            logger.warning(f"BSE Symbol {symbol} missing ISIN.")
            continue

        # Check for Symbol Rename using ISIN database
        if isin_code not in isin.index:
            isin_updated = True
            isin.at[isin_code, "SYMBOL"] = symbol

        if symbol != isin.at[isin_code, "SYMBOL"]:
            isin_updated = True
            old = isin.at[isin_code, "SYMBOL"].lower()
            new = symbol.lower()
            isin.at[isin_code, "SYMBOL"] = symbol
            
            symbol_file = data_dir / f"{new}{prefix}.csv"
            old_file = data_dir / f"{old}{prefix}.csv"
            try:
                old_file.rename(symbol_file)
                logger.warning(f"BSE Name Changed: Renamed {old}{prefix}.csv to {new}{prefix}.csv")
            except FileNotFoundError:
                logger.warning(f"BSE Name Changed: {old} to {new}. No historical CSV file to rename.")

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
            symbol_file,
            series,
            row.OpnPric,
            row.HghPric,
            row.LwPric,
            row.ClsPric,
            row.TtlTradgVol,
            trades,
            delivered,
            data_dir,
            dates,
        )

    if isin_updated:
        isin.to_csv(isin_file)
    logger.info("BSE EOD sync complete")


def update_bse_symbol(
    symbol_file: Path,
    series: Any,
    open_price: Any,
    high_price: Any,
    low_price: Any,
    close_price: Any,
    volume: Any,
    trades: Any,
    delivered: Any,
    data_dir: Path,
    dates: Any,
) -> None:
    """Append one daily record to a stock's CSV file, avoiding duplicate dates."""
    text = b""
    if not symbol_file.exists():
        text += HEADER
    else:
        with symbol_file.open("rb") as handle:
            try:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - 200))
                if dates.pandasDt.encode() in handle.read():
                    return  # Already updated for this date
            except Exception:
                pass

    # Round price fields to 2 decimals
    open_p = f"{float(open_price):.2f}" if not pd.isna(open_price) else ""
    high_p = f"{float(high_price):.2f}" if not pd.isna(high_price) else ""
    low_p = f"{float(low_price):.2f}" if not pd.isna(low_price) else ""
    close_p = f"{float(close_price):.2f}" if not pd.isna(close_price) else ""
    vol = f"{int(volume)}" if not pd.isna(volume) else "0"

    total_trades = f"{int(trades)}" if trades != "" else ""
    
    # Calculate QTY_PER_TRADE
    if trades != "" and int(trades) > 0:
        qty_per_trade = f"{(int(volume) / int(trades)):.2f}"
    else:
        qty_per_trade = ""

    dlv_qty = f"{int(delivered)}" if delivered != "" else ""

    row_text = f"{dates.pandasDt},{open_p},{high_p},{low_p},{close_p},{vol},{series},{total_trades},{qty_per_trade},{dlv_qty}\n"
    text += row_text.encode()

    with symbol_file.open("ab") as handle:
        handle.write(text)
