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

from .corporate_actions import parse_bonus_factor, parse_split_factor
from .delivery import update_pending_delivery_data
from .holidays import (
    get_holiday_list as fetch_holiday_list,
    get_muhurat_holiday_info,
    is_special_session,
)
from .indices import update_index_eod as persist_index_eod
from .indices import update_indice as persist_indice
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

        if self.dt.day == curTime.day and curTime.hour < 16:
            # Display the users local time
            local_time = curTime.replace(hour=19, minute=0).astimezone(tz_local)
            t_str = local_time.strftime("%I:%M%p")  # 07:00PM
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


def _get_split(sym: str, string: str, logger: logging.Logger) -> float | None:
    factor = parse_split_factor(string)
    if factor is None:
        logger.warning(f"{sym}: Split Not Matched. {string}")
        return None
    return factor


def _get_bonus(sym: str, string: str, logger: logging.Logger) -> float | None:
    factor = parse_bonus_factor(string)
    if factor is None:
        logger.warning(f"{sym}: Bonus Not Matched. {string}")
        return None
    return factor


def _make_adjustment(
    symbol: str,
    adjustmentFactor: float,
    data_dir: Path,
    dates: Dates,
    logger: logging.Logger,
    prev_commit: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[pd.DataFrame, Any]]:
    if prev_commit:
        file = prev_commit["file"]
        df = prev_commit["df"]
    else:
        file = symbol
        import duckdb
        conn = duckdb.connect(str(config.DUCKDB_FILE))
        try:
            df = conn.execute("SELECT * FROM eod_data WHERE Symbol = ? AND Exchange = 'NSE'", (symbol,)).fetchdf()
        finally:
            conn.close()

        if df.empty:
            logger.warning(f"{symbol}: Data not found in DuckDB for adjustment - {dates.pandasDt}")
            return None

        df['Date'] = pd.to_datetime(df['Date'])
        df = df.set_index("Date")

    # Remove timezone info as DataFrame index is not timezone aware
    dt = dates.dt.replace(tzinfo=None)

    if dt in df.index:
        idx = df.index.get_loc(dt)
        if isinstance(idx, slice):
            logger.warning(
                f"Duplicate dates detected on {symbol} making adjustment - {dates.pandasDt}"
            )
            raise RuntimeError()
        last = df.iloc[idx:]
        df = df.iloc[:idx].copy()
    else:
        last = df.loc[dt:]
        df = df.loc[:dt].copy()
        if not df.empty:
            idx = df.index.get_loc(df.index[-1])

    for col in ("Open", "High", "Low", "Close"):
        # nearest 0.05 = round(nu / 0.05) * 0.05
        df.loc[:, col] = ((df[col] / adjustmentFactor / 0.05).round() * 0.05).round(2)

    df = pd.concat([df, last])

    return (df, file)


