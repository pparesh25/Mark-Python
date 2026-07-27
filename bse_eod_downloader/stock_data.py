"""BSE stock EOD database maintenance helpers using DuckDB."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional
import pandas as pd
import duckdb
import config
from eod_store import upsert_daily_prices

TEXT_COLUMNS = [
    "Date", "Open", "High", "Low", "Close", "Volume", "Series",
    "TOTAL_TRADES", "QTY_PER_TRADE", "DLV_QTY",
]


def _symbol_text_path(data_dir: Path, symbol: str) -> Path:
    safe_name = "".join(
        character.lower()
        if character.isalnum() or character in "._-"
        else "_"
        for character in symbol.strip().upper()
    )
    return data_dir / f"{safe_name}.csv"


def _write_text_frame(path: Path, frame: pd.DataFrame) -> None:
    frame = frame.copy()
    for column in TEXT_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.date
    frame = (
        frame.loc[:, TEXT_COLUMNS]
        .drop_duplicates(subset=["Date"], keep="last")
        .sort_values("Date")
    )
    temporary = path.with_suffix(".csv.tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)


def _merge_renamed_text_file(data_dir: Path, old_symbol: str, new_symbol: str) -> None:
    old_path = _symbol_text_path(data_dir, old_symbol)
    new_path = _symbol_text_path(data_dir, new_symbol)
    if old_path == new_path or not old_path.exists():
        return
    old_frame = pd.read_csv(old_path)
    if new_path.exists():
        old_frame = pd.concat([old_frame, pd.read_csv(new_path)], ignore_index=True)
    # Match DuckDB's rename-collision rule: when both tickers contain the same
    # date, retain the row with the greater traded volume.
    old_frame["_volume_rank"] = pd.to_numeric(
        old_frame.get("Volume"), errors="coerce"
    ).fillna(0)
    old_frame = old_frame.sort_values(
        ["Date", "_volume_rank"], kind="stable"
    ).drop(columns=["_volume_rank"])
    _write_text_frame(new_path, old_frame)
    old_path.unlink()


def _upsert_text_prices(
    data_dir: Path,
    rows: pd.DataFrame,
    renames: list[tuple[str, str]],
) -> None:
    """Keep the per-symbol BSE text backup aligned with compact DuckDB."""
    data_dir.mkdir(parents=True, exist_ok=True)
    for old_symbol, new_symbol in renames:
        _merge_renamed_text_file(data_dir, old_symbol, new_symbol)

    for row in rows.to_dict(orient="records"):
        path = _symbol_text_path(data_dir, row["Symbol"])
        incoming = pd.DataFrame([{
            "Date": row["Date"],
            "Open": row["Open"],
            "High": row["High"],
            "Low": row["Low"],
            "Close": row["Close"],
            "Volume": row["Volume"],
            "Series": row["Series"],
            "TOTAL_TRADES": row["Total_Trades"],
            "QTY_PER_TRADE": row["Qty_Per_Trade"],
            "DLV_QTY": row["Dlv_Qty"],
        }])
        if path.exists():
            incoming = pd.concat(
                [pd.read_csv(path), incoming],
                ignore_index=True,
            )
        _write_text_frame(path, incoming)


def sync_adjusted_text_prices(
    data_dir: Path,
    symbol: str,
    adjusted_prices: pd.DataFrame,
) -> None:
    """Idempotently copy adjusted OHLCV values from DuckDB into one text file."""
    path = _symbol_text_path(data_dir, symbol)
    adjusted = adjusted_prices.copy()
    adjusted["Date"] = pd.to_datetime(adjusted["Date"]).dt.date
    adjusted = adjusted.set_index("Date")

    if path.exists():
        text = pd.read_csv(path)
        text["Date"] = pd.to_datetime(text["Date"]).dt.date
        text = text.set_index("Date")
    else:
        text = pd.DataFrame(index=adjusted.index)

    for column in ("Open", "High", "Low", "Close", "Volume"):
        text.loc[adjusted.index, column] = adjusted[column]
    if "Series" in adjusted.columns:
        text.loc[adjusted.index, "Series"] = adjusted["Series"]
    text.index.name = "Date"
    _write_text_frame(path, text.reset_index())


def update_bse_eod(
    df_merged: pd.DataFrame,
    data_dir: Path,
    logger: logging.Logger,
    metadata_dir: Path,
    dates: Any,
) -> None:
    """Update the dedicated DuckDB and its complete BSE text backup."""
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

    # BSE occasionally publishes two security codes/tickers for the same ISIN
    # on one day (for example ``BAJAJ-AUTO#`` and ``BAJAJ-AUTO``).  A stable
    # ISIN must map to one daily row. Prefer a normal ticker over a temporary
    # ``#`` alias, then the most liquid row.
    daily_rows = df_merged.copy()
    daily_rows["_clean_isin"] = daily_rows["ISIN"].astype(str).str.strip()
    daily_rows["_clean_symbol"] = daily_rows["TckrSymb"].astype(str).str.strip()
    daily_rows["_temporary_alias"] = daily_rows["_clean_symbol"].str.endswith("#")
    daily_rows["_numeric_volume"] = pd.to_numeric(
        daily_rows["TtlTradgVol"], errors="coerce"
    ).fillna(0)
    valid_isin = (
        daily_rows["ISIN"].notna()
        & daily_rows["_clean_isin"].ne("")
        & daily_rows["_clean_isin"].str.upper().ne("NAN")
    )
    duplicate_isins = int(
        daily_rows.loc[valid_isin, "_clean_isin"].duplicated(keep=False).sum()
    )
    if duplicate_isins:
        logger.warning(
            f"BSE {dates.pandasDt}: resolving {duplicate_isins} duplicate "
            f"same-ISIN rows by primary ticker and volume"
        )
    daily_rows = (
        daily_rows.sort_values(
            ["_temporary_alias", "_numeric_volume"],
            ascending=[True, False],
            kind="stable",
        )
        .drop_duplicates(subset=["_clean_isin"], keep="first")
        .drop(
            columns=[
                "_clean_isin",
                "_clean_symbol",
                "_temporary_alias",
                "_numeric_volume",
            ]
        )
    )

    # Open a single DuckDB connection for this day's updates and run in a transaction
    conn = duckdb.connect(str(config.DUCKDB_FILE))
    conn.execute("BEGIN TRANSACTION")
    df_insert = pd.DataFrame()
    renames: list[tuple[str, str]] = []
    try:
        insert_rows = []
        # Use high-speed itertuples instead of iterrows
        for row in daily_rows.itertuples():
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
                renames.append((
                    f"{old}{prefix.upper()}",
                    f"{new}{prefix.upper()}",
                ))

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

    if not df_insert.empty:
        _upsert_text_prices(data_dir, df_insert, renames)

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
