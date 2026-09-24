"""NSE delivery-report maintenance helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd


def update_pending_delivery_data(
    nse: Any,
    date_str: str,
    daily_bhavcopy_dir: Optional[Path],
    logger: Any,
    tz_in: Any,
) -> bool:
    """Apply a late delivery report only to the consolidated daily bhavcopy.

    Returns ``True`` when the report is applied or when retrying should stop;
    returns ``False`` when the report should remain pending.
    """

    dt = datetime.fromisoformat(date_str)
    days_since_failure = (datetime.now(tz_in) - dt).days
    dt = dt.replace(tzinfo=None)
    report_file = None

    try:
        report_file = nse.deliveryBhavcopy(dt)
    except Exception:
        if days_since_failure >= 5:
            logger.warning(
                f"Max attempts reached: Aborting future attempts for report dated {dt}"
            )
            return True
        logger.info(f"{dt:%d %b}: Delivery report not yet updated.")
        return False

    try:
        if daily_bhavcopy_dir is None:
            report_file.unlink()
            return True
        daily_file = daily_bhavcopy_dir / f"{dt:%Y%m%d}_BENNY_NSE_EOD.txt"
        if not daily_file.exists():
            logger.warning(
                f"Consolidated daily bhavcopy missing for pending delivery date {dt:%Y-%m-%d}"
            )
            report_file.unlink()
            return True

        delivery = pd.read_csv(report_file)
        delivery.columns = delivery.columns.str.strip()
        delivery = delivery[
            delivery["SERIES"].str.strip().isin(("EQ", "BE", "BZ", "SM", "ST"))
        ].copy()
        delivery["SYMBOL"] = delivery["SYMBOL"].astype(str).str.strip().str.upper()
        # NSE writes " -" where a figure is not published; read it as missing
        for column in ("NO_OF_TRADES", "DELIV_QTY"):
            if column in delivery.columns:
                delivery[column] = pd.to_numeric(delivery[column], errors="coerce")
        delivery = delivery.set_index("SYMBOL")

        daily = pd.read_csv(daily_file)
        for index, row in daily.iterrows():
            symbol = str(row["Symbol"]).strip().upper()
            lookup = symbol.removesuffix("_SME")
            if lookup not in delivery.index:
                continue
            report_row = delivery.loc[lookup]
            if isinstance(report_row, pd.DataFrame):
                report_row = report_row.iloc[0]
            trades = report_row["NO_OF_TRADES"]
            series = str(report_row["SERIES"]).strip()
            delivered_quantity = (
                row["Volume"] if series in ("BE", "BZ") else report_row["DELIV_QTY"]
            )
            avg_trades = (
                float("nan") if pd.isna(trades)
                else round(float(row["Volume"]) / float(trades), 2)
            )
            daily.loc[index, ["TOTAL_TRADES", "QTY_PER_TRADE", "DLV_QTY"]] = [
                trades,
                avg_trades,
                delivered_quantity,
            ]
        temporary = daily_file.with_suffix(daily_file.suffix + ".tmp")
        daily.to_csv(temporary, index=False)
        temporary.replace(daily_file)

        report_file.unlink()
        return True
    except Exception as error:
        logger.error(f"Error updating pending delivery data for {date_str}: {error}")
        if report_file and report_file.exists():
            report_file.unlink()
        return False
