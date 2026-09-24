"""BSE EOD Data Downloader and Database Sync."""

from __future__ import annotations

import json
import logging
import hashlib
import zipfile
from datetime import datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import tzlocal
import config
from screening_readiness import clear_session_attempts, presume_session_closed

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
    logger: logging.Logger,
) -> pd.DataFrame:
    """Download corporate actions from BSE website directly using HTTP API and return as DataFrame."""
    logger.info("Downloading BSE Corporate Actions directly via API...")
    
    url = "https://api.bseindia.com/BseIndiaAPI/api/CorpactCSVDownload/w"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.bseindia.com/",
        "Origin": "https://www.bseindia.com"
    }
    
    fdate_str = start_date.strftime("%Y%m%d")
    tdate_str = end_date.strftime("%Y%m%d")
    
    params = {
        "scripcode": "",
        "Fdate": fdate_str,
        "TDate": tdate_str,
        "Purposecode": "",
        "strSearch": "S",
        "ddlindustrys": "",
        "ddlcategorys": "E",
        "segment": "0"
    }
    
    logger.info(f"BSE API Call: dates {fdate_str} to {tdate_str}")
    r = requests.get(url, headers=headers, params=params, timeout=15)
    
    if r.status_code != 200 or not r.text.strip():
        raise RuntimeError(
            f"BSE corporate-action API returned HTTP {r.status_code} "
            "without usable data"
        )
        
    df = pd.read_csv(StringIO(r.text))
    
    # Normalize columns
    df.columns = [c.strip() for c in df.columns]
    
    # Parse Ex Date
    df["Ex_Date_Parsed"] = pd.to_datetime(df["Ex Date"], format="%d %b %Y", errors="coerce")
    return df


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
    _data_dir: Path,
    dates: Dates,
    logger: logging.Logger,
    action_key: str,
) -> tuple[str, str | None, int]:
    """Apply one corporate action once in DuckDB."""
    import duckdb

    conn = duckdb.connect(str(config.DUCKDB_FILE))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bse_corporate_action_ledger (
            Action_Key VARCHAR NOT NULL,
            Ex_Date DATE NOT NULL,
            Symbol VARCHAR NOT NULL,
            Factor DOUBLE NOT NULL,
            Rows_Adjusted BIGINT NOT NULL,
            Applied_At TIMESTAMP NOT NULL
        )
        """
    )
    already_applied = conn.execute(
        """
        SELECT Symbol, Rows_Adjusted
        FROM bse_corporate_action_ledger
        WHERE Action_Key = ?
        LIMIT 1
        """,
        [action_key],
    ).fetchone()
    if already_applied:
        conn.close()
        logger.info(
            f"BSE Corporate Action already applied: "
            f"{already_applied[0]} on {dates.pandasDt}"
        )
        return "already_applied", already_applied[0], int(already_applied[1])

    # Try normal symbol, then SME symbol
    try:
        for sym_suffix in ("", "_SME"):
            sym = f"{symbol.upper()}{sym_suffix}"
            df = conn.execute("SELECT * FROM eod_data WHERE Symbol = ? AND Exchange = 'BSE'", (sym,)).fetchdf()

            if df.empty:
                continue

            df['Date'] = pd.to_datetime(df['Date'])
            df = df.set_index("Date")

            # Remove timezone info as DataFrame index is not timezone aware
            dt = dates.dt.replace(tzinfo=None)
            mask_before = df.index < dt
            rows_adjusted = int(mask_before.sum())
            if rows_adjusted == 0:
                logger.warning(
                    f"BSE Corporate Action found {sym}, but it has no price "
                    f"history before {dates.pandasDt}"
                )
                return "no_prior_history", sym, 0

            logger.info(
                f"BSE Adjusting in DuckDB: {sym} by factor "
                f"{adjustmentFactor:.4f} ({rows_adjusted} prior rows)"
            )
            for col in ("Open", "High", "Low", "Close"):
                df.loc[mask_before, col] = (
                    (df.loc[mask_before, col] / adjustmentFactor / 0.05).round()
                    * 0.05
                ).round(2)

            conn.execute("BEGIN TRANSACTION")
            from eod_store import replace_symbol_prices
            adjusted = df.reset_index()
            replace_symbol_prices(conn, "BSE", sym, adjusted)
            conn.execute(
                """
                INSERT INTO bse_corporate_action_ledger
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                [
                    action_key,
                    dates.dt.date(),
                    sym,
                    adjustmentFactor,
                    rows_adjusted,
                ],
            )
            conn.execute("COMMIT")
            return "applied", sym, rows_adjusted
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()

    logger.warning(
        f"BSE Corporate Action symbol not found in price history: "
        f"{symbol.upper()} on {dates.pandasDt}"
    )
    return "symbol_not_found", None, 0


