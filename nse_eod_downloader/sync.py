"""EOD synchronization orchestration.

The workflow is dependency-injected so it can be tested without contacting
NSE or touching the real data directory.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from screening_readiness import clear_session_attempts, presume_session_closed


@dataclass(frozen=True)
class SyncDependencies:
    nse_available: bool
    nse_factory: Callable[[Path], Any]
    dates_factory: Callable[[str], Any]
    check_special_sessions: Callable[..., bool]
    check_for_holidays: Callable[..., bool]
    check_reports_status: Callable[..., dict[str, bool]]
    update_pending_delivery: Callable[..., bool]
    update_stock: Callable[..., Any]
    update_index: Callable[..., Any]
    date_encoder: type[json.JSONEncoder]
    sync_lock: Any
    tz_in: Any


def sync_database(
    data_dir: Path,
    logger: Any,
    metadata_dir: Path,
    daily_bhavcopy_dir: Optional[Path],
    deps: SyncDependencies,
) -> bool:
    """Synchronize local NSE data using injected downloader/maintenance code."""

    if not deps.nse_available:
        logger.error("nse package is not installed; database cannot be synced.")
        return False

    meta_file = metadata_dir / "meta.json"
    if not meta_file.exists():
        logger.error(f"meta.json not found in {metadata_dir}. Bootstrap first.")
        return False

    with deps.sync_lock:
        try:
            meta = json.loads(meta_file.read_bytes())
            last_update_str = meta.get("lastUpdate")
            if not last_update_str:
                logger.error("meta.json does not have 'lastUpdate'")
                return False

            dates = deps.dates_factory(last_update_str)
            temp_dir = data_dir / "temp"
            temp_dir.mkdir(exist_ok=True)
            nse = deps.nse_factory(temp_dir)
            meta_updated = False

            if deps.check_special_sessions(nse, meta, dates, logger, deps.tz_in):
                meta_updated = True

            meta.setdefault("DLV_PENDING_DATES", [])
            if meta["DLV_PENDING_DATES"]:
                pending_list = meta["DLV_PENDING_DATES"].copy()
                logger.info("Updating pending delivery reports.")
                for date_str in pending_list:
                    if deps.update_pending_delivery(
                        nse, date_str, daily_bhavcopy_dir, logger, deps.tz_in
                    ):
                        meta["DLV_PENDING_DATES"].remove(date_str)
                        meta_updated = True

            while dates.nextDate():
                if deps.check_for_holidays(nse, dates, meta, logger):
                    meta["lastUpdate"] = dates.lastUpdate = dates.dt
                    meta_updated = True
                    continue

                meta_updated = True
                logger.info(f"Downloading NSE reports for {dates.pandasDt}")

                report_status = None
                if dates.dt.date() == dates.today.date():
                    try:
                        report_status = deps.check_reports_status(nse, dates)
                        required_reports = {
                            "CM-UDIFF-BHAVCOPY-CSV": "Equity Bhavcopy not yet updated.",
                            "INDEX-SNAPSHOT": "Indices report not yet updated.",
                            "CM-BHAVDATA-FULL": "Delivery Report Unavailable. Will retry in subsequent sync",
                        }
                        for key, message in required_reports.items():
                            if not report_status.get(key):
                                logger.warning(message)
                                if key != "CM-BHAVDATA-FULL":
                                    raise RuntimeError("Reports not yet updated on NSE")
                    except Exception as error:
                        logger.info(f"Reports check failed or not ready: {error}. Stopping sync.")
                        break

                try:
                    bhav_file = nse.equityBhavcopy(dates.dt)
                    index_file = nse.indicesBhavcopy(dates.dt)
                except Exception as error:
                    meta_updated = True
                    if presume_session_closed(meta, dates.dt.date(), dates.today.date()):
                        logger.warning(
                            f"NSE bhavcopy/indices for {dates.pandasDt} are still unavailable "
                            f"after repeated runs ({error}); treating it as a market holiday."
                        )
                        meta["lastUpdate"] = dates.lastUpdate = dates.dt
                        continue
                    # Keep the cursor on this session so the next run retries
                    # it; skipping ahead would leave a permanent price gap.
                    logger.warning(
                        f"Failed to download bhavcopy/indices for {dates.pandasDt}: {error}. "
                        "Sync stopped; this date will be retried on the next run."
                    )
                    break

                delivery_file = None
                if report_status is None or report_status.get("CM-BHAVDATA-FULL"):
                    try:
                        delivery_file = nse.deliveryBhavcopy(dates.dt)
                    except Exception:
                        meta["DLV_PENDING_DATES"].append(dates.dt.isoformat())
                        logger.warning(
                            f"Delivery report unavailable for {dates.pandasDt}. Will retry."
                        )
                else:
                    meta["DLV_PENDING_DATES"].append(dates.dt.isoformat())

                try:
                    isin = pd.read_csv(metadata_dir / "isin.csv", index_col="ISIN")
                    bhavcopy_rows: list[str] = []
                    deps.update_stock(
                        bhav_file, delivery_file, data_dir, metadata_dir,
                        isin, dates, logger, bhavcopy_rows,
                    )
                    deps.update_index(index_file, data_dir, dates, logger, bhavcopy_rows)
                    # No adjustment here: the day is stored as published, and the split and
                    # bonus factors are decided after the sync from the raw prices and the
                    # exchanges' lists (price_adjustments.refresh) -- never divided in place.

                    if daily_bhavcopy_dir and bhavcopy_rows:
                        daily_bhavcopy_dir.mkdir(parents=True, exist_ok=True)
                        output = daily_bhavcopy_dir / (
                            f"{dates.dt.strftime('%Y%m%d')}_BENNY_NSE_EOD.txt"
                        )
                        header = "Symbol,Date,Open,High,Low,Close,Volume,Series,TOTAL_TRADES,QTY_PER_TRADE,DLV_QTY\n"
                        output.write_text(header + "".join(bhavcopy_rows), encoding="utf-8")
                        logger.info(f"Saved daily bhavcopy to {output.name}")

                    for file in (bhav_file, delivery_file, index_file):
                        if file and file.exists():
                            file.unlink()
                except Exception:
                    logger.exception(f"Error during sync for {dates.pandasDt}.")
                    for file in (bhav_file, delivery_file, index_file):
                        if file and file.exists():
                            file.unlink()
                    break

                clear_session_attempts(meta, dates.dt.date())
                meta["lastUpdate"] = dates.lastUpdate = dates.dt
                meta_updated = True
                logger.info(f"{dates.pandasDt}: Sync complete")

            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            if meta_updated:
                meta_file.write_text(
                    json.dumps(meta, indent=3, cls=deps.date_encoder),
                    encoding="utf-8",
                )
            nse.exit()
            return True
        except Exception as error:
            logger.error(f"Error in sync_database: {error}")
            return False
