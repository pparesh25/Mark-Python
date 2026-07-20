"""Capital-market daily-file and ISIN/symbol maintenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd


STOCK_SERIES = ("EQ", "BE", "BZ", "SM", "ST")
HEADER = b"Date,Open,High,Low,Close,Volume,Series,TOTAL_TRADES,QTY_PER_TRADE,DLV_QTY\n"


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
    """Persist one NSE equity bhavcopy and update daily stock files."""

    logger.info("Starting Capital Market Data Sync")
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
        symbol_file = data_dir / f"{row.TckrSymb.lower()}{prefix}.csv"

        if pd.isna(row.Index):
            raise ValueError(f"{row.TckrSymb} missing ISIN number.")

        if row.Index not in isin.index:
            isin_updated = True
            isin.at[row.Index, "SYMBOL"] = row.TckrSymb

        if row.TckrSymb != isin.at[row.Index, "SYMBOL"]:
            isin_updated = True
            old = isin.at[row.Index, "SYMBOL"].lower()
            new = row.TckrSymb.lower()
            isin.at[row.Index, "SYMBOL"] = row.TckrSymb
            symbol_file = data_dir / f"{new}{prefix}.csv"
            old_file = data_dir / f"{old}{prefix}.csv"
            try:
                old_file.rename(symbol_file)
            except FileNotFoundError:
                logger.warning(f"Renaming daily/{old}.csv to {new}.csv. No such file.")
            logger.warning(f"Name Changed: {old} to {new}")

        update_nse_symbol(
            symbol_file,
            row.SctySrs,
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

        if bhavcopy_rows is not None:
            symbol = f"{row.TckrSymb.upper()}{prefix.upper()}"
            avg_trades = "" if trades == "" or pd.isna(trades) else round(row.TtlTradgVol / trades, 2)
            bhavcopy_rows.append(
                f"{symbol},{dates.pandasDt},{row.OpnPric},{row.HghPric},{row.LwPric},{row.ClsPric},{row.TtlTradgVol},{row.SctySrs},{trades},{avg_trades},{delivered}\n"
            )

    if isin_updated:
        isin.to_csv(isin_file)
    logger.info("EOD sync complete")


def update_nse_symbol(
    symbol_file: Path,
    series: Any,
    open_price: Any,
    high_price: Any,
    low_price: Any,
    close_price: Any,
    volume: Any,
    trades: Any,
    delivered: Any,
    daily_folder: Path,
    dates: Any,
) -> None:
    """Append one stock row, avoiding duplicate dates."""

    text = b""
    if not symbol_file.exists():
        sme_file = daily_folder / f"{symbol_file.stem}_sme.csv"
        if "_sme" not in symbol_file.name and sme_file.exists():
            sme_file.rename(symbol_file)
        else:
            text += HEADER
    else:
        with symbol_file.open("rb") as handle:
            try:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - 200))
                if dates.pandasDt.encode() in handle.read():
                    return
            except Exception:
                pass

    avg_trades = "" if trades == "" or pd.isna(trades) else round(volume / trades, 2)
    text += bytes(
        f"{dates.pandasDt},{open_price},{high_price},{low_price},{close_price},{volume},{series},{trades},{avg_trades},{delivered}\n",
        encoding="utf-8",
    )
    with symbol_file.open("ab") as handle:
        handle.write(text)

