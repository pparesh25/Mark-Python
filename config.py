"""Centralised configuration for the Mark screener pipeline.

All hard-coded paths and strategy parameters live here so the screener and the
unified orchestrator share a single source of truth.

Note: the change guide (mark_screener_changes_guide.md §5) suggested
``BASE_DIR = .../Strong/mark`` but that path is a stale non-git copy. The live
project is the git repo at ``Mark Python`` — we use that instead.
"""

from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Runtime & Timestamps
# ---------------------------------------------------------------------------
import os

# Capture timestamp: check if an environment variable is set first, so child
# processes launched by the orchestrator share the exact same timestamp.
CURRENT_TIMESTAMP = os.environ.get('MARK_SCREENER_TIMESTAMP')
if not CURRENT_TIMESTAMP:
    _NOW = datetime.now()
    CURRENT_TIMESTAMP = _NOW.strftime('%Y%m%d_%H%M%S')
    CURRENT_DATE = _NOW.strftime('%Y%m%d')
else:
    CURRENT_DATE = CURRENT_TIMESTAMP.split('_')[0]

# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent                    # live git repo (dynamic)
DATA_DIR = Path.home() / 'Mark EMA and HTF' / 'Database' / 'NSE_EOD'
BSE_DATA_DIR = Path.home() / 'Mark EMA and HTF' / 'Database' / 'BSE_EOD'

# Fundamentals (all-company.csv) are downloaded fresh into the project's own
# fundamental_files/ folder, timestamped per run.
# The downloader first writes a fixed-name all-company.csv there (Chrome saves
# whatever name the server returns), then the orchestrator renames it to
# all-company_{timestamp}.csv.
FUNDAMENTAL_DIR = Path.home() / 'Mark EMA and HTF' / 'Database' / 'fundamental_files'
FUNDAMENTAL_FILE = FUNDAMENTAL_DIR / f"all-company_{CURRENT_DATE}.csv"

# ---------------------------------------------------------------------------
# Outputs (relative to USER HOME / Mark EMA and HTF)
# ---------------------------------------------------------------------------
# CSV outputs are split into per-suffix subfolders (mirrors chart_images),
# and filenames carry a timestamp so a same-day morning run is not clobbered
# by an evening run.
OUT_DIR = Path.home() / 'Mark EMA and HTF' / 'qualified_stocks'
MARK_OUT_DIR = OUT_DIR / 'mark'
MARK_TIGHT_OUT_DIR = OUT_DIR / 'mark_tight'
FLAG_OUT_DIR = OUT_DIR / 'Flag'
TIGHT_FLAG_OUT_DIR = OUT_DIR / 'Tight Flag'

CHART_DIR = Path.home() / 'Mark EMA and HTF' / 'chart_images'
MARK_CHART_DIR = CHART_DIR / 'mark'
MARK_TIGHT_CHART_DIR = CHART_DIR / 'mark_tight'
FLAG_CHART_DIR = CHART_DIR / 'Flag'
TIGHT_FLAG_CHART_DIR = CHART_DIR / 'Tight Flag'

# Per-run logs go here (not loose at the project root).
LOG_DIR = Path.home() / 'Mark EMA and HTF' / 'logs'

# Consolidated daily bhavcopy text files go here.
DAILY_BHAVCOPY_DIR = Path.home() / 'Mark EMA and HTF' / 'Daily Bhavcopy NSE'
BSE_DAILY_BHAVCOPY_DIR = Path.home() / 'Mark EMA and HTF' / 'Daily Bhavcopy BSE'

# EOD downloader metadata (meta.json and isin.csv) goes here.
EOD_METADATA_DIR = BASE_DIR / 'metadata'

# ---------------------------------------------------------------------------
# External tools / dependencies
# ---------------------------------------------------------------------------
AMIBROKER_WATCHLIST_DIR = Path(
    '/Users/paresh/.opentrader-pro/databases/NSE_EOD/watchlists'
)
SCREENER_CREDS = BASE_DIR / 'Screener login' / 'screener.txt'
SCREENER_SCREEN_ID = '3427564'

# ---------------------------------------------------------------------------
# Strategy parameters (guide §3.1)
# ---------------------------------------------------------------------------
# Breakout qualifies if it occurred on ANY of the last N days; N=1 = today-only
BREAKOUT_WINDOW = 5

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
# Timestamped log file per run, stored in its own folder.
LOG_FILE = LOG_DIR / f"screening_log_{CURRENT_TIMESTAMP}.txt"


def ensure_dirs():
    """Create all output directories if they don't exist.

    Mirrors the original module-level mkdir calls (Mark screener.py:32-35) but
    centralised so the orchestrator can call it once before screening.
    """
    for d in (OUT_DIR, MARK_OUT_DIR, MARK_TIGHT_OUT_DIR,
              FLAG_OUT_DIR, TIGHT_FLAG_OUT_DIR,
              CHART_DIR, MARK_CHART_DIR, MARK_TIGHT_CHART_DIR,
              FLAG_CHART_DIR, TIGHT_FLAG_CHART_DIR,
              LOG_DIR, AMIBROKER_WATCHLIST_DIR, FUNDAMENTAL_DIR,
              DAILY_BHAVCOPY_DIR, BSE_DAILY_BHAVCOPY_DIR, EOD_METADATA_DIR,
              BSE_DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)


def validate_inputs():
    """Return a list of human-readable error strings for missing inputs.

    Empty list = everything the screener needs is present.
    """
    errors = []
    if not DATA_DIR.exists():
        errors.append(f"Data directory not found: {DATA_DIR}")
    if not FUNDAMENTAL_FILE.exists():
        errors.append(f"Fundamentals file not found: {FUNDAMENTAL_FILE}")
    if not SCREENER_CREDS.exists():
        errors.append(f"Screener credentials not found: {SCREENER_CREDS}")
    return errors
