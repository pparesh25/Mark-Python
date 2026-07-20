"""BSE EOD Data Downloader and Database Sync."""

from __future__ import annotations

import json
import logging
import time
import os
import re
import zipfile
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import tzlocal

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

from .corporate_actions import parse_bonus_factor, parse_split_factor
from .holidays import load_holidays_from_meta, load_special_sessions_from_meta, is_special_session
from .stock_data import update_bse_eod, update_bse_symbol

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


def _download_corporate_actions(
    start_date: datetime,
    end_date: datetime,
    download_dir: Path,
    logger: logging.Logger,
) -> pd.DataFrame:
    """Download corporate actions from BSE website using Selenium and load as DataFrame."""
    logger.info("Downloading BSE Corporate Actions using Selenium...")
    download_dir.mkdir(parents=True, exist_ok=True)
    
    # Clean up existing Corporate_Actions files in temp dir
    for f in download_dir.glob("Corporate_Actions*.csv"):
        try:
            f.unlink()
        except OSError:
            pass

    chrome_options = Options()
    chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    chrome_options.add_experimental_option("prefs", {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True
    })

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    
    # Enable headless downloads
    driver.execute_cdp_cmd('Page.setDownloadBehavior', {
        'behavior': 'allow',
        'downloadPath': str(download_dir)
    })

    try:
        driver.get("https://www.bseindia.com/corporates/corporate_act")
        time.sleep(5)
        
        datepickers = driver.find_elements(By.NAME, "datepicker")
        if len(datepickers) < 2:
            raise RuntimeError("Could not find date picker inputs on page.")

        from_date_input = datepickers[0]
        to_date_input = datepickers[1]

        from_date_str = start_date.strftime("%d/%m/%Y")
        to_date_str = end_date.strftime("%d/%m/%Y")

        driver.execute_script("arguments[0].value = arguments[1];", from_date_input, from_date_str)
        driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", from_date_input)
        driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", from_date_input)

        driver.execute_script("arguments[0].value = arguments[1];", to_date_input, to_date_str)
        driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", to_date_input)
        driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", to_date_input)

        logger.info(f"Setting dates: {from_date_str} to {to_date_str}")
        time.sleep(1)

        submit_btn = driver.find_element(By.ID, "btnSubmit")
        driver.execute_script("arguments[0].click();", submit_btn)
        time.sleep(5)

        download_btn = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "dwld-icon"))
        )
        driver.execute_script("arguments[0].click();", download_btn)
        
        # Wait for file to download
        time.sleep(5)
        
        downloaded_files = list(download_dir.glob("Corporate_Actions*.csv"))
        if not downloaded_files:
            raise FileNotFoundError("Corporate Actions CSV download failed.")
        
        csv_file = downloaded_files[0]
        logger.info(f"Successfully downloaded: {csv_file.name}")
        df = pd.read_csv(csv_file)
        
        # Normalize columns
        df.columns = [c.strip() for c in df.columns]
        
        # Parse Ex Date
        df["Ex_Date_Parsed"] = pd.to_datetime(df["Ex Date"], format="%d %b %Y", errors="coerce")
        return df

    finally:
        driver.quit()


def _get_split(sym: str, string: str, logger: logging.Logger) -> float | None:
    factor = parse_split_factor(string)
    if factor is None:
        logger.warning(f"BSE {sym}: Split Not Matched. {string}")
        return None
    return factor


def _get_bonus(sym: str, string: str, logger: logging.Logger) -> float | None:
    factor = parse_bonus_factor(string)
    if factor is None:
        logger.warning(f"BSE {sym}: Bonus Not Matched. {string}")
        return None
    return factor