def _adjust_bse_stocks(
    actions_df: pd.DataFrame,
    data_dir: Path,
    dates: Dates,
    logger: logging.Logger,
):
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
        action_type = None
        adjustmentFactor = None

        # BSE lists splits, consolidations and bonuses in Purpose.  A
        # consolidation ratio ("from Rs 1/- to Rs 10/-") parses to a factor
        # below one, which raises the older prices as required.
        if "split" in purpose or "sub-division" in purpose:
            action_type = "split"
            adjustmentFactor = _get_split(symbol, purpose + " " + remarks, logger)

        elif "consolidation" in purpose:
            action_type = "consolidation"
            adjustmentFactor = _get_split(symbol, purpose + " " + remarks, logger)

        elif "bonus" in purpose:
            action_type = "bonus"
            adjustmentFactor = _get_bonus(symbol, purpose + " " + remarks, logger)

        if action_type is None:
            continue

        action_key_source = "|".join(
            [code, dates.pandasDt, str(row["Purpose"]).strip()]
        )
        action_key = hashlib.sha256(
            action_key_source.encode("utf-8")
        ).hexdigest()
        if adjustmentFactor:
            status, _, rows_adjusted = _make_adjustment(
                symbol,
                adjustmentFactor,
                data_dir,
                dates,
                logger,
                action_key=action_key,
            )
        else:
            status, rows_adjusted = "factor_not_parsed", 0
        logger.info(
            f"BSE Corporate Action: {symbol} {action_type} "
            f"factor={adjustmentFactor} status={status} "
            f"rows={rows_adjusted}"
        )


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

    # Determine date range to download corporate actions
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
                start_date=dates.dt + timedelta(days=1),
                end_date=datetime.now(tz_IN),
                logger=logger
            )
            relevant_actions = actions_df["Purpose"].astype(str).str.contains(
                "split|sub-division|consolidation|bonus",
                case=False,
                regex=True,
                na=False,
            ).sum() if not actions_df.empty else 0
            logger.info(
                f"BSE Corporate Action download summary: {len(actions_df)} total, "
                f"{relevant_actions} split/consolidation/bonus actions"
            )
        except Exception as e:
            logger.error(
                f"Failed to download BSE corporate actions: {e}. "
                f"Sync stopped so its checkpoint cannot skip adjustments."
            )
            return False

    # Main Day-by-Day Loop
    session = requests.Session()
    session.headers.update(headers)
    
    while dates.nextDate():
        curDt = dates.dt.strftime("%d-%b-%Y")
        is_weekend = dates.dt.weekday() in (5, 6)
        is_holiday = curDt in holidays
        is_special = is_special_session(dates.dt.replace(tzinfo=None), special_sessions)

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

        # Step 1: Corporate Actions Adjustments (Splits/Bonuses)
        if not actions_df.empty:
            try:
                _adjust_bse_stocks(actions_df, data_dir, dates, logger)
            except Exception as e:
                logger.error(f"Error applying corporate actions for {dates.pandasDt}: {e}")
                return False

        # Step 2: Download Bhavcopy & Deliverables
        bhav_date_str = dates.pandasDt.replace("-", "")
        bhavcopy_url = f"https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{bhav_date_str}_F_0000.CSV"
        
        delv_date_str = dates.dt.strftime("%d%m")
        deliverables_url = f"https://www.bseindia.com/BSEDATA/gross/{dates.dt.year}/SCBSEALL{delv_date_str}.zip"

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


        # Load both DataFrames
        try:
            df_bhav = pd.read_csv(BytesIO(r_bhav.content))
            df_bhav.columns = [c.strip() for c in df_bhav.columns]
            
            # Save raw daily bhavcopy
            raw_bhav_file = daily_bhavcopy_dir / f"BhavCopy_BSE_CM_{bhav_date_str}.csv"
            raw_bhav_file.write_bytes(r_bhav.content)
            
            if delivery_status == 200:
                with zipfile.ZipFile(BytesIO(r_delv.content)) as zf:
                    with zf.open(zf.namelist()[0]) as f_delv:
                        raw_delv_bytes = f_delv.read()

                df_delv = pd.read_csv(BytesIO(raw_delv_bytes), sep="|", dtype=str)
                df_delv.columns = [c.strip() for c in df_delv.columns]
            else:
                df_delv = pd.DataFrame(columns=["SCRIP CODE", "DELIVERY QTY"], dtype=str)
            
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
        clear_session_attempts(meta, dates.dt.date())
        meta["lastUpdate"] = dates.dt.isoformat()
        _write_meta(meta_file, meta)

    logger.info("BSE EOD Sync Pipeline completed successfully!")
    return True
