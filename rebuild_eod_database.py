"""Build the price store from NSE's and BSE's own daily files: the last two years, raw, then the factors.

    python rebuild_eod_database.py                       # build beside the live file, then swap it in
    python rebuild_eod_database.py --data-root "/path/to/Mark EMA and HTF"
    python rebuild_eod_database.py --fill-missing        # add the sessions the existing store lacks, in place

Run it once to leave the old store (version 1, whose prices were divided in
place and in places wrongly: POCL three times, ACUTAAS's BSE split never), or
any time to start clean; run_screener.py runs it by itself when it finds a
version 1 file. For every session of the last two years it downloads NSE's
UDiFF bhavcopy and delivery file (sec_bhavdata_full) and BSE's bhavcopy and
delivery file (SCBSEALL), stores them as published, fetches both exchanges'
corporate-action lists for the window, decides the factors
(price_adjustments.py), and only then swaps the new file in. The old one is
kept beside it (NSE_BSE_EOD.v1-backup.duckdb, or .previous.duckdb).

Sessions are every weekday, the special sessions known (meta.json's,
screening_readiness.KNOWN_SPECIAL_SESSIONS, Muhurat) and every other weekend
day NSE has a bhavcopy for: each weekend day is asked of NSE (one request), so
a Saturday session no calendar knew -- 2025-02-01, the Union Budget, was
missing from the first rebuild on both exchanges -- is still found. A file
whose trade date is another day is not used.

--fill-missing works on the existing store: the sessions it lacks on either
exchange (screening_readiness.find_missing_sessions over the whole stored
range) and every day neither exchange stored that NSE turns out to have a file
for are downloaded with the same cached per-day code, stored under the
symbols the store has today (matched by ISIN, BSE also by scrip code -- an old
day never renames anything), and the factors are decided again.

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
from io import BytesIO
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
from screening_readiness import _read_calendar, find_missing_sessions, is_special_session_day, latest_eligible_eod_date

RETRY_WAITS = (5, 15, 45)
BSE_PAUSE = 1.0


@dataclass
class Day:
    """What the per-day store functions read from their `dates` argument."""
    dt: datetime

    @property
    def pandasDt(self) -> str:
        return self.dt.strftime("%Y-%m-%d")


def sessions(start: date, end: date, special: set[date], holidays: dict | None = None) -> list[date]:
    """Every weekday in [start, end] and the special sessions (incl. Muhurat): holidays show as days without files."""
    days = [start + timedelta(n) for n in range((end - start).days + 1)]
    return [d for d in days if d.weekday() < 5 or is_special_session_day(d, holidays or {}, special)]


def weekend_probes(start: date, end: date, special: set[date], holidays: dict | None = None) -> list[date]:
    """The weekend days of [start, end] not known as sessions: each is asked of NSE once, in case it was one."""
    known = set(sessions(start, end, special, holidays))
    days = [start + timedelta(n) for n in range((end - start).days + 1)]
    return [d for d in days if d not in known]


def trade_days(content, day: date) -> bool:
    """True unless the bhavcopy's TradDt column names another day (a file served for the wrong date)."""
    try:
        frame = pd.read_csv(BytesIO(content) if isinstance(content, bytes) else content, usecols=["TradDt"], nrows=50)
    except (ValueError, KeyError):
        return True                                     # an older layout without TradDt: nothing to compare
    inside = set(pd.to_datetime(frame["TradDt"].astype(str).str.strip(), errors="coerce").dt.date.dropna())
    return not inside or inside == {day}


def nse_files(nse, day: date, folder: Path, logger) -> tuple[Path | None, Path | None, str]:
    """(bhavcopy csv, delivery csv, note) for one NSE day, from the cache or NSE; no delivery without a bhavcopy."""
    folder.mkdir(parents=True, exist_ok=True)
    bhav = folder / f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv"
    dlv = folder / f"sec_bhavdata_full_{day:%d%m%Y}.csv"
    stamp = datetime.combine(day, datetime.min.time())
    for path, fetch in ((bhav, nse.equityBhavcopy), (dlv, nse.deliveryBhavcopy)):
        if path is dlv and not bhav.is_file():
            break                                       # no session: its delivery file is not asked for
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
    if bhav.is_file() and not trade_days(bhav, day):
        logger.warning(f"NSE bhavcopy for {day} holds another trade date; not used.")
        return None, None, ""
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
    if out[0] is not None and not trade_days(out[0], day):
        return None, None, f"BSE bhavcopy for {day} holds another trade date; not used"
    return out[0], out[1], ""


