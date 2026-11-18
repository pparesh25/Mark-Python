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
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Runtime & Timestamps
# ---------------------------------------------------------------------------
import os

# Run dates follow the exchange calendar, not the machine's clock setting, so
# Windows, Linux and macOS machines agree on "today" (fundamentals, CSV and
# watchlist file names) whatever their local time zone.  The Rust engine
# (0.4.0+) counts Days_Since_Breakout from each exchange's latest stored
# session and ends each chart on the symbol's last candle, not on this date.
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")

# Capture timestamp: check if an environment variable is set first, so child
# processes launched by the orchestrator share the exact same timestamp.
CURRENT_TIMESTAMP = os.environ.get('MARK_SCREENER_TIMESTAMP')
if not CURRENT_TIMESTAMP:
    _NOW = datetime.now(MARKET_TIMEZONE)
    CURRENT_TIMESTAMP = _NOW.strftime('%Y%m%d_%H%M%S')
    CURRENT_DATE = _NOW.strftime('%Y%m%d')
else:
    CURRENT_DATE = CURRENT_TIMESTAMP.split('_')[0]

# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent                    # live git repo (dynamic)
BSE_ONLY_STOCKS = True


def _configured_path(environment_name: str, default: Path) -> Path:
    """Return an expanded user-configurable path without assuming an OS."""
    configured = os.environ.get(environment_name, "").strip()
    return Path(configured).expanduser() if configured else default


DATA_ROOT = _configured_path(
    "MARK_DATA_ROOT",
    Path.home() / "Mark EMA and HTF",
)
DATABASE_DIR = DATA_ROOT / "Database"
DATA_DIR = DATABASE_DIR / "NSE_EOD"
BSE_DATA_DIR = DATABASE_DIR / "BSE_EOD"
DUCKDB_FILE = DATABASE_DIR / "NSE_BSE_EOD.duckdb"

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
# The price store keeps two years of raw daily prices (decided with Paresh,
# 2026-10-02): enough for the 252-session indicators; a thinly traded stock with
# fewer sessions in two years is skipped by the screener. Older raw rows are
# dropped after every sync; factors and actions are kept.
EOD_RETAIN_YEARS = 2
# Number of the exchange's most recent sessions searched for a Flag pole
# (engine 0.5.0; before, the symbol's own last candles).  Passed to the Rust
# engine on every run.
FLAG_LOOKBACK_DAYS = 90
# A symbol is screened only when its latest candle is within this many
# sessions of its exchange's latest session.  Suspended or delisted series are
# skipped, and an NSE ISIN that stopped trading no longer hides the company's
# active BSE listing.  Passed to the Rust engine on every run.
MAX_STALE_SESSIONS = 5

# Fundamentals (all-company.csv) are downloaded fresh into the project's own
# fundamental_files/ folder, timestamped per run.
# The downloader first writes a fixed-name all-company.csv there, then the
# orchestrator renames it to all-company_{timestamp}.csv.
FUNDAMENTAL_DIR = DATABASE_DIR / "fundamental_files"
_today_fund_file = FUNDAMENTAL_DIR / f"all-company_{CURRENT_DATE}.csv"
# The ordinary pipeline targets today's file.  Reusing an older file is an
# explicit offline choice made through ``--allow-stale-fundamentals``.
FUNDAMENTAL_FILE = _today_fund_file


def select_fundamental_file(allow_stale: bool = False) -> Path:
    """Choose today's fundamentals, or the newest local fallback on request."""
    today_file = FUNDAMENTAL_DIR / f"all-company_{CURRENT_DATE}.csv"
    if today_file.exists() or not allow_stale:
        return today_file
    existing = sorted(FUNDAMENTAL_DIR.glob("all-company_*.csv"))
    return existing[-1] if existing else today_file

# ---------------------------------------------------------------------------
# Outputs (relative to USER HOME / Mark EMA and HTF)
# ---------------------------------------------------------------------------
# CSV outputs are split into per-suffix subfolders (mirrors chart_images),
# and filenames carry a timestamp so a same-day morning run is not clobbered
# by an evening run.
OUT_DIR = DATA_ROOT / "qualified_stocks"
CHART_DIR = DATA_ROOT / "chart_images"