def _adjust_nse_stocks(meta: dict, data_dir: Path, dates: Dates, logger: logging.Logger):
    logger.info("Making adjustments for splits and bonus")

    dtStr = dates.dt.strftime("%d-%b-%Y")

    for actions in ("equityActions", "smeActions", "mfActions"):
        df_commits: dict[str, dict[str, Any]] = {}
        post_commits: List[Tuple[str, float]] = []
        error_context = None

        try:
            for act in meta.get(actions, []):
                sym = act["symbol"]
                purpose = act["subject"].lower()
                ex = act["exDate"]
                series = act["series"]

                if series not in ("EQ", "BE", "BZ", "SM", "ST"):
                    continue

                if series in ("SM", "ST"):
                    sym += "_sme"

                if (
                    "split" in purpose
                    or "splt" in purpose
                    or "consolidation" in purpose
                    ) and ex == dtStr:
                    if "consolidation" in purpose:
                        error_context = f"{sym} - consolidation - {dtStr}"
                        i = purpose.index("consolidation")
                    else:
                        error_context = f"{sym} - Split - {dtStr}"
                        i = purpose.index("spl")

                    adjustmentFactor = _get_split(sym, purpose[i:], logger)

                    if adjustmentFactor is None:
                        logger.warning(
                            f"Possible adjustment failure: SPLIT - {sym} - {purpose} - exDate: {dtStr}"
                        )
                        continue

                    commit = _make_adjustment(
                        sym, adjustmentFactor, data_dir, dates, logger, df_commits.get(sym, None)
                    )

                    if commit:
                        df, file = commit
                        if sym in df_commits:
                            df_commits[sym]["df"] = df
                        else:
                            df_commits[sym] = {"file": file, "df": df}

                        post_commits.append((sym, adjustmentFactor))
                        logger.info(f"{sym}: {purpose}")

                if "bonus" in purpose and ex == dtStr:
                    if (
                        "deb" in purpose
                        or "pref" in purpose
                        or "ncrps" in purpose
                        or "dvr" in purpose
                    ):
                        continue

                    error_context = f"{sym} - Bonus - {dtStr}"
                    adjustmentFactor = _get_bonus(sym, purpose, logger)

                    if adjustmentFactor is None:
                        logger.warning(
                            f"Possible adjustment failure: BONUS - {sym} - {purpose} - exDate: {dtStr}"
                        )
                        continue

                    commit = _make_adjustment(
                        sym, adjustmentFactor, data_dir, dates, logger, df_commits.get(sym, None)
                    )

                    if commit:
                        df, file = commit
                        if sym in df_commits:
                            df_commits[sym]["df"] = df
                        else:
                            df_commits[sym] = {"file": file, "df": df}

                        post_commits.append((sym, adjustmentFactor))
                        logger.warning(f"{sym}: {purpose}")

        except Exception as e:
            logger.critical(f"Adjustment Error - Context {error_context}")
            df_commits.clear()
            raise e

        # commit changes
        for sym, commit in df_commits.items():
            sym_name = commit["file"]
            df = commit["df"]

            dt = dates.dt.replace(tzinfo=None)

            if dt in df.index:
                idx = df.index.get_loc(dt)
                if isinstance(idx, int):
                    close = df.at[df.index[idx], "Close"]
                    if idx > 0:
                        prev_close = df.at[df.index[idx - 1], "Close"]
                        diff = close / prev_close
                        if diff > 1.5 or diff < 0.67:
                            context = f"Current Close {close}, Previous Close {prev_close}"
                            logger.warning(
                                f"WARN: Possible adjustment failure in {sym}: {context} - {dates.pandasDt}"
                            )
            else:
                logger.warning(
                    f"Unable to verify adjustment on {sym} - Please confirm manually. - {dates.pandasDt}"
                )

            import duckdb
            conn = duckdb.connect(str(config.DUCKDB_FILE))
            try:
                conn.execute("DELETE FROM eod_data WHERE Symbol = ? AND Exchange = 'NSE'", (sym,))
                df_insert = df.reset_index()
                df_insert['Date'] = pd.to_datetime(df_insert['Date']).dt.date
                conn.execute("INSERT INTO eod_data SELECT * FROM df_insert")
            finally:
                conn.close()

        df_commits.clear()
        post_commits.clear()


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


@retry()
def _validate_nse_actions_file(nse: NSE, dates: Dates, meta: dict, logger: logging.Logger, tz_IN: ZoneInfo):
    for action in ("equity", "sme", "mf"):
        segment = "equities" if action == "equity" else action

        if f"{action}Actions" not in meta:
            logger.warning(f"Downloading NSE {action.upper()} actions")

            meta[f"{action}Actions"] = nse.actions(
                segment=segment,
                from_date=dates.dt,
                to_date=dates.dt + timedelta(8),
            )

            meta[f"{action}ActionsExpiry"] = (dates.dt + timedelta(7)).isoformat()
        else:
            expiryDate = datetime.fromisoformat(
                meta[f"{action}ActionsExpiry"]
            ).astimezone(tz_IN)

            newExpiry = (expiryDate + timedelta(7)).isoformat()

            # Update every 7 days from last download
            if dates.dt < expiryDate:
                continue

            logger.warning(f"Updating NSE {action.upper()} actions")

            meta[f"{action}Actions"] = nse.actions(
                segment=segment,
                from_date=expiryDate,
                to_date=expiryDate + timedelta(8),
            )

            meta[f"{action}ActionsExpiry"] = newExpiry


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


def _update_pending_delivery_data(nse: NSE, date_str: str, data_dir: Path, logger: logging.Logger, tz_IN: ZoneInfo) -> bool:
    return update_pending_delivery_data(nse, date_str, data_dir, logger, tz_IN)


# Compatibility entry points.  The implementation now lives in focused
# modules; these names remain stable for the sync orchestrator and callers
# that imported the original monolithic module.
def _update_nse_eod(*args, **kwargs):
    return persist_stock_eod(*args, **kwargs)


def _update_nse_symbol(*args, **kwargs):
    return persist_stock_symbol(*args, **kwargs)


def _update_index_eod(*args, **kwargs):
    return persist_index_eod(*args, **kwargs)


def _update_indice(*args, **kwargs):
    return persist_indice(*args, **kwargs)


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
        validate_actions=_validate_nse_actions_file,
        check_reports_status=_check_reports_update_status,
        update_pending_delivery=_update_pending_delivery_data,
        update_stock=_update_nse_eod,
        update_index=_update_index_eod,
        adjust_stocks=_adjust_nse_stocks,
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