def _nse_session(nse_factory):
    if nse_factory is None:
        from nse import NSE
        (config.DATA_DIR / "temp").mkdir(parents=True, exist_ok=True)
        nse_factory = lambda: NSE(config.DATA_DIR / "temp", server=True)          # noqa: E731
    return nse_factory()


def _close(nse, logger) -> None:
    try:
        nse.exit()
    except Exception as error:                   # closing the session is best effort
        logger.debug(f"NSE session did not close cleanly: {error}")


def store_day(day: date, bhav: Path | None, dlv: Path | None, bse_bhav: bytes | None, bse_dlv: bytes | None,
              meta_dir: Path, logger) -> None:
    """Store one day's files into config.DUCKDB_FILE with the sync's own per-day code."""
    stamp = Day(datetime.combine(day, datetime.min.time()))
    if bhav is not None:
        isin_file = meta_dir / "isin.csv"
        isin = (pd.read_csv(isin_file, index_col="ISIN") if isin_file.is_file()
                else pd.DataFrame(columns=["ISIN", "SYMBOL"]).set_index("ISIN"))
        update_nse_eod(bhav, dlv, config.DATA_DIR, meta_dir, isin, stamp, logger)
    if bse_bhav is not None:
        update_bse_eod(merge_bse_day(bse_bhav, bse_dlv, day, logger), config.BSE_DATA_DIR, logger, meta_dir, stamp)


def rebuild(logger: logging.Logger, years: float = config.EOD_RETAIN_YEARS, now: datetime | None = None,
            nse_factory=None, get=requests.get, accept_gaps: bool = False) -> dict:
    now = now or datetime.now(config.MARKET_TIMEZONE)
    metadata = config.EOD_METADATA_DIR
    end = latest_eligible_eod_date(now, metadata)
    start = end - timedelta(days=round(365.25 * years))
    holidays, special = _read_calendar(metadata)
    cache = config.DATABASE_DIR / "rebuild_cache"
    scratch_meta = cache / "metadata"
    scratch_meta.mkdir(parents=True, exist_ok=True)
    live = Path(config.DUCKDB_FILE)
    side = live.with_name(live.stem + ".rebuild.duckdb")
    side.unlink(missing_ok=True)
    conn = duckdb.connect(str(side))
    create_schema(conn)
    conn.close()

    nse = _nse_session(nse_factory)
    report = {"sessions": 0, "holidays": [], "nse_missing": [], "bse_missing": [], "failed": [],
              "weekend_sessions": []}
    original_db = config.DUCKDB_FILE
    config.DUCKDB_FILE = side                    # the per-day store functions write to config.DUCKDB_FILE
    try:
        days = sessions(start, end, special, holidays)
        probes = set(weekend_probes(start, end, special, holidays))
        logger.info(f"Rebuilding the price store from exchange files: {start} -> {end}, {len(days)} weekdays "
                    f"and special sessions, {len(probes)} weekend days asked of NSE")
        for n, day in enumerate(sorted(set(days) | probes), 1):
            bhav, dlv, nse_note = nse_files(nse, day, cache / "NSE" / str(day.year), logger)
            if day in probes:
                if bhav is None:                     # an ordinary weekend: BSE is not asked
                    continue
                logger.warning(f"{day} ({day:%A}) has an NSE bhavcopy: a session no calendar listed; stored.")
                report["weekend_sessions"].append(str(day))
            bse_bhav, bse_dlv, bse_note = bse_files(day, cache / "BSE" / str(day.year), get)
            if nse_note:
                report["failed"].append(f"{day} {nse_note}")
            if bse_note:                             # counted as a day BSE lacks: a hole when NSE has it
                logger.warning(bse_note)
            if bhav is None and bse_bhav is None:
                report["holidays"].append(str(day))
                continue
            report["sessions"] += 1
            if bhav is None:
                report["nse_missing"].append(str(day))
            if bse_bhav is None:
                report["bse_missing"].append(str(day))
            store_day(day, bhav, dlv, bse_bhav, bse_dlv, scratch_meta, logger)
            if n % 25 == 0:
                logger.info(f"  {n} days asked, last {day}")
        conn = duckdb.connect(str(side))
        try:
            # The whole history, recorded per exchange when it came (else the daily
            # maintenance asks again, see action_lists.refresh_history_if_due).
            report["action_lists"] = action_lists.refresh_history(
                conn, nse, start - timedelta(days=action_lists.HISTORY_LEAD_DAYS),
                end + timedelta(days=action_lists.LOOK_AHEAD_DAYS), now.date(), logger, get)
            report["prices"] = price_adjustments.rebuild(conn, metadata / "corporate_action_overrides.csv",
                                                         config.DATABASE_DIR / "corporate_action_review.csv", end,
                                                         logger)
            report["raw_rows_dropped"] = apply_retention(conn, years)
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
    finally:
        config.DUCKDB_FILE = original_db
        _close(nse, logger)
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


