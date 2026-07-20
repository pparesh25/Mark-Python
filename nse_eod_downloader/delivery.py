"""NSE delivery-report maintenance helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


def update_pending_delivery_data(
    nse: Any,
    date_str: str,
    data_dir: Path,
    logger: Any,
    tz_in: Any,
) -> bool:
    """Apply a previously unavailable delivery report to a daily file.

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
        delivery = pd.read_csv(report_file, index_col="SYMBOL")
        delivery_folder = data_dir / "nseDelivery" / str(dt.year)
        delivery_folder.mkdir(parents=True, exist_ok=True)
        delivery.to_csv(delivery_folder / report_file.name)

        delivery = delivery[
            delivery[" SERIES"].isin((" EQ", " BE", " BZ", " SM", " ST"))
        ]

        for symbol in delivery.index:
            daily_file = data_dir / f"{symbol.lower()}.csv"
            if not daily_file.exists():
                daily_file = data_dir / f"{symbol.lower()}_sme.csv"
                if not daily_file.exists():
                    continue

            daily = pd.read_csv(daily_file, index_col="Date", parse_dates=["Date"])
            if dt not in daily.index:
                continue

            volume = daily.loc[dt, "Volume"]
            trades, delivered_quantity = delivery.loc[
                symbol, [" NO_OF_TRADES", " DELIV_QTY"]
            ]
            series = delivery.loc[symbol, " SERIES"].strip()
            delivered_quantity = (
                volume if series in ("BE", "BZ") else int(delivered_quantity)
            )
            avg_trades = (
                "" if trades == "" or pd.isna(trades) else round(volume / trades, 2)
            )
            daily.loc[dt, ["TOTAL_TRADES", "QTY_PER_TRADE", "DLV_QTY"]] = [
                trades,
                avg_trades,
                delivered_quantity,
            ]
            daily.to_csv(daily_file)

        report_file.unlink()
        return True
    except Exception as error:
        logger.error(f"Error updating pending delivery data for {date_str}: {error}")
        if report_file and report_file.exists():
            report_file.unlink()
        return False