def _make_adjustment(
    symbol: str,
    adjustmentFactor: float,
    data_dir: Path,
    dates: Dates,
    logger: logging.Logger,
) -> None:
    # Try normal symbol, then SME symbol
    for sym_suffix in ("", "_SME"):
        sym = f"{symbol.upper()}{sym_suffix}"
        import duckdb
        conn = duckdb.connect(str(config.DUCKDB_FILE))
        try:
            df = conn.execute("SELECT * FROM eod_data WHERE Symbol = ? AND Exchange = 'BSE'", (sym,)).fetchdf()
        finally:
            conn.close()

        if df.empty:
            continue

        logger.info(f"BSE Adjusting in DuckDB: {sym} by factor {adjustmentFactor:.4f}")
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.set_index("Date")

        # Remove timezone info as DataFrame index is not timezone aware
        dt = dates.dt.replace(tzinfo=None)

        if dt in df.index:
            idx = df.index.get_loc(dt)
            if isinstance(idx, slice):
                logger.warning(f"Duplicate dates detected on BSE {sym} making adjustment - {dates.pandasDt}")
                raise RuntimeError()
            last = df.iloc[idx:]
            df = df.iloc[:idx].copy()
        else:
            last = df.loc[dt:]
            df = df.loc[:dt].copy()

        for col in ("Open", "High", "Low", "Close"):
            df.loc[:, col] = ((df[col] / adjustmentFactor / 0.05).round() * 0.05).round(2)

        df = pd.concat([df, last])

        # Write back to DuckDB
        conn = duckdb.connect(str(config.DUCKDB_FILE))
        try:
            conn.execute("DELETE FROM eod_data WHERE Symbol = ? AND Exchange = 'BSE'", (sym,))
            df_insert = df.reset_index()
            df_insert['Date'] = pd.to_datetime(df_insert['Date']).dt.date
            conn.execute("INSERT INTO eod_data SELECT * FROM df_insert")
        finally:
            conn.close()
        break