def _remove_tree(path: Path, inside: Path) -> None:
    """Delete a scratch folder, only one below `inside` (the cache)."""
    if path.exists():
        path.resolve().relative_to(inside.resolve())          # raises for anything outside the cache
        shutil.rmtree(path)


def _quoted(path: Path) -> str:
    return str(path).replace("'", "''")


def backfill(conn, side: Path, exchange: str, day: date) -> tuple[int, int]:
    """Copy `exchange`'s rows of `day` from the side store into `conn`'s raw_prices; returns (stored, unknown).

    Each row goes under the symbol the store has TODAY for its ISIN (BSE: else
    its scrip code; NSE: else the ISIN's first nine characters when only one
    symbol has them), so an old day renames nothing; a security the store
    does not know is left out. The day's rows of those symbols are replaced.
    """
    conn.execute(f"ATTACH '{_quoted(side)}' AS side_day (READ_ONLY)")
    try:
        conn.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE backfill_rows AS
            WITH by_isin AS (SELECT ISIN, min(Symbol) AS Symbol FROM securities WHERE Exchange = $ex GROUP BY ISIN),
                 by_code AS (SELECT Code, min(Symbol) AS Symbol FROM securities
                             WHERE Exchange = $ex AND Code IS NOT NULL GROUP BY Code),
                 by_prefix AS (SELECT left(ISIN, {price_adjustments.ISIN_SECURITY}) AS Prefix, min(Symbol) AS Symbol
                               FROM securities WHERE Exchange = $ex GROUP BY Prefix HAVING count(*) = 1),
                 rows AS (
                    SELECT r.*, COALESCE(i.Symbol, c.Symbol, p.Symbol) AS Target
                    FROM side_day.raw_prices r
                    JOIN side_day.securities s ON s.Exchange = r.Exchange AND s.Symbol = r.Symbol
                    LEFT JOIN by_isin i ON i.ISIN = s.ISIN
                    LEFT JOIN by_code c ON c.Code = s.Code AND $ex = 'BSE'
                    LEFT JOIN by_prefix p ON p.Prefix = left(s.ISIN, {price_adjustments.ISIN_SECURITY}) AND $ex = 'NSE'
                    WHERE r.Exchange = $ex AND r.Date = $day)
            SELECT * FROM rows
            QUALIFY Target IS NULL OR row_number() OVER (PARTITION BY Target ORDER BY COALESCE(Volume, 0) DESC) = 1
            """,
            {"ex": exchange, "day": day},
        )
        stored, unknown = conn.execute(
            "SELECT count(Target), count(*) - count(Target) FROM backfill_rows").fetchone()
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute("DELETE FROM raw_prices WHERE Exchange = $ex AND Date = $day AND Symbol IN "
                         "(SELECT Target FROM backfill_rows WHERE Target IS NOT NULL)", {"ex": exchange, "day": day})
            conn.execute("INSERT INTO raw_prices (Date, Symbol, Exchange, Open, High, Low, Close, Volume, Total_Trades, "
                         "Dlv_Qty) SELECT Date, Target, Exchange, Open, High, Low, Close, Volume, Total_Trades, Dlv_Qty "
                         "FROM backfill_rows WHERE Target IS NOT NULL")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("DROP TABLE backfill_rows")
    finally:
        conn.execute("DETACH side_day")
    return int(stored), int(unknown)


def fill_missing(logger: logging.Logger, now: datetime | None = None, nse_factory=None, get=requests.get) -> dict:
    """Download into the existing store (config.DUCKDB_FILE) the sessions it lacks, then decide the factors again."""
    now = now or datetime.now(config.MARKET_TIMEZONE)
    metadata = config.EOD_METADATA_DIR
    live = Path(config.DUCKDB_FILE)
    conn = duckdb.connect(str(live), read_only=True)
    try:
        if schema_version(conn) != 2:
            raise RuntimeError("the price store is not version 2; run rebuild_eod_database.py first")
        stored = {exchange: set() for exchange in ("NSE", "BSE")}
        for exchange, day in conn.execute("SELECT DISTINCT Exchange, Date FROM raw_prices").fetchall():
            stored.setdefault(exchange, set()).add(day)
    finally:
        conn.close()
    if not stored["NSE"] or not stored["BSE"]:
        raise RuntimeError("the price store holds no NSE or no BSE day to fill between")
    first = min(min(days) for days in stored.values())
    last = max(max(days) for days in stored.values())
    wanted: dict[date, set[str]] = {}
    for exchange, days in find_missing_sessions(live, metadata, lookback_days=None).items():
        for day in days:
            wanted.setdefault(day, set()).add(exchange)
    neither = [first + timedelta(n) for n in range((last - first).days + 1)]
    neither = [d for d in neither if d not in stored["NSE"] and d not in stored["BSE"] and d not in wanted]
    report = {"range": f"{first} -> {last}", "expected_missing": {str(d): sorted(e) for d, e in sorted(wanted.items())},
              "asked_of_nse": len(neither), "found": [], "stored": {}, "unknown_securities": {}, "failed": []}
    logger.info(f"Filling the store {first} -> {last}: {len(wanted)} sessions known missing, "
                f"{len(neither)} days without rows on either exchange asked of NSE")
    cache = config.DATABASE_DIR / "rebuild_cache"
    work = cache / "fill"
    work.mkdir(parents=True, exist_ok=True)
    nse = _nse_session(nse_factory)
    original_db = config.DUCKDB_FILE
    try:
        for day in sorted(set(wanted) | set(neither)):
            need = wanted.get(day, {"NSE", "BSE"})
            bhav, dlv, nse_note = (nse_files(nse, day, cache / "NSE" / str(day.year), logger) if "NSE" in need
                                   else (None, None, ""))
            if day not in wanted:
                if bhav is None:
                    continue                         # a holiday or weekend: nothing to fill
                logger.warning(f"{day} ({day:%A}) has an NSE bhavcopy but no stored rows: filling it.")
                report["found"].append(str(day))
            bse_bhav, bse_dlv, bse_note = (bse_files(day, cache / "BSE" / str(day.year), get) if "BSE" in need
                                           else (None, None, ""))
            for note in (nse_note, bse_note):
                if note:
                    report["failed"].append(f"{day} {note}")
            side = work / f"{day:%Y%m%d}.duckdb"
            side_meta = work / f"{day:%Y%m%d}-metadata"
            side.unlink(missing_ok=True)
            _remove_tree(side_meta, work)
            side_meta.mkdir(parents=True)
            side_conn = duckdb.connect(str(side))
            create_schema(side_conn)
            side_conn.close()
            config.DUCKDB_FILE = side
            try:
                store_day(day, bhav, dlv, bse_bhav, bse_dlv, side_meta, logger)
            finally:
                config.DUCKDB_FILE = original_db
            conn = duckdb.connect(str(live))
            try:
                for exchange, present in (("NSE", bhav is not None), ("BSE", bse_bhav is not None)):
                    if not present:
                        if exchange in need:
                            report["failed"].append(f"{day} {exchange}: no file")
                        continue
                    stored_rows, unknown = backfill(conn, side, exchange, day)
                    report["stored"][f"{day} {exchange}"] = stored_rows
                    if unknown:
                        report["unknown_securities"][f"{day} {exchange}"] = unknown
                    logger.info(f"{day} {exchange}: {stored_rows} rows stored, {unknown} of securities the store "
                                "does not know left out")
            finally:
                conn.close()
            side.unlink(missing_ok=True)
            _remove_tree(side_meta, work)
        conn = duckdb.connect(str(live))
        try:
            report["prices"] = price_adjustments.rebuild(conn, metadata / "corporate_action_overrides.csv",
                                                         config.DATABASE_DIR / "corporate_action_review.csv",
                                                         now.date(), logger)
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
    finally:
        config.DUCKDB_FILE = original_db
        _close(nse, logger)
    report["still_missing"] = {exchange: [str(d) for d in days] for exchange, days in
                               find_missing_sessions(live, metadata, lookback_days=None).items()}
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
    ap.add_argument("--fill-missing", action="store_true",
                    help="add the sessions the existing store lacks (in place) instead of building a new store")
    args = ap.parse_args(argv)
    if args.data_root:
        config.configure_data_root(args.data_root)
    config.ensure_dirs()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.fill_missing:
        report = fill_missing(logging.getLogger("fill-missing"))
        print(json.dumps(report, indent=1, default=str))
        sys.exit(1 if report["failed"] or report["still_missing"] else 0)
    report = rebuild(logging.getLogger("rebuild"), args.years, accept_gaps=args.accept_gaps)
    print(json.dumps({k: (v if not isinstance(v, list) else f"{len(v)}: {v[:5]}") for k, v in report.items()},
                     indent=1, default=str))
    sys.exit(0 if report.get("swapped") else 1)


if __name__ == "__main__":
    main()
