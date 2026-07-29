"""NSE index bhavcopy and daily index-file maintenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pandas as pd


def update_index_eod(
    file: Path,
    _data_dir: Path,
    dates: Any,
    logger: Any,
    bhavcopy_rows: Optional[list[str]] = None,
) -> None:
    """Append normalized index rows to the consolidated daily bhavcopy."""

    frame = pd.read_csv(file, index_col="Index Name")
    columns = [
        "Open Index Value", "High Index Value", "Low Index Value",
        "Closing Index Value", "Volume", "P/E",
    ]
    for column in columns:
        frame.loc[:, column] = pd.to_numeric(frame[column], errors="coerce")

    for symbol in frame.index:
        values = frame.loc[symbol, columns]
        if bhavcopy_rows is not None:
            clean_symbol = symbol.replace("/", "-").replace(":", "-").upper()
            bhavcopy_rows.append(
                f"{clean_symbol},{dates.pandasDt},{values.iloc[0]},{values.iloc[1]},{values.iloc[2]},{values.iloc[3]},{values.iloc[4]},{values.iloc[5]},,,\n"
            )
    logger.info("Index sync complete.")