# Per-run logs go here (not loose at the project root).
LOG_DIR = DATA_ROOT / "logs"

# Consolidated daily bhavcopy text files go here.
DAILY_BHAVCOPY_DIR = DATA_ROOT / "Daily Bhavcopy NSE"
BSE_DAILY_BHAVCOPY_DIR = DATA_ROOT / "Daily Bhavcopy BSE"

# EOD downloader metadata (meta.json and isin.csv) goes here.
EOD_METADATA_DIR = DATABASE_DIR / "metadata"

# BSE's corporate-action CSVs downloaded by hand from bseindia.com are dropped here; every run imports
# them (action_lists.import_bse_files) when BSE refuses the scripted download.
BSE_ACTIONS_INBOX = DATABASE_DIR / "BSE corporate actions"
# Qualified CSVs and run logs/fingerprints older than this many days are deleted after a run (0 keeps all).
def _retain_days(text: str | None, default: int = 60) -> int:
    try:
        return max(0, int(str(text).strip()))
    except (TypeError, ValueError):
        return default


OUTPUT_RETAIN_DAYS = _retain_days(os.environ.get("MARK_OUTPUT_RETAIN_DAYS"))
# A run warns when the stored BSE list was last refreshed (fetched or imported) this many days ago or more.
BSE_LIST_STALE_DAYS = 7


def get_output_dirs(exchange: str):
    """Get the output and chart directories for a specific exchange (NSE or BSE)."""
    ex_upper = exchange.upper()
    out_base = OUT_DIR / ex_upper
    chart_base = CHART_DIR / ex_upper
    
    return {
        'mark_tight_out': out_base / 'mark_tight',
        'tight_flag_out': out_base / 'Tight Flag',
        'mark_tight_chart': chart_base / 'mark_tight',
        'tight_flag_chart': chart_base / 'Tight Flag',
    }


# ---------------------------------------------------------------------------
# External tools / dependencies
# ---------------------------------------------------------------------------
OPENTRADER_ROOT = _configured_path(
    "MARK_OPENTRADER_ROOT",
    Path.home() / ".opentrader-pro",
)
WATCHLISTS_ENABLED = os.environ.get(
    "MARK_WRITE_WATCHLISTS", "1"
).strip().lower() not in {"0", "false", "no", "off"}
STRIP_SME_WATCHLIST_SUFFIX = os.environ.get(
    "MARK_STRIP_SME_WATCHLIST_SUFFIX", "0"
).strip().lower() in {"1", "true", "yes", "on"}


def get_screener_credentials_path(root: Path | None = None) -> Path:
    """Return the private Screener.in credential file outside the repository."""
    return _configured_path(
        "MARK_SCREENER_CREDS",
        (root or DATA_ROOT) / "Screener login" / "screener.txt",
    )


SCREENER_CREDS = get_screener_credentials_path()
SCREENER_SCREEN_ID = '3427564'

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
# Timestamped log file per run, stored in its own folder.
LOG_FILE = LOG_DIR / f"screening_log_{CURRENT_TIMESTAMP}.txt"
# Held while a run is in progress, so a manual run during a scheduled one (or
# two launchers at once) does not collide on the DuckDB write lock.
RUN_LOCK_FILE = DATA_ROOT / "mark_screener.lock"


def get_watchlist_dir(exchange: str, root: Path | None = None) -> Path:
    """Return OpenTrader Pro's exchange-specific watchlist directory."""
    exchange_name = exchange.strip().upper()
    if exchange_name not in {"NSE", "BSE"}:
        raise ValueError(f"Unsupported exchange for watchlist: {exchange}")
    database_name = f"{exchange_name}_EOD"
    return (root or OPENTRADER_ROOT) / "databases" / database_name / "watchlists"


