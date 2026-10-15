"""BSE EOD Data Downloader and Database Sync."""

from __future__ import annotations

import json
import logging
import zipfile
from datetime import datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import tzlocal
import config
from screening_readiness import clear_session_attempts, is_special_session_day, presume_session_closed

from .holidays import load_holidays_from_meta, load_special_sessions_from_meta, is_special_session
from .stock_data import update_bse_eod

tz_IN = ZoneInfo("Asia/Kolkata")
tz_local = tzlocal.get_localzone()

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.bseindia.com/",
}


class Dates:
    def __init__(self, lastUpdate: str, tz_IN: ZoneInfo):
        today = datetime.now(tz_IN)
        self.today = datetime.combine(today, datetime.min.time())
        dt = datetime.fromisoformat(lastUpdate).astimezone(tz_IN)
        self.dt = self.lastUpdate = dt
        self.pandasDt = self.dt.strftime("%Y-%m-%d")
        self.tz_IN = tz_IN

    def nextDate(self) -> bool:
        curTime = datetime.now(self.tz_IN)
        self.dt = self.dt + timedelta(1)

        if self.dt > curTime:
            return False

        # If it's today, only download after 6:00 PM (18:00) when BSE files are ready
        if self.dt.date() == curTime.date() and curTime.hour < 18:
            local_time = curTime.replace(hour=18, minute=0).astimezone(tz_local)
            t_str = local_time.strftime("%I:%M%p")
            logging.info(
                f"BSE EOD: All Up To Date. Check again after {t_str} for today's BSE EOD data"
            )
            return False

        self.pandasDt = self.dt.strftime("%Y-%m-%d")
        return True


BHAVCOPY_URL = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{day:%Y%m%d}_F_0000.CSV"
DELIVERY_URL = "https://www.bseindia.com/BSEDATA/gross/{day:%Y}/SCBSEALL{day:%d%m}.zip"


def merge_bse_day(bhav_bytes: bytes, delivery_zip: bytes | None, day, logger: logging.Logger) -> pd.DataFrame:
    """One BSE day: the UDiFF bhavcopy with SCBSEALL's delivered quantity joined on the scrip code.

    The delivery zip's table is its SCBSEALL*.TXT member (a few zips carry a
    second file), and its DATE column must name `day`: BSE sometimes ignores the
    year folder and serves another year's file (2010-04-29 came back as
    2011-04-29), which would put another day's delivery on this one.
    """
    df_bhav = pd.read_csv(BytesIO(bhav_bytes))
    df_bhav.columns = [c.strip() for c in df_bhav.columns]
    df_delv = pd.DataFrame(columns=["SCRIP CODE", "DELIVERY QTY"], dtype=str)
    if delivery_zip is not None:
        with zipfile.ZipFile(BytesIO(delivery_zip)) as zf:
            names = zf.namelist()
            member = next((n for n in names if n.upper().startswith("SCBSEALL")), names[0])
            raw = pd.read_csv(BytesIO(zf.read(member)), sep="|", dtype=str)
        raw.columns = [c.strip() for c in raw.columns]
        inside = set(raw.get("DATE", pd.Series(dtype=str)).astype(str).str.strip())
        if inside and inside != {day.strftime("%d%m%Y")}:
            logger.warning(f"BSE Deliverables for {day} hold another day ({sorted(inside)[:2]}); not used.")
        else:
            df_delv = raw
    df_bhav["FinInstrmId"] = df_bhav["FinInstrmId"].astype(str).str.strip()
    df_delv["SCRIP CODE"] = df_delv["SCRIP CODE"].astype(str).str.strip()
    merged = pd.merge(df_bhav, df_delv, left_on="FinInstrmId", right_on="SCRIP CODE", how="left")
    merged["TOTAL_TRADES"] = merged["TtlNbOfTxsExctd"]
    merged["DLV_QTY"] = pd.to_numeric(merged["DELIVERY QTY"], errors="coerce")
    return merged


def _write_meta(meta_file: Path, meta: dict) -> None:
    meta_file.write_text(json.dumps(meta, indent=4), encoding="utf-8")


