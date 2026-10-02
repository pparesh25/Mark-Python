from __future__ import annotations

import logging
import json
import time
import os
import re
import sys
import zipfile
import threading
import dateutil.parser
import itertools
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import tzlocal
import config
from screening_readiness import EOD_READY_TIME

from .delivery import update_pending_delivery_data
from .holidays import (
    get_holiday_list as fetch_holiday_list,
    get_muhurat_holiday_info,
    is_special_session,
)
from .indices import update_index_eod as persist_index_eod
from .stock_data import update_nse_eod as persist_stock_eod
from .stock_data import update_nse_symbol as persist_stock_symbol
from .sync import SyncDependencies, sync_database as run_sync_database

try:
    from nse import NSE
    NSE_AVAILABLE = True
except ImportError:
    NSE_AVAILABLE = False

dateRegex = re.compile(r"\b[A-Za-z]+, [A-Za-z]+ \d{2}, \d{4}\b")
headerText = b"Date,Open,High,Low,Close,Volume,Series,TOTAL_TRADES,QTY_PER_TRADE,DLV_QTY\n"
indexHeaderText = b"Date,Open,High,Low,Close,Volume,P/E,Series,TOTAL_TRADES,QTY_PER_TRADE,DLV_QTY\n"
tz_IN = ZoneInfo("Asia/Kolkata")
tz_local = tzlocal.get_localzone()

sync_lock = threading.Lock()


class DateEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)


class Dates:
    "A class for date related functions in EOD2"

    def __init__(self, lastUpdate: str, tz_IN: ZoneInfo):
        today = datetime.now(tz_IN)
        self.today = datetime.combine(today, datetime.min.time())
        dt = datetime.fromisoformat(lastUpdate).astimezone(tz_IN)
        self.dt = self.lastUpdate = dt
        self.pandasDt = self.dt.strftime("%Y-%m-%d")
        self.tz_IN = tz_IN

    def nextDate(self) -> bool:
        """Set the next trading date and return True.
        If its a future date, return False"""
        curTime = datetime.now(self.tz_IN)
        self.dt = self.dt + timedelta(1)

        if self.dt > curTime:
            return False

        # Use the same 6 PM IST cut-off as the readiness check and the BSE
        # downloader so one catch-up run cannot leave NSE a session ahead.
        if self.dt.date() == curTime.date() and curTime.time() < EOD_READY_TIME:
            # Display the users local time
            local_time = curTime.replace(
                hour=EOD_READY_TIME.hour, minute=EOD_READY_TIME.minute
            ).astimezone(tz_local)
            t_str = local_time.strftime("%I:%M%p")  # 06:00PM
            logging.info(
                f"All Up To Date. Check again after {t_str} for today's EOD data"
            )
            return False

        self.pandasDt = self.dt.strftime("%Y-%m-%d")
        return True


