#!/usr/bin/env python
"""Unified Mark screener pipeline — single command runs the whole workflow.

Replaces ``run_complete_screening.sh`` + the three ``.sh`` wrappers with one
Python orchestrator that:

  Step 1 — updates the eod2 price database (via the existing run_eod2.sh).
  Step 2 — downloads fresh fundamentals from screener.in (Selenium downloader).
  Step 3 — runs the technical + fundamental screen (Mark screener_5dbreakout.py).

Improvements over the legacy shell orchestrator:
  * Real error propagation — a non-zero exit from any step aborts the run.
    (The old wrappers always reported success, masking failures.)
  * Fundamentals validation — verifies all-company.csv exists, is non-empty
    and looks like the expected export before screening consumes it.
  * Per-step timing and a final summary, all tee'd to a timestamped log file.

Usage (conda env required — TA-Lib / selenium / pandas live there):
    conda activate mark_screener
    python "/Users/paresh/Desktop/Mark Python/run_screener.py"
"""

import importlib.util
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import config
from config import (
    BASE_DIR, FUNDAMENTAL_FILE, OUT_DIR,
    BREAKOUT_WINDOW, CURRENT_TIMESTAMP,
)

# Screener module executed in Step 3 (filename has a space — quoted via Path).
SCREENER_MODULE = BASE_DIR / 'Mark screener_5dbreakout.py'

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

    The previous DEBUG root logger also captured low-level HTTP/HPACK and
    Selenium transport traces, making an ordinary run produce thousands of
    mostly useless lines.  Keep operational messages at INFO and suppress
    dependency internals unless a dependency reports a warning or error.
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
        'httpx', 'httpcore', 'hpack', 'selenium', 'urllib3',
        'webdriver_manager', 'websocket', 'websockets',
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

    The old pipeline would silently screen against a missing/empty file (the
    Selenium downloader never propagated failures). Catch that here.
    """
    if not FUNDAMENTAL_FILE.exists():
        logger.error(color(f"  ✗ fundamentals missing: {FUNDAMENTAL_FILE}", 'RED'))
        logger.error(color("    Did the screener.in download fail? Aborting.", 'RED'))
        sys.exit(1)

    size = FUNDAMENTAL_FILE.stat().st_size
    if size < 1000:
        logger.error(color(f"  ✗ fundamentals file too small ({size} bytes) — "
                           "likely an error page, not the export.", 'RED'))
        sys.exit(1)

    with open(FUNDAMENTAL_FILE, 'r', encoding='utf-8', errors='replace') as f:
        header = f.readline().strip()
    # Columns the priority-score helper depends on (see add_priority_score).
    expected_cols = [
        'NSE Code', 'Current Price', 'Debt to equity', 'Volume 1month average',
        'Return over 1year', 'Return over 3months', 'Return on capital employed',
        'Return on equity', 'Piotroski score', 'Altman Z Score',
        'YOY Quarterly sales growth', 'YOY Quarterly profit growth',
        'Change in FII holding', 'Change in DII holding', 'Pledged percentage',
    ]
    missing = [c for c in expected_cols if c not in header]
    if missing:
        logger.error(color(f"  ✗ header missing expected columns: {missing}", 'RED'))
        logger.error(f"    first line: {header[:120]}")
        sys.exit(1)

    mtime = datetime.fromtimestamp(FUNDAMENTAL_FILE.stat().st_mtime)
    age_min = (datetime.now() - mtime).total_seconds() / 60
    logger.info(color(f"  ✓ {FUNDAMENTAL_FILE.name} OK "
                      f"({size/1024:.0f} KB, modified {mtime:%H:%M:%S}, "
                      f"{age_min:.0f} min ago)", 'GREEN'))


# ---------------------------------------------------------------------------
# Step 3 & 4 — run screeners as subprocesses
# ---------------------------------------------------------------------------
FLAG_SCREENER_MODULE = BASE_DIR / 'flag_screener.py'

def run_screening(logger):
    """Execute Mark screener_5dbreakout.py as a subprocess for NSE and BSE."""
    success_nse = run_step("Step 3/4: NSE Stock Screening", [sys.executable, "-u", str(SCREENER_MODULE), "--exchange", "NSE"], logger)
    success_bse = run_step("Step 3/4: BSE Stock Screening", [sys.executable, "-u", str(SCREENER_MODULE), "--exchange", "BSE"], logger)
    return success_nse and success_bse


def run_flag_screening(logger):
    """Execute flag_screener.py as a subprocess for NSE and BSE."""
    success_nse = run_step("Step 4/4: NSE Flag & Tight Flag Screening", [sys.executable, "-u", str(FLAG_SCREENER_MODULE), "--exchange", "NSE"], logger)
    success_bse = run_step("Step 4/4: BSE Flag & Tight Flag Screening", [sys.executable, "-u", str(FLAG_SCREENER_MODULE), "--exchange", "BSE"], logger)
    return success_nse and success_bse


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    os.environ['MARK_SCREENER_TIMESTAMP'] = config.CURRENT_TIMESTAMP
    logger = setup_logging()
    workflow_start = time.time()

    width = 64
    logger.info(color('═' * width, 'CYAN'))
    logger.info(color('  Mark Screener — Unified Pipeline', 'BOLD'))
    logger.info(color(f'  Breakout window: {BREAKOUT_WINDOW} day(s)   |   '
                      f'run: {CURRENT_TIMESTAMP}   |   log: {config.LOG_FILE.name}',
                      'CYAN'))
    logger.info(color('═' * width, 'CYAN'))

    # --- Step 1: eod2 database update -------------------------------------
    logger.info(color("▶ Step 1/4: EOD Data Update (eod2)", 'BOLD'))
    logger.info(color("  Starting NSE EOD download and adjustment in Python...", 'CYAN'))
    t0 = time.time()
    t_nse_start = time.time()
    try:
        from nse_eod_downloader import eod_downloader
        success_nse = eod_downloader.sync_database(
            config.DATA_DIR,
            logger,
            metadata_dir=config.EOD_METADATA_DIR,
            daily_bhavcopy_dir=config.DAILY_BHAVCOPY_DIR
        )
    except Exception as e:
        logger.error(color(f"  ✗ Failed to execute NSE EOD update in Python: {e}", 'RED'))
        sys.exit(1)
        
    elapsed_nse = time.time() - t_nse_start
    if not success_nse:
        logger.error(color(f"  ✗ NSE EOD update FAILED after {elapsed_nse:.1f}s", 'RED'))
        sys.exit(1)
    logger.info(color(f"  ✓ NSE EOD update completed in {elapsed_nse:.1f}s", 'GREEN'))
        
    logger.info(color("  Starting BSE EOD download and adjustment in Python...", 'CYAN'))
    t_bse_start = time.time()
    try:
        from bse_eod_downloader import eod_downloader as bse_eod_downloader
        success_bse = bse_eod_downloader.sync_database(
            config.BSE_DATA_DIR,
            logger,
            metadata_dir=config.EOD_METADATA_DIR,
            daily_bhavcopy_dir=config.BSE_DAILY_BHAVCOPY_DIR
        )
    except Exception as e:
        logger.error(color(f"  ✗ Failed to execute BSE EOD update in Python: {e}", 'RED'))
        sys.exit(1)
        
    elapsed_bse = time.time() - t_bse_start
    if not success_bse:
        logger.error(color(f"  ✗ BSE EOD update FAILED after {elapsed_bse:.1f}s", 'RED'))
        sys.exit(1)
    logger.info(color(f"  ✓ BSE EOD update completed in {elapsed_bse:.1f}s", 'GREEN'))
        
    elapsed = time.time() - t0
    logger.info(color(f"  ✓ Total EOD update completed in {elapsed:.1f}s", 'GREEN'))

    # --- Step 2: fundamentals download ------------------------------------
    logger.info(color("▶ Step 2/4: Fundamental Data Download (screener.in)", 'BOLD'))
    if FUNDAMENTAL_FILE.exists():
        logger.info(color(f"  ✓ Fundamentals file for today already exists: {FUNDAMENTAL_FILE.name}", 'GREEN'))
        logger.info(color("  Skipping browser download process.", 'CYAN'))
    else:
        logger.info(color("  Starting Selenium browser automation directly from Python...", 'CYAN'))
        t0 = time.time()
        
        try:
            import fundamental_downloader
            # Ensure output directories exist before download
            config.ensure_dirs()
            
            success = fundamental_downloader.download_screener_csv(
                output_dir=config.FUNDAMENTAL_DIR,
                headless=True
            )
        except Exception as e:
            logger.error(color(f"  ✗ Failed to execute fundamental download in Python: {e}", 'RED'))
            sys.exit(1)
            
        elapsed = time.time() - t0
        if not success:
            logger.error(color(f"  ✗ Fundamental download FAILED after {elapsed:.1f}s", 'RED'))
            sys.exit(1)
            
        logger.info(color(f"  ✓ download completed in {elapsed:.1f}s", 'GREEN'))

        # Rename downloaded all-company.csv to date-based format
        temp_file = config.FUNDAMENTAL_DIR / "all-company.csv"
        if temp_file.exists():
            try:
                # Path.replace() atomically overwrites an existing target across
                # platforms (unlike rename, which can fail on some OSes).
                temp_file.replace(FUNDAMENTAL_FILE)
                logger.info(color(f"  ✓ Renamed downloaded file to {FUNDAMENTAL_FILE.name}", 'GREEN'))
            except Exception as e:
                logger.error(color(f"  ✗ Failed to rename downloaded file to date-based name: {e}", 'RED'))
                sys.exit(1)
        else:
            logger.error(color(f"  ✗ Downloaded file not found at {temp_file}", 'RED'))
            sys.exit(1)

    validate_fundamentals(logger)

    # --- Step 3: screening ------------------------------------------------
    run_screening(logger)

    # --- Step 4: flag screening -------------------------------------------
    run_flag_screening(logger)

    # --- Summary ----------------------------------------------------------
    total = time.time() - workflow_start
    logger.info(color('─' * width, 'CYAN'))
    logger.info(color(f"  Pipeline complete in {total:.1f}s", 'BOLD'))

    # Surface this run's CSVs (now timestamped + in per-suffix subfolders).
    outputs = sorted(OUT_DIR.rglob(f"{CURRENT_TIMESTAMP}_*_qualified.csv"))
    if outputs:
        logger.info(color("  Qualified CSVs:", 'GREEN'))
        for p in outputs:
            logger.info(f"    {p.parent.name}/{p.name}  ({p.stat().st_size/1024:.1f} KB)")
    logger.info(color(f"  Full log: {config.LOG_FILE}", 'CYAN'))
    logger.info(color('═' * width, 'CYAN'))


if __name__ == '__main__':
    main()