def sync_database(
    data_dir: Path,
    logger: logging.Logger,
    metadata_dir: Path,
    daily_bhavcopy_dir: Path,
) -> bool:
    """Synchronize the BSE EOD database by downloading and processing missing dates."""
    metadata_dir.mkdir(parents=True, exist_ok=True)
    daily_bhavcopy_dir.mkdir(parents=True, exist_ok=True)

    meta_file = metadata_dir / "bse_meta.json"
    if meta_file.is_file():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    else:
        # Default to 15 days ago for initial sync range
        init_date = (datetime.now(tz_IN) - timedelta(15)).replace(hour=0, minute=0, second=0, microsecond=0)
        meta = {
            "lastUpdate": init_date.isoformat(),
            "data-version": 1.0,
            "year": init_date.year,
        }

    # Load holidays & special sessions from shared NSE meta.json
    holidays = load_holidays_from_meta(metadata_dir)
    special_sessions = load_special_sessions_from_meta(metadata_dir)

    dates = Dates(meta["lastUpdate"], tz_IN)
    logger.info(f"BSE Database sync starting. Last update: {dates.pandasDt}")

    # Main Day-by-Day Loop
    session = requests.Session()
    session.headers.update(headers)
    
    while dates.nextDate():
        curDt = dates.dt.strftime("%d-%b-%Y")
        is_weekend = dates.dt.weekday() in (5, 6)
        is_holiday = curDt in holidays
        # A special session (a circular's, a known one) or Muhurat trading, which
        # NSE's list names a holiday ("Laxmi Pujan"): BSE trades it too. Only the
        # circular's sessions counted here, so a Muhurat session was skipped.
        is_special = (is_special_session(dates.dt.replace(tzinfo=None), special_sessions)
                      or is_special_session_day(dates.dt.date(), holidays, ()))

        if is_weekend and not is_special:
            meta["lastUpdate"] = dates.dt.isoformat()
            _write_meta(meta_file, meta)
            continue

        if is_holiday and not is_special:
            logger.info(f"BSE EOD: {curDt} Market Holiday: {holidays[curDt]}")
            meta["lastUpdate"] = dates.dt.isoformat()
            _write_meta(meta_file, meta)
            continue

        logger.info(f"▶ BSE Processing Date: {dates.pandasDt}")

        # The day is stored as published; split and bonus factors are decided after
        # the sync from the raw prices and the exchanges' lists, matched by scrip
        # code (price_adjustments.refresh) -- not by today's name, not in place.
        # Download Bhavcopy & Deliverables
        bhav_date_str = dates.pandasDt.replace("-", "")
        bhavcopy_url = BHAVCOPY_URL.format(day=dates.dt)
        deliverables_url = DELIVERY_URL.format(day=dates.dt)

        logger.info(f"Downloading BSE Bhavcopy for {dates.pandasDt}...")
        r_bhav = session.get(bhavcopy_url, timeout=15)
        if r_bhav.status_code == 404:
            if presume_session_closed(meta, dates.dt.date(), datetime.now(tz_IN).date()):
                logger.warning(
                    f"BSE Bhavcopy for {dates.pandasDt} is still missing after "
                    "repeated runs; treating it as a market holiday."
                )
                meta["lastUpdate"] = dates.dt.isoformat()
                _write_meta(meta_file, meta)
                continue
            # Keep the cursor on this session: a file that is not published
            # yet (or a transient 404) must be retried, not skipped forever.
            logger.warning(
                f"BSE Bhavcopy not available for {dates.pandasDt} (HTTP 404). "
                "Sync stopped; this date will be retried on the next run."
            )
            _write_meta(meta_file, meta)
            return False
        elif r_bhav.status_code != 200:
            logger.error(f"Failed to download BSE Bhavcopy for {dates.pandasDt} (HTTP {r_bhav.status_code})")
            return False

        # Delivery quantities are optional enrichment; prices are stored even
        # when the deliverables archive is late or missing.
        logger.info(f"Downloading BSE Deliverables for {dates.pandasDt}...")
        try:
            r_delv = session.get(deliverables_url, timeout=15)
            delivery_status = r_delv.status_code
        except requests.RequestException as error:
            r_delv = None
            delivery_status = error
        if delivery_status != 200:
            logger.warning(
                f"BSE Deliverables unavailable for {dates.pandasDt} ({delivery_status}); "
                "storing prices without delivery data."
            )


        try:
            df_merged = merge_bse_day(r_bhav.content, r_delv.content if delivery_status == 200 else None,
                                      dates.dt.date(), logger)
            raw_bhav_file = daily_bhavcopy_dir / f"BhavCopy_BSE_CM_{bhav_date_str}.csv"
            raw_bhav_file.write_bytes(r_bhav.content)
        except Exception as e:
            logger.error(f"Error parsing download files for {dates.pandasDt}: {e}")
            return False

        # Store the day
        try:
            update_bse_eod(df_merged, data_dir, logger, metadata_dir, dates)
        except Exception as e:
            logger.error(f"Error updating BSE EOD files for {dates.pandasDt}: {e}")
            return False

        # Save checkpoint
        clear_session_attempts(meta, dates.dt.date())
        meta["lastUpdate"] = dates.dt.isoformat()
        _write_meta(meta_file, meta)

    logger.info("BSE EOD Sync Pipeline completed successfully!")
    return True