def configure_watchlists(
    enabled: bool,
    root: Path | None = None,
    strip_sme_suffix: bool | None = None,
) -> None:
    """Apply CLI watchlist settings to this process and child processes."""
    global WATCHLISTS_ENABLED, OPENTRADER_ROOT, STRIP_SME_WATCHLIST_SUFFIX
    WATCHLISTS_ENABLED = enabled
    os.environ["MARK_WRITE_WATCHLISTS"] = "1" if enabled else "0"
    if root is not None:
        OPENTRADER_ROOT = Path(root).expanduser()
        os.environ["MARK_OPENTRADER_ROOT"] = str(OPENTRADER_ROOT)
    if strip_sme_suffix is not None:
        STRIP_SME_WATCHLIST_SUFFIX = strip_sme_suffix
        os.environ["MARK_STRIP_SME_WATCHLIST_SUFFIX"] = (
            "1" if strip_sme_suffix else "0"
        )


def configure_data_root(root: Path) -> None:
    """Rebase every mutable data/output path under one portable root."""
    global DATA_ROOT, DATABASE_DIR, DATA_DIR, BSE_DATA_DIR, DUCKDB_FILE
    global FUNDAMENTAL_DIR, FUNDAMENTAL_FILE, OUT_DIR, CHART_DIR, LOG_DIR
    global DAILY_BHAVCOPY_DIR, BSE_DAILY_BHAVCOPY_DIR, EOD_METADATA_DIR, LOG_FILE
    global SCREENER_CREDS, RUN_LOCK_FILE, BSE_ACTIONS_INBOX

    DATA_ROOT = Path(root).expanduser()
    os.environ["MARK_DATA_ROOT"] = str(DATA_ROOT)
    DATABASE_DIR = DATA_ROOT / "Database"
    DATA_DIR = DATABASE_DIR / "NSE_EOD"
    BSE_DATA_DIR = DATABASE_DIR / "BSE_EOD"
    DUCKDB_FILE = DATABASE_DIR / "NSE_BSE_EOD.duckdb"
    FUNDAMENTAL_DIR = DATABASE_DIR / "fundamental_files"
    FUNDAMENTAL_FILE = select_fundamental_file()
    OUT_DIR = DATA_ROOT / "qualified_stocks"
    CHART_DIR = DATA_ROOT / "chart_images"
    LOG_DIR = DATA_ROOT / "logs"
    DAILY_BHAVCOPY_DIR = DATA_ROOT / "Daily Bhavcopy NSE"
    BSE_DAILY_BHAVCOPY_DIR = DATA_ROOT / "Daily Bhavcopy BSE"
    EOD_METADATA_DIR = DATABASE_DIR / "metadata"
    BSE_ACTIONS_INBOX = DATABASE_DIR / "BSE corporate actions"
    SCREENER_CREDS = get_screener_credentials_path(DATA_ROOT)
    LOG_FILE = LOG_DIR / f"screening_log_{CURRENT_TIMESTAMP}.txt"
    RUN_LOCK_FILE = DATA_ROOT / "mark_screener.lock"


def ensure_dirs(include_watchlists: bool | None = None):
    """Create all output directories if they don't exist."""
    for ex in ('NSE', 'BSE'):
        dirs = get_output_dirs(ex)
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)
            
    for d in (
        DATA_ROOT, DATABASE_DIR, DATA_DIR, BSE_DATA_DIR, FUNDAMENTAL_DIR,
        OUT_DIR, CHART_DIR, LOG_DIR, DAILY_BHAVCOPY_DIR,
        BSE_DAILY_BHAVCOPY_DIR, EOD_METADATA_DIR, BSE_ACTIONS_INBOX, SCREENER_CREDS.parent,
    ):
        d.mkdir(parents=True, exist_ok=True)

    write_watchlists = (
        WATCHLISTS_ENABLED if include_watchlists is None else include_watchlists
    )
    if write_watchlists:
        for exchange in ("NSE", "BSE"):
            get_watchlist_dir(exchange).mkdir(parents=True, exist_ok=True)


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
