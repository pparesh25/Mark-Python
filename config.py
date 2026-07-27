"""Centralised configuration for the Mark screener pipeline.

All hard-coded paths and strategy parameters live here so the screener and the
unified orchestrator share a single source of truth.

Note: the change guide (mark_screener_changes_guide.md §5) suggested
``BASE_DIR = .../Strong/mark`` but that path is a stale non-git copy. The live
project is the git repo at ``Mark Python`` — we use that instead.
"""

from datetime import datetime
from pathlib import Path
import platform

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
DUCKDB_FILE = Path.home() / 'Mark EMA and HTF' / 'Database' / 'NSE_BSE_EOD.duckdb'
BSE_ONLY_STOCKS = True

# Pipeline Mode Flags:
# USE_RUST_CALCULATION: If True, screening calculation is performed by the Rust engine.
# USE_RUST_CHARTS: If True, chart image export is performed by the Rust engine.
# Mode 1 (Full Rust): USE_RUST_CALCULATION = True, USE_RUST_CHARTS = True
# Mode 2 (Hybrid R&D): USE_RUST_CALCULATION = False, USE_RUST_CHARTS = True
# Mode 3 (Pure Python): USE_RUST_CALCULATION = False, USE_RUST_CHARTS = False
USE_RUST_CALCULATION = True
USE_RUST_CHARTS = True


def _rust_platform_key(system_name=None, machine_name=None):
    """Return the prebuilt-binary directory for this operating system."""
    system = (system_name or platform.system()).strip().lower()
    machine = (machine_name or platform.machine()).strip().lower()
    architecture = {
        'amd64': 'x86_64',
        'x64': 'x86_64',
        'x86_64': 'x86_64',
        'aarch64': 'aarch64',
        'arm64': 'aarch64',
    }.get(machine, machine or 'unknown')
    return f"{system}-{architecture}"


def _resolve_rust_binary():
    """Prefer an explicitly configured or downloaded prebuilt Rust engine."""
    executable = 'stock_analysis.exe' if platform.system() == 'Windows' else 'stock_analysis'
    override = os.environ.get('MARK_RUST_BINARY')
    if override:
        return Path(override).expanduser()

    prebuilt = (
        BASE_DIR / 'stock_analysis' / 'bin' /
        _rust_platform_key() / executable
    )
    if prebuilt.is_file():
        return prebuilt

    # Preserve the local Cargo path for macOS development and source builds.
    return BASE_DIR / 'stock_analysis' / 'target' / 'release' / executable


RUST_BINARY = _resolve_rust_binary()

# All strategies require a full year of daily price history.  The same values
# are mirrored in the Rust screener so neither engine can qualify a newly
# listed stock with a shortened indicator window.
MIN_HISTORY_DAYS = 252
EMA_PERIOD = 200
# Keep a little under three years of sessions. It covers every indicator
# window while bounding the local DuckDB price store.
EOD_RETAIN_CANDLES = 600
# Number of most-recent daily candles searched for a Flag pole.  This value is
# passed to the Rust engine on every run so both implementations use it.
FLAG_LOOKBACK_DAYS = 90

# Fundamentals (all-company.csv) are downloaded fresh into the project's own
# fundamental_files/ folder, timestamped per run.
# The downloader first writes a fixed-name all-company.csv there (Chrome saves
# whatever name the server returns), then the orchestrator renames it to
# all-company_{timestamp}.csv.
FUNDAMENTAL_DIR = Path.home() / 'Mark EMA and HTF' / 'Database' / 'fundamental_files'
_today_fund_file = FUNDAMENTAL_DIR / f"all-company_{CURRENT_DATE}.csv"
if _today_fund_file.exists():
    FUNDAMENTAL_FILE = _today_fund_file
else:
    _existing_funds = sorted(list(FUNDAMENTAL_DIR.glob("all-company_*.csv")))
    FUNDAMENTAL_FILE = _existing_funds[-1] if _existing_funds else _today_fund_file

# ---------------------------------------------------------------------------
# Outputs (relative to USER HOME / Mark EMA and HTF)
# ---------------------------------------------------------------------------
# CSV outputs are split into per-suffix subfolders (mirrors chart_images),
# and filenames carry a timestamp so a same-day morning run is not clobbered
# by an evening run.
OUT_DIR = Path.home() / 'Mark EMA and HTF' / 'qualified_stocks'
CHART_DIR = Path.home() / 'Mark EMA and HTF' / 'chart_images'

# Per-run logs go here (not loose at the project root).
LOG_DIR = Path.home() / 'Mark EMA and HTF' / 'logs'

# Consolidated daily bhavcopy text files go here.
DAILY_BHAVCOPY_DIR = Path.home() / 'Mark EMA and HTF' / 'Daily Bhavcopy NSE'
BSE_DAILY_BHAVCOPY_DIR = Path.home() / 'Mark EMA and HTF' / 'Daily Bhavcopy BSE'

# EOD downloader metadata (meta.json and isin.csv) goes here.
EOD_METADATA_DIR = Path.home() / 'Mark EMA and HTF' / 'Database' / 'metadata'


def get_output_dirs(exchange: str):
    """Get the output and chart directories for a specific exchange (NSE or BSE)."""
    ex_upper = exchange.upper()
    out_base = OUT_DIR / ex_upper
    chart_base = CHART_DIR / ex_upper
    
    return {
        'mark_out': out_base / 'mark',
        'mark_tight_out': out_base / 'mark_tight',
        'tight_flag_out': out_base / 'Tight Flag',
        
        'mark_chart': chart_base / 'mark',
        'mark_tight_chart': chart_base / 'mark_tight',
        'tight_flag_chart': chart_base / 'Tight Flag',
    }


# ---------------------------------------------------------------------------
# External tools / dependencies
# ---------------------------------------------------------------------------
AMIBROKER_WATCHLIST_DIR = Path(
    '/Users/paresh/.opentrader-pro/databases/NSE_EOD/watchlists'
)
SCREENER_CREDS = BASE_DIR / 'Screener login' / 'screener.txt'
SCREENER_SCREEN_ID = '3427564'

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
# Timestamped log file per run, stored in its own folder.
LOG_FILE = LOG_DIR / f"screening_log_{CURRENT_TIMESTAMP}.txt"


def ensure_dirs():
    """Create all output directories if they don't exist."""
    for ex in ('NSE', 'BSE'):
        dirs = get_output_dirs(ex)
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)
            
    for d in (OUT_DIR, CHART_DIR, LOG_DIR, AMIBROKER_WATCHLIST_DIR, FUNDAMENTAL_DIR,
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
