#!/usr/bin/env python
"""Unified Mark screener pipeline — single command runs the whole workflow.

Replaces ``run_complete_screening.sh`` + the three ``.sh`` wrappers with one
Python orchestrator that:

  Step 1 — updates the eod2 price database (via the existing run_eod2.sh).
  Step 2 — downloads fresh fundamentals from screener.in over HTTP.
  Steps 3-4 — launches one Rust process for every local operation: technical
  screening, fundamental join, ranking, CSV/watchlist output and charts.

Improvements over the legacy shell orchestrator:
  * Real error propagation — a non-zero exit from any step aborts the run.
    (The old wrappers always reported success, masking failures.)
  * Fundamentals validation — verifies all-company.csv exists, is non-empty
    and looks like the expected export before screening consumes it.
  * Per-step timing and a final summary, all tee'd to a timestamped log file.

Usage:
    python run_screener.py

Windows and Linux launchers create/use a project-local virtual environment;
the normal precompiled-Rust path does not require TA-Lib.
"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import config
import run_fingerprint
from screening_readiness import (
    LocalDataError,
    find_missing_sessions,
    inspect_eod_readiness,
    rebase_sync_cursor,
)
from config import CURRENT_TIMESTAMP

# Exit codes a scheduler acts on (run_scheduled.sh retries both later that evening):
# the run screened data that is not current (sync stopped, data older than the
# target, NSE/BSE as-of differ) -- only reported when MARK_SCHEDULED=1, so a
# manual run still exits 0 -- and another run holding the run lock.
EXIT_DATA_NOT_CURRENT = 3
EXIT_BUSY = 4
# --verify: the stored prices differed from a full rebuild and were repaired (5), or the verify failed (6).
EXIT_VERIFY_REPAIRED = 5
EXIT_VERIFY_FAILED = 6
# A run lock older than this is stale whatever its process.
RUN_LOCK_MAX_AGE_SECONDS = 6 * 3600

# ANSI colours for the console (log file gets plain text via strip).
_C = {
    'RESET': '\033[0m', 'BOLD': '\033[1m',
    'RED': '\033[31m', 'GREEN': '\033[32m',
    'YELLOW': '\033[33m', 'CYAN': '\033[36m', 'BLUE': '\033[34m',
}


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def setup_logging():
    """Configure concise application logging for both console and file.

    The previous DEBUG root logger also captured low-level HTTP/HPACK transport
    traces, making an ordinary run produce thousands of mostly useless lines.
    Keep operational messages at INFO and suppress dependency internals unless
    a dependency reports a warning or error.
    """
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # idempotent if re-imported

    fmt = logging.Formatter('%(asctime)s  %(levelname)-7s  %(message)s',
                            datefmt='%H:%M:%S')

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Guard against an unexpected dependency becoming verbose again.  A run
    # normally stays far below this limit; rotated files remain available for
    # post-mortem debugging without allowing one run to fill the disk.
    fh = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=2 * 1024 * 1024,
        backupCount=2,
        encoding='utf-8',
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    for noisy_logger in (
        'httpx', 'httpcore', 'hpack', 'urllib3', 'websocket', 'websockets',
    ):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    return logger


def color(text, name):
    """Wrap text in an ANSI colour (no-op if name missing)."""
    return f"{_C.get(name, '')}{text}{_C['RESET']}"


# ---------------------------------------------------------------------------
# Step runner with strict error propagation
# ---------------------------------------------------------------------------
def run_step(name, cmd, logger, cwd=None):
    """Run a subprocess, stream its output, abort the pipeline on failure.

    Unlike the legacy shell wrappers (which printed "complete" regardless of
    exit code), this raises SystemExit on any non-zero return so the next step
    never runs against stale/missing data.
    """
    logger.info(color(f"▶ {name}", 'BOLD'))
    logger.info(f"  command: {' '.join(str(c) for c in cmd)}")
    t0 = time.time()

    try:
        # Start the process with stdout/stderr piped so we can capture and log it
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            cwd=cwd
        )
        
        # Read the output line by line as it is printed and log it
        for line in proc.stdout:
            logger.info(line.rstrip('\n'))
            
        proc.wait()
    except FileNotFoundError as e:
        logger.error(color(f"  ✗ executable not found: {e}", 'RED'))
        sys.exit(1)

    elapsed = time.time() - t0
    if proc.returncode != 0:
        logger.error(color(
            f"  ✗ {name} FAILED (exit {proc.returncode}) after {elapsed:.1f}s",
            'RED'))
        logger.error(color("  Aborting pipeline — fix the error above and re-run.",
                           'RED'))
        sys.exit(proc.returncode)

    logger.info(color(f"  ✓ completed in {elapsed:.1f}s", 'GREEN'))
    return elapsed


# ---------------------------------------------------------------------------
# Step 2 validation — verify the fundamentals download actually produced data
# ---------------------------------------------------------------------------
def validate_fundamentals(logger):
    """Sanity-check all-company.csv before the screener reads it.

    The old pipeline would silently screen against a missing/empty file.
    Catch that here.
    """
    fundamental_file = config.FUNDAMENTAL_FILE
    if not fundamental_file.exists():
        logger.error(color(f"  ✗ fundamentals missing: {fundamental_file}", 'RED'))
        logger.error(color("    Did the screener.in download fail? Aborting.", 'RED'))
        sys.exit(1)

    size = fundamental_file.stat().st_size
    if size < 1000:
        logger.error(color(f"  ✗ fundamentals file too small ({size} bytes) — "
                           "likely an error page, not the export.", 'RED'))
        sys.exit(1)

    with open(fundamental_file, 'r', encoding='utf-8', errors='replace') as f:
        header = f.readline().strip()
    # Columns required for ISIN joining and the chart information panel.
    expected_cols = [
        'ISIN Code', 'Name', 'Industry', 'Current Price',
        'Market Capitalization', 'Return on capital employed',
        'Return on equity',
    ]
    missing = [c for c in expected_cols if c not in header]
    if missing:
        logger.error(color(f"  ✗ header missing expected columns: {missing}", 'RED'))
        logger.error(f"    first line: {header[:120]}")
        sys.exit(1)

    mtime = datetime.fromtimestamp(fundamental_file.stat().st_mtime)
    age_min = (datetime.now() - mtime).total_seconds() / 60
    logger.info(color(f"  ✓ {fundamental_file.name} OK "
                      f"({size/1024:.0f} KB, modified {mtime:%H:%M:%S}, "
                      f"{age_min:.0f} min ago)", 'GREEN'))


def describe_rust_binary(binary: Path, info: dict | None = None) -> str:
    """Identify the Rust engine so runs on different machines can be compared.

    The log line keeps a 16-character checksum; the run fingerprint JSON has
    the full sha256.
    """
    info = info or run_fingerprint.engine_info(binary)
    return f"{info['version']} | sha256 {info['sha256'][:16]} | {info['path']}"


def _rename_downloaded_fundamentals(logger, elapsed: float) -> None:
    """Move the downloader's fixed-name all-company.csv to today's date-based name."""
    logger.info(color(f"  ✓ download completed in {elapsed:.1f}s", 'GREEN'))
    temp_file = config.FUNDAMENTAL_DIR / "all-company.csv"
    if not temp_file.exists():
        logger.error(color(f"  ✗ Downloaded file not found at {temp_file}", 'RED'))
        sys.exit(1)
    try:
        # Path.replace() atomically overwrites an existing target across
        # platforms (unlike rename, which can fail on some OSes).
        temp_file.replace(config.FUNDAMENTAL_FILE)
        logger.info(color(f"  ✓ Renamed downloaded file to {config.FUNDAMENTAL_FILE.name}", 'GREEN'))
    except Exception as e:
        logger.error(color(f"  ✗ Failed to rename downloaded file to date-based name: {e}", 'RED'))
        sys.exit(1)


def fundamentals_taken_before_close(path: Path, session) -> bool:
    """True when `path` was written before `session`'s close (18:00 IST).

    Screener.in's "Current Price" is the live price, so a file downloaded during
    the day carries intraday prices that the EOD data (and a file taken after
    the close) do not (2026-11-05: Windows downloaded at 12:50, the Mac at 19:30).
    """
    from screening_readiness import EOD_READY_TIME

    close = datetime.combine(session, EOD_READY_TIME, tzinfo=config.MARKET_TIMEZONE)
    written = datetime.fromtimestamp(path.stat().st_mtime, config.MARKET_TIMEZONE)
    return written < close


def consistency_warning(logger, warnings: list, message: str) -> None:
    """Log a result-changing data condition prominently; the final summary repeats it.

    Two machines screening different sessions produce different lists (one
    session flipped 35 of 293 qualifiers in the 2026-10 comparison), so this
    must not hide among ordinary progress lines.
    """
    if message in warnings:
        return
    warnings.append(message)
    banner = '!' * 64
    logger.warning(color(banner, 'YELLOW'))
    logger.warning(color(f"  RESULTS MAY DIFFER: {message}", 'YELLOW'))
    logger.warning(color(banner, 'YELLOW'))


def _pid_alive(pid: int) -> bool:
    """Whether a process exists on this machine (never signals or stops it)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) would terminate the process on Windows.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_lock(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _stale_lock_reason(path: Path, holder) -> str | None:
    """Why an existing run lock can be taken over, or None while its run may be alive."""
    if not isinstance(holder, dict):
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return "lock file vanished"
        return f"unreadable and {age / 3600:.1f} h old" if age > RUN_LOCK_MAX_AGE_SECONDS else None
    age = time.time() - float(holder.get("started", 0) or 0)
    if age > RUN_LOCK_MAX_AGE_SECONDS:
        return f"started {age / 3600:.1f} h ago"
    if holder.get("host") == platform.node():
        try:
            pid = int(holder.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0
        if not _pid_alive(pid):
            return f"process {pid} is gone"
    return None


def acquire_run_lock(logger, path: Path | None = None):
    """Take the run lock (an O_EXCL file holding PID and start time); None when another run holds it."""
    path = Path(path or config.RUN_LOCK_FILE)
    record = {
        "pid": os.getpid(),
        "host": platform.node(),
        "started": time.time(),
        "started_ist": datetime.now(config.MARKET_TIMEZONE).isoformat(timespec="seconds"),
        "run": config.CURRENT_TIMESTAMP,
        "token": uuid.uuid4().hex,
    }
    for attempt in (1, 2):
        try:
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = _read_lock(path)
            reason = _stale_lock_reason(path, holder)
            if reason and attempt == 1:
                logger.warning(f"  Removing a stale run lock ({reason}): {path}")
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            holder = holder if isinstance(holder, dict) else {}
            logger.error(color(
                f"  ✗ Another Mark Screener run is in progress (pid {holder.get('pid', '?')} on "
                f"{holder.get('host', '?')}, run {holder.get('run', '?')}, started "
                f"{holder.get('started_ist', '?')}). Not starting a second one; try again when it "
                f"has finished. Lock: {path}",
                'RED',
            ))
            return None
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        return {"path": path, "token": record["token"]}
    return None


def release_run_lock(lock) -> None:
    """Remove the run lock if it is still this run's."""
    if not lock:
        return
    holder = _read_lock(lock["path"])
    if isinstance(holder, dict) and holder.get("token") == lock["token"]:
        try:
            lock["path"].unlink()
        except OSError:
            pass


def write_run_fingerprint(logger, fingerprint: dict | None) -> None:
    """Write the fingerprint JSON; any failure is only a warning."""
    if fingerprint is None:
        return
    try:
        path = run_fingerprint.write(fingerprint)
        logger.info(f"  Run fingerprint written: {path}")
    except Exception as error:  # noqa: BLE001 - never stop the run for a fingerprint
        logger.warning(f"  Run fingerprint could not be written: {error}")


def report_missing_sessions(logger):
    """Warn about trading sessions absent from the local database, over the whole retained range."""
    try:
        missing = find_missing_sessions(config.DUCKDB_FILE, config.EOD_METADATA_DIR, lookback_days=None)
    except LocalDataError as error:
        logger.warning(f"  Missing-session check skipped: {error}")
        return
    for exchange, days in missing.items():
        shown = ", ".join(f"{day:%Y-%m-%d}" for day in days[:10])
        more = f" (+{len(days) - 10} more)" if len(days) > 10 else ""
        logger.warning(color(
            f"  {exchange} price data is missing {len(days)} session(s): "
            f"{shown}{more}. Results can differ from a machine that has them; "
            "`python rebuild_eod_database.py --fill-missing` adds them.",
            'YELLOW',
        ))


def run_all_screening(logger, include_sme=False, only_sme=False):
    """Run every post-download operation in one Rust process."""
    if not config.RUST_BINARY.exists():
        logger.error(color(
            f"  ✗ Rust pipeline binary not found: {config.RUST_BINARY}",
            "RED",
        ))
        sys.exit(1)

    cmd = [
        str(config.RUST_BINARY),
        "pipeline",
        str(config.DUCKDB_FILE),
        str(config.FUNDAMENTAL_FILE),
        str(config.OUT_DIR),
        str(config.CHART_DIR),
        config.CURRENT_TIMESTAMP,
        str(config.OPENTRADER_ROOT),
        "--flag-lookback",
        str(config.FLAG_LOOKBACK_DAYS),
        "--max-stale-sessions",
        str(config.MAX_STALE_SESSIONS),
    ]
    if include_sme:
        cmd.append("--include-sme")
    if only_sme:
        cmd.append("--only-sme")
    if not config.WATCHLISTS_ENABLED:
        cmd.append("--no-watchlists")
    if config.STRIP_SME_WATCHLIST_SUFFIX:
        cmd.append("--strip-sme-suffix")

    run_step(
        "Steps 3-4/4: Local Rust screening, outputs and charts",
        cmd,
        logger,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def ensure_current_store(logger) -> bool:
    """True when the price store is version 2 or 3 (a version 2 store is migrated by the maintenance step);
    an old or missing one is rebuilt from exchange files first."""
    import duckdb
    from eod_store import schema_version

    if config.DUCKDB_FILE.exists():
        conn = duckdb.connect(str(config.DUCKDB_FILE), read_only=True)
        try:
            if schema_version(conn) in (2, 3):
                return True
        finally:
            conn.close()
    logger.info(color(
        "  One-time rebuild of the price store from NSE/BSE files (two years, about 30-40 minutes); "
        "the old store is kept as a backup.", 'CYAN'))
    import rebuild_eod_database
    report = rebuild_eod_database.rebuild(logger)
    if not report.get("swapped"):
        logger.warning(f"  Rebuild incomplete ({len(report.get('holes', []))} days missing on one exchange); "
                       "it continues on the next run from its cache.")
    return bool(report.get("swapped"))


def build_argument_parser():
    """Build the portable command-line interface used by every launcher."""
    import argparse

    parser = argparse.ArgumentParser(description="Unified Mark Screener Orchestrator")
    sme_group = parser.add_mutually_exclusive_group()
    sme_group.add_argument(
        '--include-sme',
        action='store_true',
        help="Include SME stocks in screening",
    )
    sme_group.add_argument(
        '--only-sme',
        action='store_true',
        help="Screen ONLY SME stocks",
    )
    parser.add_argument(
        '--data-root',
        type=Path,
        help="Override the default ~/Mark EMA and HTF data root",
    )
    parser.add_argument(
        '--watchlist-root',
        type=Path,
        help="Override the default ~/.opentrader-pro root",
    )
    parser.add_argument(
        '--watchlists',
        action=argparse.BooleanOptionalAction,
        default=config.WATCHLISTS_ENABLED,
        help="Write OpenTrader Pro watchlists (default: enabled)",
    )
    parser.add_argument(
        '--strip-sme-watchlist-suffix',
        action='store_true',
        default=config.STRIP_SME_WATCHLIST_SUFFIX,
        help="Remove the internal _SME suffix from OpenTrader watchlist symbols",
    )
    parser.add_argument(
        '--allow-stale-fundamentals',
        action='store_true',
        help="Use the newest older local fundamentals when today's file is absent",
    )
    parser.add_argument(
        '--skip-sync',
        action='store_true',
        help="Never download or adjust EOD data here: the database is generated "
             "elsewhere (the market-data lake's lake.export_mark) and is screened as it is",
    )
    parser.add_argument(
        '--verify',
        action='store_true',
        help="Only compare the price store with a full rebuild from its raw prices and repair it on a "
             f"difference, then exit (0 equal, {EXIT_VERIFY_REPAIRED} repaired, {EXIT_VERIFY_FAILED} failed)",
    )
    return parser


def main():
    args = build_argument_parser().parse_args()

    if args.data_root:
        config.configure_data_root(args.data_root)
    config.configure_watchlists(
        args.watchlists,
        args.watchlist_root,
        strip_sme_suffix=args.strip_sme_watchlist_suffix,
    )
    config.FUNDAMENTAL_FILE = config.select_fundamental_file(
        allow_stale=args.allow_stale_fundamentals
    )
    # Logging and result writers expect their parent directories to exist.
    # Creating them here also makes the first run identical on every OS.
    config.ensure_dirs()

    os.environ['MARK_SCREENER_TIMESTAMP'] = config.CURRENT_TIMESTAMP
    logger = setup_logging()
    lock = acquire_run_lock(logger)
    if lock is None:
        sys.exit(EXIT_BUSY)
    try:
        code = run_verify(logger) if args.verify else run_pipeline(args, logger)
    finally:
        release_run_lock(lock)
    if code:
        sys.exit(code)


def run_verify(logger) -> int:
    """--verify under the run lock: the full verify and repair of the price store; the exit code."""
    logger.info(color("▶ Price store verify: every stored price and decision against a full rebuild", 'BOLD'))
    try:
        from eod_maintenance import verify_store
        result = verify_store(logger)
    except Exception as error:  # noqa: BLE001 - reported as the exit code
        logger.error(color(f"  ✗ Verify failed: {error}", 'RED'))
        return EXIT_VERIFY_FAILED
    if result.get("status") == "ok":
        logger.info(color(f"  ✓ The price store equals a full rebuild ({result.get('seconds')}s).", 'GREEN'))
        return 0
    tables = ", ".join(f"{table} ({len(pairs)} symbols)" for table, pairs in result.get("differences", {}).items())
    if result.get("status") == "repaired":
        logger.warning(color(f"  ! Differences found and repaired from raw prices: {tables}", 'YELLOW'))
        return EXIT_VERIFY_REPAIRED
    logger.error(color(f"  ✗ Differences found and not repaired: {tables}", 'RED'))
    return EXIT_VERIFY_FAILED


def run_pipeline(args, logger) -> int:
    """Steps 1-4 under the run lock; the exit code (EXIT_DATA_NOT_CURRENT only for a scheduled run)."""
    workflow_start = time.time()

    width = 64
    logger.info(color('═' * width, 'CYAN'))
    logger.info(color('  Mark Screener — Unified Pipeline', 'BOLD'))
    logger.info(color(
        f'  Mark Tight + Tight Flag   |   full history retained   |   '
        f'run: {CURRENT_TIMESTAMP}   |   log: {config.LOG_FILE.name}',
        'CYAN',
    ))
    if config.WATCHLISTS_ENABLED:
        logger.info(color(
            f"  OpenTrader watchlists: {config.OPENTRADER_ROOT}",
            'CYAN',
        ))
    else:
        logger.info(color("  OpenTrader watchlists: disabled", 'CYAN'))
    logger.info(color('═' * width, 'CYAN'))

    # Conditions that make this run's lists differ from a run on fully
    # current data; logged where found and repeated in the final summary.
    consistency_warnings: list[str] = []
    sync_stopped = False

    # --- Step 1: EOD update only when a locally eligible session is missing
    logger.info(color("▶ Step 1/4: EOD Data Update (eod2)", 'BOLD'))
    store_ready = args.skip_sync or ensure_current_store(logger)
    try:
        readiness = inspect_eod_readiness(
            config.DUCKDB_FILE,
            config.EOD_METADATA_DIR,
            datetime.now(config.MARKET_TIMEZONE),
        )
    except LocalDataError as error:
        logger.error(color(f"  ✗ Local EOD data is not usable: {error}", 'RED'))
        sys.exit(1)

    as_of = ", ".join(f"{exchange}={value:%Y-%m-%d}" for exchange, value in readiness.as_of_dates.items())
    logger.info(f"  Local EOD as-of: {as_of}; eligible target: {readiness.target_date:%Y-%m-%d}")
    if not store_ready:
        logger.warning("  The price store could not be rebuilt yet; screening the old store, no update.")
    elif args.skip_sync:
        # The database comes from the lake (docs/MARK_DB.md in the strategy project):
        # syncing here would apply corporate actions a second time to adjusted prices.
        if readiness.needs_update:
            logger.warning(
                "  --skip-sync: the database is behind the eligible target; screening it as it is."
            )
        else:
            logger.info(color("  ✓ Local EOD is current for this run (--skip-sync).", 'GREEN'))
    elif readiness.needs_update:
        logger.info(color("  Missing eligible EOD session; starting NSE/BSE update...", 'CYAN'))
        if rebase_sync_cursor(config.EOD_METADATA_DIR / "meta.json", readiness.as_of_dates["NSE"]):
            logger.info("  NSE sync cursor rebased to the actual local EOD date.")
        if rebase_sync_cursor(config.EOD_METADATA_DIR / "bse_meta.json", readiness.as_of_dates["BSE"]):
            logger.info("  BSE sync cursor rebased to the actual local EOD date.")
        t0 = time.time()
        try:
            from nse_eod_downloader import eod_downloader
            success_nse = eod_downloader.sync_database(
                config.DATA_DIR, logger, metadata_dir=config.EOD_METADATA_DIR,
                daily_bhavcopy_dir=config.DAILY_BHAVCOPY_DIR,
            )
        except Exception as error:
            logger.warning(f"  NSE EOD update unavailable: {error}")
            success_nse = False
        try:
            from bse_eod_downloader import eod_downloader as bse_eod_downloader
            success_bse = bse_eod_downloader.sync_database(
                config.BSE_DATA_DIR, logger, metadata_dir=config.EOD_METADATA_DIR,
                daily_bhavcopy_dir=config.BSE_DAILY_BHAVCOPY_DIR,
            )
        except Exception as error:
            logger.warning(f"  BSE EOD update unavailable: {error}")
            success_bse = False

        try:
            after_update = inspect_eod_readiness(
                config.DUCKDB_FILE,
                config.EOD_METADATA_DIR,
                datetime.now(config.MARKET_TIMEZONE),
            )
        except LocalDataError as error:
            logger.error(color(f"  ✗ Local EOD data became unusable: {error}", 'RED'))
            sys.exit(1)
        readiness = after_update
        if not success_nse or not success_bse or after_update.needs_update:
            fallback_as_of = ", ".join(
                f"{exchange}={value:%Y-%m-%d}" for exchange, value in after_update.as_of_dates.items()
            )
            sync_stopped = True
            consistency_warning(
                logger, consistency_warnings,
                "EOD update incomplete/unavailable; continuing on local data "
                f"({fallback_as_of}; target {after_update.target_date:%Y-%m-%d}).",
            )
        else:
            logger.info(color(f"  ✓ EOD update completed in {time.time() - t0:.1f}s", 'GREEN'))
    else:
        logger.info(color("  ✓ Local EOD is current for this run; network update skipped.", 'GREEN'))
    if store_ready and not args.skip_sync:
        # Every run, synced or not: the action lists around today and the factors
        # decided from the raw prices (eod_maintenance.py) -- a split listed late,
        # a demerger, a renamed BSE scrip are caught here, never applied twice.
        try:
            from eod_maintenance import maintain
            summary = maintain(logger)
            logger.info(color(f"  ✓ Corporate actions decided from raw prices: {summary.get('factors')}", 'GREEN'))
        except Exception as error:
            logger.warning(f"  Corporate-action step failed ({error}); prices stay as last decided.")
    report_missing_sessions(logger)

    # --- Step 2: fundamentals download ------------------------------------
    logger.info(color("▶ Step 2/4: Fundamental Data Download (screener.in)", 'BOLD'))
    refresh_fundamentals = False
    if config.FUNDAMENTAL_FILE.exists() and not args.allow_stale_fundamentals:
        try:
            session = min(readiness.as_of_dates.values())
            refresh_fundamentals = fundamentals_taken_before_close(config.FUNDAMENTAL_FILE, session)
        except Exception as error:
            logger.warning(f"  Could not tell when the fundamentals file was taken ({error}); using it.")
    if config.FUNDAMENTAL_FILE.exists() and not refresh_fundamentals:
        logger.info(color(f"  ✓ Using local fundamentals file: {config.FUNDAMENTAL_FILE.name}", 'GREEN'))
        logger.info(color("  Skipping HTTP download.", 'CYAN'))
    else:
        if refresh_fundamentals:
            logger.info(color(
                f"  {config.FUNDAMENTAL_FILE.name} was downloaded before the {session:%Y-%m-%d} close, so its "
                "Current Price is intraday; downloading it again.", 'CYAN'))
        logger.info(color("  Starting optimized direct HTTP download...", 'CYAN'))
        t0 = time.time()
        
        try:
            import fundamental_downloader
            # Ensure output directories exist before download
            config.ensure_dirs()
            
            success = fundamental_downloader.download_screener_csv(
                output_dir=config.FUNDAMENTAL_DIR
            )
        except Exception as e:
            if refresh_fundamentals:
                success = False
                logger.warning(f"  Fundamentals download failed ({e}).")
            else:
                logger.error(color(f"  ✗ Failed to execute fundamental download in Python: {e}", 'RED'))
                sys.exit(1)

        elapsed = time.time() - t0
        if not success and refresh_fundamentals:
            # The file taken earlier is still on disk (the download writes to a temporary name).
            consistency_warning(
                logger, consistency_warnings,
                f"{config.FUNDAMENTAL_FILE.name} was taken before the close and could not be refreshed; its "
                "Current Price is intraday and differs from a machine that has the after-close file.")
        elif not success:
            logger.error(color(f"  ✗ Fundamental download FAILED after {elapsed:.1f}s", 'RED'))
            sys.exit(1)
        else:
            _rename_downloaded_fundamentals(logger, elapsed)

    validate_fundamentals(logger)

    screening_as_of = min(readiness.as_of_dates.values())
    freshness = "local fallback" if readiness.needs_update else "current eligible local data"
    logger.info(
        color(
            f"  Screening as-of: {screening_as_of:%Y-%m-%d} ({freshness}; "
            f"target {readiness.target_date:%Y-%m-%d})",
            'CYAN',
        )
    )
    as_of_text = ", ".join(
        f"{exchange}={value:%Y-%m-%d}" for exchange, value in sorted(readiness.as_of_dates.items())
    )
    as_of_mismatch = len(set(readiness.as_of_dates.values())) > 1
    if as_of_mismatch:
        consistency_warning(
            logger, consistency_warnings,
            f"NSE and BSE local data end on different sessions ({as_of_text}).",
        )
    if readiness.needs_update:
        consistency_warning(
            logger, consistency_warnings,
            f"screening local fallback data ({as_of_text}), older than the eligible "
            f"target {readiness.target_date:%Y-%m-%d}.",
        )

    # --- Steps 3 & 4: screening -------------------------------------------
    engine = None
    if config.RUST_BINARY.exists():
        try:
            engine = run_fingerprint.engine_info(config.RUST_BINARY)
            logger.info(f"  Rust engine: {describe_rust_binary(config.RUST_BINARY, engine)}")
        except Exception as error:  # noqa: BLE001 - the engine step reports a real failure
            logger.warning(f"  Rust engine could not be identified: {error}")

    fingerprint = None
    try:
        fingerprint = run_fingerprint.collect(
            engine=engine,
            readiness=readiness,
            local_fallback=readiness.needs_update,
            sync_stopped=sync_stopped,
            warnings=consistency_warnings,
            include_sme=args.include_sme,
            only_sme=args.only_sme,
            external_store=args.skip_sync,
        )
        # A --skip-sync store is made elsewhere (the lake export stores eod_prices
        # as a table) and is not a version 1 store waiting for a rebuild.
        if not args.skip_sync and (fingerprint.get("store") or {}).get("kind") == "v1":
            consistency_warning(
                logger, consistency_warnings,
                "the price store is an old version 1 store (not rebuilt); its prices can "
                "differ from a version 2 store.",
            )
        if isinstance(fingerprint.get("data_as_of"), dict):
            fingerprint["data_as_of"]["warnings"] = list(consistency_warnings)
        logger.info(color("  Run fingerprint:", 'CYAN'))
        for line in run_fingerprint.summary_lines(fingerprint):
            logger.info(f"    {line}")
    except Exception as error:  # noqa: BLE001 - fingerprinting never stops the run
        logger.warning(f"  Run fingerprint unavailable: {error}")
    write_run_fingerprint(logger, fingerprint)

    run_all_screening(
        logger,
        include_sme=args.include_sme,
        only_sme=args.only_sme,
    )

    if fingerprint is not None:
        try:
            run_fingerprint.add_outputs(fingerprint)
            if isinstance(fingerprint.get("data_as_of"), dict):
                fingerprint["data_as_of"]["warnings"] = list(consistency_warnings)
            outputs = fingerprint.get("outputs") or {}
            if "error" not in outputs:
                logger.info("  Run fingerprint outputs: " + (" | ".join(
                    f"{name} {info['count']} #{info['hash'][:16]}" for name, info in outputs.items()
                ) or "none written"))
        except Exception as error:  # noqa: BLE001
            logger.warning(f"  Run fingerprint outputs unavailable: {error}")
        write_run_fingerprint(logger, fingerprint)

    # --- Summary ----------------------------------------------------------
    total = time.time() - workflow_start
    logger.info(color('─' * width, 'CYAN'))
    logger.info(color(f"  Pipeline complete in {total:.1f}s", 'BOLD'))

    # Surface this run's CSVs (now timestamped + in per-suffix subfolders).
    outputs = sorted(config.OUT_DIR.rglob(f"{CURRENT_TIMESTAMP}_*_qualified.csv"))
    if outputs:
        logger.info(color("  Qualified CSVs:", 'GREEN'))
        for p in outputs:
            logger.info(f"    {p.parent.name}/{p.name}  ({p.stat().st_size/1024:.1f} KB)")
    if config.WATCHLISTS_ENABLED:
        watchlist_base = config.OPENTRADER_ROOT / "databases"
        watchlists = sorted(
            watchlist_base.rglob(f"{CURRENT_TIMESTAMP}_*.txt")
        )
        if watchlists:
            logger.info(color("  OpenTrader watchlists:", 'GREEN'))
            for path in watchlists:
                logger.info(
                    f"    {path.relative_to(config.OPENTRADER_ROOT)}"
                )
    if consistency_warnings:
        logger.warning(color(
            "  RESULTS MAY DIFFER from a run on complete, current data:", 'YELLOW'))
        for message in consistency_warnings:
            logger.warning(color(f"    ! {message}", 'YELLOW'))
    logger.info(color(f"  Full log: {config.LOG_FILE}", 'CYAN'))
    if fingerprint is not None:
        logger.info(color(f"  Run fingerprint: {run_fingerprint.fingerprint_path()}", 'CYAN'))
    logger.info(color('═' * width, 'CYAN'))

    data_not_current = sync_stopped or readiness.needs_update or as_of_mismatch
    if data_not_current and os.environ.get("MARK_SCHEDULED") == "1":
        logger.warning(color(
            f"  Scheduled run: the data screened is not current; exit code {EXIT_DATA_NOT_CURRENT} "
            "so the scheduler tries again later.", 'YELLOW'))
        return EXIT_DATA_NOT_CURRENT
    return 0


if __name__ == '__main__':
    main()
