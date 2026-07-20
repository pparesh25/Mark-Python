"""NSE index bhavcopy and daily index-file maintenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pandas as pd


INDEX_HEADER = b"Date,Open,High,Low,Close,Volume,P/E,Series,TOTAL_TRADES,QTY_PER_TRADE,DLV_QTY\n"


def update_index_eod(
    file: Path,
    data_dir: Path,
    dates: Any,
    logger: Any,
    bhavcopy_rows: Optional[list[str]] = None,
) -> None:
    """Persist one index report and append its daily index values."""

    folder = data_dir / "nseIndices" / str(dates.dt.year)
    folder.mkdir(parents=True, exist_ok=True)
    frame = pd.read_csv(file, index_col="Index Name")
    frame.to_csv(folder / file.name)
    columns = [
        "Open Index Value", "High Index Value", "Low Index Value",
        "Closing Index Value", "Volume", "P/E",
    ]
    for column in columns:
        frame.loc[:, column] = pd.to_numeric(frame[column], errors="coerce")

    for symbol in frame.index:
        values = frame.loc[symbol, columns]
        update_indice(symbol, *values, data_dir, dates)
        if bhavcopy_rows is not None:
            clean_symbol = symbol.replace("/", "-").replace(":", "-").upper()
            bhavcopy_rows.append(
                f"{clean_symbol},{dates.pandasDt},{values.iloc[0]},{values.iloc[1]},{values.iloc[2]},{values.iloc[3]},{values.iloc[4]},{values.iloc[5]},,,\n"
            )
    logger.info("Index sync complete.")


def update_indice(
    symbol: str,
    open_price: Any,
    high_price: Any,
    low_price: Any,
    close_price: Any,
    volume: Any,
    pe: Any,
    daily_folder: Path,
    dates: Any,
) -> None:
    """Append one index row, avoiding duplicate dates."""

    symbol = symbol.replace("/", "-").replace(":", "-")
    file = daily_folder / f"{symbol.lower()}.csv"
    text = b""
    if not file.is_file():
        text += INDEX_HEADER
    else:
        with file.open("rb") as handle:
            try:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - 200))
                if dates.pandasDt.encode() in handle.read():
                    return
            except Exception:
                pass

    text += bytes(
        f"{dates.pandasDt},{open_price},{high_price},{low_price},{close_price},{volume},{pe},,,,\n",
        encoding="utf-8",
    )
    with file.open("ab") as handle:
        handle.write(text)

