"""NSE delivery-report maintenance helpers.

A day whose delivery report (sec_bhavdata_full) was not published at sync time
is stored without trades and delivered quantity and kept in
meta["DLV_PENDING_DATES"]. When the report comes, its figures are written into
the stored raw prices of that NSE day (and into the optional daily text copy);
the date leaves the pending list only once the price store took them.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import duckdb
import pandas as pd

import config

STOCK_SERIES = ("EQ", "BE", "BZ", "SM", "ST")
SME_SERIES = ("SM", "ST")
TRADE_FOR_TRADE = ("BE", "BZ")          # every trade is delivered: delivered quantity = volume


def read_delivery_report(report_file: Path, day: date, logger: Any) -> pd.DataFrame:
    """(Symbol as stored, Series, Total_Trades, Dlv_Qty) of the stock series in one report.

    NSE writes " -" where a figure is not published (read as missing), and may
    answer for a day without a session with another day's file: such a file is
    not used (an empty frame comes back).
    """
    delivery = pd.read_csv(report_file)
    delivery.columns = delivery.columns.str.strip()
    if "DATE1" in delivery.columns:
        inside = set(pd.to_datetime(delivery["DATE1"].astype(str).str.strip(), format="%d-%b-%Y",
                                    errors="coerce").dt.date.dropna())
        if inside and inside != {day}:
            logger.warning(f"NSE delivery file for {day} holds {sorted(inside)[:2]}; not used.")
            delivery = delivery.iloc[0:0]
    delivery["SERIES"] = delivery["SERIES"].astype(str).str.strip()
    delivery = delivery[delivery["SERIES"].isin(STOCK_SERIES)].copy()
    for column in ("NO_OF_TRADES", "DELIV_QTY"):
        delivery[column] = (pd.to_numeric(delivery[column], errors="coerce") if column in delivery.columns
                            else float("nan"))
    symbol = delivery["SYMBOL"].astype(str).str.strip().str.upper()
    # The store keeps NSE's SME series under SYMBOL_SME (stock_data.update_nse_eod).
    symbol = symbol.where(~delivery["SERIES"].isin(SME_SERIES), symbol + "_SME")
    out = pd.DataFrame({"Symbol": symbol, "Series": delivery["SERIES"],
                        "Total_Trades": delivery["NO_OF_TRADES"], "Dlv_Qty": delivery["DELIV_QTY"]})
    # A symbol listed twice keeps its last row, as the sync's own lookup does.
    return out.drop_duplicates("Symbol", keep="last").reset_index(drop=True)


def store_delivery(report: pd.DataFrame, day: date, database: Path | None = None) -> int:
    """Write the report's trades and delivered quantity into raw_prices for NSE `day`; returns the rows updated.

    Rows already stored for that day are updated in one transaction, nothing is
    added. BE/BZ (trade-for-trade) rows take their stored volume as delivered.
    """
    frame = report.assign(Total_Trades=pd.to_numeric(report["Total_Trades"], errors="coerce").round().astype("Int64"),
                          Dlv_Qty=pd.to_numeric(report["Dlv_Qty"], errors="coerce").round().astype("Int64"),
                          _whole=report["Series"].isin(TRADE_FOR_TRADE))
    conn = duckdb.connect(str(database or config.DUCKDB_FILE))
    conn.register("late_delivery", frame[["Symbol", "Total_Trades", "Dlv_Qty", "_whole"]])
    try:
        conn.execute("BEGIN TRANSACTION")
        try:
            matched = conn.execute(
                "SELECT count(*) FROM raw_prices r JOIN late_delivery d ON r.Symbol = d.Symbol "
                "WHERE r.Exchange = 'NSE' AND r.Date = ?", [day]).fetchone()[0]
            conn.execute(
                """
                UPDATE raw_prices r
                   SET Total_Trades = d.Total_Trades,
                       Dlv_Qty = CASE WHEN d._whole THEN r.Volume ELSE d.Dlv_Qty END
                  FROM late_delivery d
                 WHERE r.Exchange = 'NSE' AND r.Date = ? AND r.Symbol = d.Symbol
                """,
                [day],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.unregister("late_delivery")
        conn.close()
    return int(matched)


def _update_daily_text(daily_file: Path, report: pd.DataFrame) -> None:
    """The optional consolidated daily copy, with the late figures."""
    by_symbol = report.set_index("Symbol")
    daily = pd.read_csv(daily_file)
    for index, row in daily.iterrows():
        symbol = str(row["Symbol"]).strip().upper()
        if symbol not in by_symbol.index:
            continue
        found = by_symbol.loc[symbol]
        trades = found["Total_Trades"]
        delivered = row["Volume"] if found["Series"] in TRADE_FOR_TRADE else found["Dlv_Qty"]
        avg_trades = float("nan") if pd.isna(trades) or not trades else round(float(row["Volume"]) / float(trades), 2)
        daily.loc[index, ["TOTAL_TRADES", "QTY_PER_TRADE", "DLV_QTY"]] = [trades, avg_trades, delivered]
    temporary = daily_file.with_suffix(daily_file.suffix + ".tmp")
    daily.to_csv(temporary, index=False)
    temporary.replace(daily_file)


def update_pending_delivery_data(
    nse: Any,
    date_str: str,
    daily_bhavcopy_dir: Optional[Path],
    logger: Any,
    tz_in: Any,
    database: Optional[Path] = None,
) -> bool:
    """Apply a late delivery report to the stored raw prices (and the daily text copy, when kept).

    Returns ``True`` when the date can leave the pending list: the price store
    took the figures, or NSE has not published the report for 5 days (it is
    given up, and the rows stay without delivery). Returns ``False`` to keep it
    pending: not published yet, or the store could not be updated.
    """
    logger = logger or logging.getLogger(__name__)
    dt = datetime.fromisoformat(date_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz_in)
    days_since_failure = (datetime.now(tz_in) - dt).days
    dt = dt.replace(tzinfo=None)
    report_file = None

    try:
        report_file = nse.deliveryBhavcopy(dt)
    except Exception:
        if days_since_failure >= 5:
            logger.warning(
                f"Max attempts reached: Aborting future attempts for report dated {dt}; "
                "its rows stay without trades and delivered quantity."
            )
            return True
        logger.info(f"{dt:%d %b}: Delivery report not yet updated.")
        return False

    try:
        report = read_delivery_report(report_file, dt.date(), logger)
        updated = store_delivery(report, dt.date(), database)
        if updated:
            logger.info(f"{dt:%Y-%m-%d}: late delivery report stored for {updated} NSE rows.")
        else:
            logger.warning(f"{dt:%Y-%m-%d}: late delivery report matched no stored NSE row.")
    except Exception as error:
        logger.error(f"Error storing pending delivery data for {date_str}: {error}; it stays pending.")
        if report_file and Path(report_file).exists():
            Path(report_file).unlink()
        return False

    try:
        if daily_bhavcopy_dir is not None:
            daily_file = daily_bhavcopy_dir / f"{dt:%Y%m%d}_BENNY_NSE_EOD.txt"
            if daily_file.exists():
                _update_daily_text(daily_file, report)
            else:
                logger.warning(f"Consolidated daily bhavcopy missing for pending delivery date {dt:%Y-%m-%d}")
    except Exception as error:                  # the text copy is optional; the store already has the figures
        logger.error(f"Error updating the daily bhavcopy text for {date_str}: {error}")
    finally:
        if report_file and Path(report_file).exists():
            Path(report_file).unlink()
    return True
