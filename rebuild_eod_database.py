"""Build the price store from NSE's and BSE's own daily files: the last two years, raw, then the factors.

    python rebuild_eod_database.py                       # build beside the live file, then swap it in
    python rebuild_eod_database.py --data-root "/path/to/Mark EMA and HTF"

Run it once to leave the old store (version 1, whose prices were divided in
place and in places wrongly: POCL three times, ACUTAAS's BSE split never), or
any time to start clean; run_screener.py runs it by itself when it finds a
version 1 file. For every session of the last two years it downloads NSE's
UDiFF bhavcopy and delivery file (sec_bhavdata_full) and BSE's bhavcopy and
delivery file (SCBSEALL), stores them as published, fetches both exchanges'
corporate-action lists for the window, decides the factors
(price_adjustments.py), and only then swaps the new file in. The old one is
kept beside it (NSE_BSE_EOD.v1-backup.duckdb, or .previous.duckdb).

About 500 sessions x 4 files: 30-40 minutes at a polite pace. Downloads are
cached in Database/rebuild_cache, so an interrupted run goes on where it
stopped. A day with no file on either exchange is a holiday; a day only one
exchange published is reported, and the swap does not happen while NSE lacks a
day BSE traded (rerun later: the cache keeps what came).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import requests

import action_lists
import config
import price_adjustments
from bse_eod_downloader.eod_downloader import BHAVCOPY_URL, DELIVERY_URL, headers as BSE_HEADERS, merge_bse_day
from bse_eod_downloader.stock_data import update_bse_eod
from eod_store import apply_retention, create_schema, schema_version
from nse_eod_downloader.stock_data import update_nse_eod
from screening_readiness import _read_calendar, latest_eligible_eod_date

RETRY_WAITS = (5, 15, 45)
BSE_PAUSE = 1.0


@dataclass
class Day:
    """What the per-day store functions read from their `dates` argument."""
    dt: datetime

    @property
    def pandasDt(self) -> str:
        return self.dt.strftime("%Y-%m-%d")


def sessions(start: date, end: date, special: set[date]) -> list[date]:
    """Every weekday in [start, end] and the special weekend sessions: holidays show as days without files."""
    days = [start + timedelta(n) for n in range((end - start).days + 1)]
    return [d for d in days if d.weekday() < 5 or d in special]


def nse_files(nse, day: date, folder: Path, logger) -> tuple[Path | None, Path | None, str]:
    """(bhavcopy csv, delivery csv, note) for one NSE day, from the cache or NSE."""
    folder.mkdir(parents=True, exist_ok=True)
    bhav = folder / f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv"
    dlv = folder / f"sec_bhavdata_full_{day:%d%m%Y}.csv"
    stamp = datetime.combine(day, datetime.min.time())
    for path, fetch in ((bhav, nse.equityBhavcopy), (dlv, nse.deliveryBhavcopy)):
        if path.is_file() and path.stat().st_size > 0:
            continue
        for wait in (*RETRY_WAITS, None):
            try:
                fetch(stamp, folder=folder)
                break
            except RuntimeError:                     # NSE answered with a page: no file that day
                break
            except Exception as error:               # dropped or refused: back off, then give up on the day
                if wait is None:
                    return None, None, f"NSE failed: {error}"
                time.sleep(wait)
    return (bhav if bhav.is_file() else None), (dlv if dlv.is_file() else None), ""


def bse_files(day: date, folder: Path, get) -> tuple[bytes | None, bytes | None, str]:
    """(bhavcopy bytes, delivery zip bytes, note) for one BSE day, from the cache or BSE."""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for name, url in ((f"BhavCopy_BSE_CM_{day:%Y%m%d}.csv", BHAVCOPY_URL), (f"SCBSEALL{day:%d%m%Y}.zip", DELIVERY_URL)):
        path = folder / name
        if path.is_file() and path.stat().st_size > 0:
            out.append(path.read_bytes())
            continue
        body, note = None, ""
        for wait in (*RETRY_WAITS, None):
            status = None
            try:
                time.sleep(BSE_PAUSE)
                response = get(url.format(day=day), headers=BSE_HEADERS, timeout=30)
                status = response.status_code
                if status == 200 and response.content and not response.content.lstrip().startswith(b"<"):
                    body = response.content
                    path.write_bytes(body)
            except requests.RequestException as error:
                note = str(error)
            if status in (200, 404) or wait is None:    # BSE answered (a holiday is a 404), or no more tries
                break
            time.sleep(wait)
        out.append(body)
    return out[0], out[1], ""


def rebuild(logger: logging.Logger, years: float = config.EOD_RETAIN_YEARS, now: datetime | None = None,
            nse_factory=None, get=requests.get, accept_gaps: bool = False) -> dict:
    now = now or datetime.now(config.MARKET_TIMEZONE)
    metadata = config.EOD_METADATA_DIR
    end = latest_eligible_eod_date(now, metadata)
    start = end - timedelta(days=round(365.25 * years))
    _, special = _read_calendar(metadata)
    cache = config.DATABASE_DIR / "rebuild_cache"
    scratch_meta = cache / "metadata"
    scratch_meta.mkdir(parents=True, exist_ok=True)
    live = Path(config.DUCKDB_FILE)
    side = live.with_name(live.stem + ".rebuild.duckdb")
    side.unlink(missing_ok=True)
    conn = duckdb.connect(str(side))
    create_schema(conn)
    conn.close()

    if nse_factory is None:
        from nse import NSE
        (config.DATA_DIR / "temp").mkdir(parents=True, exist_ok=True)
        nse_factory = lambda: NSE(config.DATA_DIR / "temp", server=True)          # noqa: E731
    nse = nse_factory()
    report = {"sessions": 0, "holidays": [], "nse_missing": [], "bse_missing": [], "failed": []}
    original_db = config.DUCKDB_FILE
    config.DUCKDB_FILE = side                    # the per-day store functions write to config.DUCKDB_FILE
    try:
        days = sessions(start, end, special)
        logger.info(f"Rebuilding the price store from exchange files: {start} -> {end}, {len(days)} weekdays")
        for n, day in enumerate(days, 1):
            bhav, dlv, nse_note = nse_files(nse, day, cache / "NSE" / str(day.year), logger)
            bse_bhav, bse_dlv, _ = bse_files(day, cache / "BSE" / str(day.year), get)
            if nse_note:
                report["failed"].append(f"{day} {nse_note}")
            if bhav is None and bse_bhav is None:
                report["holidays"].append(str(day))
                continue
            report["sessions"] += 1
            if bhav is None:
                report["nse_missing"].append(str(day))
            else:
                isin_file = scratch_meta / "isin.csv"
                isin = (pd.read_csv(isin_file, index_col="ISIN") if isin_file.is_file()
                        else pd.DataFrame(columns=["ISIN", "SYMBOL"]).set_index("ISIN"))
                update_nse_eod(bhav, dlv, config.DATA_DIR, scratch_meta, isin, Day(datetime.combine(day, datetime.min.time())), logger)
            if bse_bhav is None:
                report["bse_missing"].append(str(day))
            else:
                merged = merge_bse_day(bse_bhav, bse_dlv, day, logger)
                update_bse_eod(merged, config.BSE_DATA_DIR, logger, scratch_meta, Day(datetime.combine(day, datetime.min.time())))
            if n % 25 == 0:
                logger.info(f"  {n}/{len(days)} days, last {day}")
        conn = duckdb.connect(str(side))
        try:
            action_lists.refresh(conn, nse, start - timedelta(days=10), end + timedelta(days=action_lists.LOOK_AHEAD_DAYS), logger, get)
            report["prices"] = price_adjustments.rebuild(conn, metadata / "corporate_action_overrides.csv",
                                                         config.DATABASE_DIR / "corporate_action_review.csv", end)
            report["raw_rows_dropped"] = apply_retention(conn, years)
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
    finally:
        config.DUCKDB_FILE = original_db
        try:
            nse.exit()
        except Exception:
            pass
    # A day one exchange traded and the other did not answer is a hole, not a holiday
    # (BSE refuses an address for a while after many requests): keep the old file.
    holes = sorted(set(report["nse_missing"]) ^ set(report["bse_missing"]))
    report["holes"] = holes
    if (report["failed"] or holes) and not accept_gaps:
        report["swapped"] = False
        logger.error(f"Rebuild left gaps (days one exchange lacks: {holes[:5]}, failures {report['failed'][:3]}); "
                     "the old store stays in place. Run again later: downloaded files are cached.")
        return report
    swap_in(side, live, end, scratch_meta, metadata, logger)
    report["swapped"] = True
    return report


def swap_in(side: Path, live: Path, last: date, scratch_meta: Path, metadata: Path, logger) -> None:
    """The new file in place of the live one (kept as a backup); the sync cursors on the last day built."""
    if live.exists():
        old = duckdb.connect(str(live), read_only=True)
        version = schema_version(old)
        old.close()
        backup = live.with_name(live.stem + (".v1-backup" if version == 1 else ".previous") + live.suffix)
        live.replace(backup)
        logger.info(f"Old price store kept as {backup.name}")
    side.replace(live)
    for name in ("meta.json", "bse_meta.json", "isin.csv", "bse_isin.csv"):       # the old cursors, kept
        if (metadata / name).is_file():
            shutil.copy2(metadata / name, metadata / f"{name}.before-rebuild")
    cursor = datetime.combine(last, datetime.min.time()).replace(tzinfo=config.MARKET_TIMEZONE).isoformat()
    for name in ("meta.json", "bse_meta.json"):
        path = metadata / name
        meta = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        for stale in ("equityActions", "smeActions", "mfActions", "equityActionsExpiry", "smeActionsExpiry",
                      "mfActionsExpiry", "missingSessionAttempts"):
            meta.pop(stale, None)
        meta["lastUpdate"] = cursor
        path.write_text(json.dumps(meta, indent=3), encoding="utf-8")
    for name in ("isin.csv", "bse_isin.csv"):
        if (scratch_meta / name).is_file():
            shutil.copy2(scratch_meta / name, metadata / name)
    logger.info(f"New price store in place: {live.name}, last session {last}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-root", type=Path, help="override the default ~/Mark EMA and HTF data root")
    ap.add_argument("--years", type=float, default=config.EOD_RETAIN_YEARS)
    ap.add_argument("--accept-gaps", action="store_true",
                    help="swap the new store in even when one exchange lacks days the other traded")
    args = ap.parse_args(argv)
    if args.data_root:
        config.configure_data_root(args.data_root)
    config.ensure_dirs()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = rebuild(logging.getLogger("rebuild"), args.years, accept_gaps=args.accept_gaps)
    print(json.dumps({k: (v if not isinstance(v, list) else f"{len(v)}: {v[:5]}") for k, v in report.items()},
                     indent=1, default=str))
    sys.exit(0 if report.get("swapped") else 1)


if __name__ == "__main__":
    main()