def _adjust_bse_stocks(actions_df: pd.DataFrame, data_dir: Path, dates: Dates, logger: logging.Logger):
    """Adjust stock files for splits/bonuses on the Ex Date."""
    dt_today = dates.dt.replace(tzinfo=None)
    
    # Filter actions matching today's Ex Date
    day_actions = actions_df[actions_df["Ex_Date_Parsed"] == dt_today]
    if day_actions.empty:
        return

    logger.info(f"BSE: Processing {len(day_actions)} corporate actions for {dates.pandasDt}")

    for _, row in day_actions.iterrows():
        code = str(row["Security Code"]).strip()
        purpose = str(row["Purpose"]).lower()
        remarks = str(row.get("Detail/Remarks", "")).lower()
        symbol = str(row["Security Name"]).strip()

        # BSE lists splits and bonuses in Purpose
        if "split" in purpose or "sub-division" in purpose:
            adjustmentFactor = _get_split(symbol, purpose + " " + remarks, logger)
            if adjustmentFactor:
                _make_adjustment(symbol, adjustmentFactor, data_dir, dates, logger)

        elif "bonus" in purpose:
            # Reconstruct ratio
            adjustmentFactor = _get_bonus(symbol, purpose + " " + remarks, logger)
            if adjustmentFactor:
                _make_adjustment(symbol, adjustmentFactor, data_dir, dates, logger)


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
        with open(meta_file, "r") as f:
            meta = json.load(f)
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

    # Determine date range to download corporate actions
    temp_dir = Path("/tmp/bse_actions")
    actions_df = pd.DataFrame()
    
    # Check if we have days to sync
    test_dates = Dates(meta["lastUpdate"], tz_IN)
    has_days_to_sync = False
    while test_dates.nextDate():
        curDt = test_dates.dt.strftime("%d-%b-%Y")
        is_weekend = test_dates.dt.weekday() in (5, 6)
        is_holiday = curDt in holidays
        is_special = is_special_session(test_dates.dt.replace(tzinfo=None), special_sessions)
        
        # If it's a trading day, we need corporate actions
        if not (is_weekend or is_holiday) or is_special:
            has_days_to_sync = True
            break

    if has_days_to_sync:
        try:
            actions_df = _download_corporate_actions(
                start_date=dates.dt,
                end_date=datetime.now(tz_IN),
                download_dir=temp_dir,
                logger=logger
            )
        except Exception as e:
            logger.error(f"Failed to download BSE corporate actions: {e}. Sync continuing without adjustments.")

    # Main Day-by-Day Loop
    while dates.nextDate():
        curDt = dates.dt.strftime("%d-%b-%Y")
        is_weekend = dates.dt.weekday() in (5, 6)
        is_holiday = curDt in holidays
        is_special = is_special_session(dates.dt.replace(tzinfo=None), special_sessions)

        if is_weekend and not is_special:
            meta["lastUpdate"] = dates.dt.isoformat()
            with open(meta_file, "w") as f:
                json.dump(meta, f, indent=4)
            continue

        if is_holiday and not is_special:
            logger.info(f"BSE EOD: {curDt} Market Holiday: {holidays[curDt]}")
            meta["lastUpdate"] = dates.dt.isoformat()
            with open(meta_file, "w") as f:
                json.dump(meta, f, indent=4)
            continue

        logger.info(f"▶ BSE Processing Date: {dates.pandasDt}")

        # Step 1: Corporate Actions Adjustments (Splits/Bonuses)
        if not actions_df.empty:
            try:
                _adjust_bse_stocks(actions_df, data_dir, dates, logger)
            except Exception as e:
                logger.error(f"Error applying corporate actions for {dates.pandasDt}: {e}")

        # Step 2: Download Bhavcopy & Deliverables
        bhav_date_str = dates.pandasDt.replace("-", "")
        bhavcopy_url = f"https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{bhav_date_str}_F_0000.CSV"
        
        delv_date_str = dates.dt.strftime("%d%m")
        deliverables_url = f"https://www.bseindia.com/BSEDATA/gross/{dates.dt.year}/SCBSEALL{delv_date_str}.zip"

        logger.info(f"Downloading BSE Bhavcopy for {dates.pandasDt}...")
        r_bhav = requests.get(bhavcopy_url, headers=headers, timeout=15)
        if r_bhav.status_code == 404:
            logger.warning(f"BSE Bhavcopy not found for {dates.pandasDt}. Skipping day (likely a market holiday).")
            meta["lastUpdate"] = dates.dt.isoformat()
            with open(meta_file, "w") as f:
                json.dump(meta, f, indent=4)
            continue
        elif r_bhav.status_code != 200:
            logger.error(f"Failed to download BSE Bhavcopy for {dates.pandasDt} (HTTP {r_bhav.status_code})")
            return False

        logger.info(f"Downloading BSE Deliverables for {dates.pandasDt}...")
        r_delv = requests.get(deliverables_url, headers=headers, timeout=15)
        if r_delv.status_code == 404:
            logger.warning(f"BSE Deliverables not found for {dates.pandasDt}. Skipping day (likely a market holiday).")
            meta["lastUpdate"] = dates.dt.isoformat()
            with open(meta_file, "w") as f:
                json.dump(meta, f, indent=4)
            continue
        elif r_delv.status_code != 200:
            logger.error(f"Failed to download BSE Deliverables for {dates.pandasDt} (HTTP {r_delv.status_code})")
            return False

        # Load both DataFrames
        try:
            df_bhav = pd.read_csv(BytesIO(r_bhav.content))
            df_bhav.columns = [c.strip() for c in df_bhav.columns]
            
            # Save raw daily bhavcopy
            raw_bhav_file = daily_bhavcopy_dir / f"BhavCopy_BSE_CM_{bhav_date_str}.csv"
            raw_bhav_file.write_bytes(r_bhav.content)
            
            with zipfile.ZipFile(BytesIO(r_delv.content)) as zf:
                with zf.open(zf.namelist()[0]) as f_delv:
                    raw_delv_bytes = f_delv.read()
                    
            df_delv = pd.read_csv(BytesIO(raw_delv_bytes), sep="|", dtype=str)
            df_delv.columns = [c.strip() for c in df_delv.columns]
            
            # Data Cleaning and Normalization
            df_bhav["FinInstrmId"] = df_bhav["FinInstrmId"].astype(str).str.strip()
            df_delv["SCRIP CODE"] = df_delv["SCRIP CODE"].astype(str).str.strip()
            
            # Merge on Scrip Code
            df_merged = pd.merge(
                df_bhav,
                df_delv,
                left_on="FinInstrmId",
                right_on="SCRIP CODE",
                how="left"
            )
            
            # Map column names
            df_merged["TOTAL_TRADES"] = df_merged["TtlNbOfTxsExctd"]
            df_merged["DLV_QTY"] = df_merged["DELIVERY QTY"]
            
        except Exception as e:
            logger.error(f"Error parsing download files for {dates.pandasDt}: {e}")
            return False

        # Step 3: Update individual stock CSV files
        try:
            update_bse_eod(df_merged, data_dir, logger, metadata_dir, dates)
        except Exception as e:
            logger.error(f"Error updating BSE EOD files for {dates.pandasDt}: {e}")
            return False

        # Save checkpoint
        meta["lastUpdate"] = dates.dt.isoformat()
        with open(meta_file, "w") as f:
            json.dump(meta, f, indent=4)
            
    logger.info("BSE EOD Sync Pipeline completed successfully!")
    return True