def retry(max_retries=10, base_wait=2, max_wait=10):
    """Decorator that retries a function or method with exponential backoff in case of exceptions."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    retries += 1
                    if retries >= max_retries:
                        raise e
                    wait_time = min(base_wait * (2 ** (retries - 1)), max_wait)
                    time.sleep(wait_time)
            return None
        return wrapper
    return decorator


def _check_reports_update_status(nse: NSE, dates: Dates) -> Dict[str, bool]:
    result = {
        "CM-UDIFF-BHAVCOPY-CSV": False,
        "CM-BHAVDATA-FULL": False,
        "INDEX-SNAPSHOT": False,
    }

    cm_data = nse.fetch_daily_reports_file_metadata(segment="CM")
    index_data = nse.fetch_daily_reports_file_metadata(segment="INDEX")

    if not (cm_data.get("CurrentDay") or index_data.get("CurrentDay")):
        return result

    cm_report_date = datetime.strptime(cm_data["currentDate"], "%d-%b-%Y")

    if cm_report_date != dates.today.replace(tzinfo=None):
        raise RuntimeError("Market is closed today or report date mismatch")

    for dct in itertools.chain(cm_data.get("CurrentDay", []), index_data.get("CurrentDay", [])):
        key = dct.get("fileKey")
        if key in result:
            result[key] = True

    return result


def _check_for_holidays(nse: NSE, dates: Dates, meta: dict, logger: logging.Logger) -> bool:
    curDt = dates.dt.strftime("%d-%b-%Y")

    if is_special_session(dates.dt.replace(tzinfo=None), meta.get("special_sessions", [])):
        return False

    if (
        "holidays" not in meta
        or meta.get("year") != dates.dt.year
    ):
        if dates.dt.year == dates.today.year:
            meta["holidays"] = _get_holiday_list(nse, logger)
            meta["year"] = dates.dt.year

    isMuhurat = curDt in meta.get("holidays", {}) and "Laxmi Pujan" in meta["holidays"][curDt]

    if isMuhurat:
        return False

    # Weekend check: NSE is closed on Saturdays (weekday 5) and Sundays
    # (weekday 6). The original code only skipped Sundays, which caused a
    # 404 on the Saturday bhavcopy download and aborted the entire sync
    # loop — leaving all subsequent trading days unsynced.
    if dates.dt.weekday() in (5, 6):  # Saturday or Sunday
        return True

    if curDt in meta.get("holidays", {}):
        logger.info(f"{curDt} Market Holiday: {meta['holidays'][curDt]}")
        return True

    return False


@retry()
def _get_holiday_list(nse: NSE, logger: logging.Logger):
    data = fetch_holiday_list(nse)
    logger.info("NSE Holiday list updated")
    return data


def _get_muhurat_holiday_info(holidays: Dict[str, List[dict]]) -> dict:
    return get_muhurat_holiday_info(holidays)


def _check_special_sessions(nse: NSE, meta: dict, dates: Dates, logger: logging.Logger, tz_IN: ZoneInfo) -> bool:
    last_update_str = meta.get("special_sessions_last_update", None)

    if last_update_str is None:
        last_update = (dates.dt - timedelta(5)).replace(tzinfo=None)
        meta["special_sessions"] = []
    else:
        last_update = datetime.fromisoformat(last_update_str)

    circulars = nse.circulars(
        dept_code="CMTR",
        from_date=last_update,
        to_date=dates.today.replace(tzinfo=None),
    )
    updated = False

    for circular in circulars.get("data", []):
        subject = circular.get("sub", "").lower()

        if "trading holiday" in subject and "on account of" in subject:
            if "holidays" in meta:
                try:
                    dt = dateutil.parser.parse(subject, fuzzy=True)
                    dt_key = dt.strftime("%d-%b-%Y")
                    if dt_key not in meta["holidays"]:
                        meta["holidays"][dt_key] = subject
                        logger.warning(f"Circular Holiday added: {subject}")
                except Exception:
                    continue
            continue

        if "live trading session" not in subject:
            continue

        try:
            dt = dateutil.parser.parse(subject, fuzzy=True)
            iso_date = dt.isoformat()
            if iso_date not in meta["special_sessions"]:
                meta["special_sessions"].append(iso_date)
                logger.warning(f"Circular Special Session: {subject}")
                updated = True
        except Exception:
            continue

    meta["special_sessions_last_update"] = dates.today.date().isoformat()
    return updated


def _update_pending_delivery_data(
    nse: NSE,
    date_str: str,
    daily_bhavcopy_dir: Optional[Path],
    logger: logging.Logger,
    tz_IN: ZoneInfo,
) -> bool:
    return update_pending_delivery_data(
        nse, date_str, daily_bhavcopy_dir, logger, tz_IN
    )


# Compatibility entry points.  The implementation now lives in focused
# modules; these names remain stable for the sync orchestrator and callers
# that imported the original monolithic module.
def _update_nse_eod(*args, **kwargs):
    return persist_stock_eod(*args, **kwargs)


def _update_nse_symbol(*args, **kwargs):
    return persist_stock_symbol(*args, **kwargs)


def _update_index_eod(*args, **kwargs):
    return persist_index_eod(*args, **kwargs)


def sync_database(
    data_dir: Path,
    logger: logging.Logger,
    metadata_dir: Path,
    daily_bhavcopy_dir: Optional[Path] = None,
) -> bool:
    """Backward-compatible entry point for the modular sync orchestrator."""

    deps = SyncDependencies(
        nse_available=NSE_AVAILABLE,
        nse_factory=lambda temp_dir: NSE(temp_dir, server=True),
        dates_factory=lambda last_update: Dates(last_update, tz_IN),
        check_special_sessions=_check_special_sessions,
        check_for_holidays=_check_for_holidays,
        check_reports_status=_check_reports_update_status,
        update_pending_delivery=_update_pending_delivery_data,
        update_stock=_update_nse_eod,
        update_index=_update_index_eod,
        date_encoder=DateEncoder,
        sync_lock=sync_lock,
        tz_in=tz_IN,
    )
    return run_sync_database(
        data_dir,
        logger,
        metadata_dir,
        daily_bhavcopy_dir,
        deps,
    )
