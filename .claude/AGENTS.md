# AGENTS.md — Mark Python Project

## Project Overview

Cross-platform NSE/BSE stock screening pipeline. Downloads EOD price data and
fundamentals, then runs Mark Tight and Tight Flag screens with priority
scoring, CSVs, charts, and OpenTrader Pro watchlists.

## Team Workflow

- **team-lead**: Orchestrates work, creates tasks, delegates to developers, sends review to alice
- **bob**: Developer — implementation, fixes, testing
- **jack**: Developer — implementation, fixes, testing
- **tom**: Developer — implementation, fixes, testing
- **alice**: Reviewer — reviews completed tasks via kanban board review flow

### Task Lifecycle
1. team-lead creates task, assigns owner, starts it
2. Owner works the task (edits code, runs checks)
3. When done: `task_add_comment` with full results, then `task_complete`
4. team-lead requests review: `review_request` with reviewer="alice"
5. alice starts review: `review_start`, then either `review_approve` or `review_request_changes`
6. If changes requested, owner fixes and re-requests review
7. When approved, team-lead closes

## Project Architecture

### Core Pipeline (run_screener.py)
1. **Step 1** — `nse_eod_downloader/eod_downloader.py`: Syncs NSE EOD database (daily OHLCV from NSE India)
2. **Step 2** — `fundamental_downloader.py`: Selenium download from screener.in (screen 3427564)
3. **Step 3** — `Mark_screener.py`: Technical + fundamental screening

### Key Files

| File | Purpose |
|------|---------|
| `run_screener.py` | Unified pipeline orchestrator |
| `config.py` | Centralized paths, parameters, strategy settings |
| `nse_eod_downloader/` | NSE EOD downloader and database maintenance modules |
| `fundamental_downloader.py` | Screener.in CSV download (Selenium) |
| `Mark_screener.py` | Main screener: Mark Tight + Priority Score |
| `clean_nse_eod.py` | Cleans NSE_EOD CSVs to Date,Open,High,Low,Close,Volume |

### Config Details (config.py)
- `BASE_DIR` = dynamic to project root
- `DATA_DIR` = NSE_EOD/
- Screener screen ID = 3427564
- Output: qualified_stocks/mark_tight/ and qualified_stocks/Tight Flag/
- Charts: chart_images/mark_tight/ and chart_images/Tight Flag/
- Logs: logs/screening_log_{timestamp}.txt
- AmiBroker watchlists in ~/.opentrader-pro/databases/NSE_EOD/watchlists/

## Setup & Running

### Environment
```bash
# Linux x86_64:
./setup_linux.sh

# Windows x64 PowerShell:
.\setup_windows.ps1

# Optional legacy Python calculation/chart fallback:
python -m pip install -r requirements-legacy.txt
```

### Running
```bash
# Full pipeline:
source .venv/bin/activate && python run_screener.py

# Or individual steps:
# EOD sync:
python -c "from nse_eod_downloader.eod_downloader import sync_database; import config, logging; logging.basicConfig(level=logging.INFO); sync_database(config.DATA_DIR, logging.getLogger(), metadata_dir=config.EOD_METADATA_DIR, daily_bhavcopy_dir=config.DAILY_BHAVCOPY_DIR)"

# Fundamentals download (needs screener.in credentials in Screener login/screener.txt):
python fundamental_downloader.py --headless

# Screener only:
python Mark_screener.py
```

### Credentials
- File: `~/Mark EMA and HTF/Screener login/screener.txt`
  (line 1: email, line 2: password)
- Must exist for fundamentals download step

## Code Conventions

- **Imports**: stdlib first, then third-party, then local (config)
- **Logging**: Use `logging.getLogger(__name__)` or pass logger parameter
- **Config**: NEVER hardcode paths — always use config.py
- **Paths**: Use `pathlib.Path` exclusively
- **DataFrames**: Column names lowercase after initial read (screener normalizes)
- **Charts**: matplotlib Agg backend (non-interactive), mplfinance candlesticks
- **Threading**: `sync_lock = threading.Lock()` for EOD sync
- **Retries**: `@retry()` decorator with exponential backoff in eod_downloader
- **Subprocess**: Subprocess for screener module (avoids ProcessPoolExecutor PicklingError on macOS)
- **Safety**: Always check `chart_dir.relative_to(BASE_DIR)` before `shutil.rmtree`
- **Dirs**: `config.ensure_dirs()` creates all output directories

## Screening Strategy Details

### Mark setup (internal)
Conditions: EMA 50 > EMA 150 > EMA 200 (stack) AND close >= 80% of 52w high AND close >= 150% of 52w low
Reverse-walk through history to find the start of the active setup → Mark_Date

### Mark Tight
Frozen base: Donchian 60-day high of highs immediately before Mark starts.
Breakout: latest close > frozen base; the 20%/50% Mark conditions remain active.
Latest breakout day = Mark_Tight_Date.

### Tight Flag
Internal candidate gate: a 90%+ run-up within 45 trading days.  Only candidates passing pole quality, breakout, holding, EMA200, and 52-week-distance filters are exported.

### Priority Score (add_priority_score in process_results)
- 30% Momentum (1y + 3m return percentiles)
- 28% Quality (ROCE + ROE + Piotroski - Debt/Equity + Altman Z)
- 22% Fuel (sales + profit growth YoY)
- 12% Sponsorship (FII + DII change)
- 8% Liquidity (log turnover)
- Tradeable flag: turnover >= 2cr AND D/E <= 1 AND pledge <= 15% AND Altman Z >= 1.8
- Sorted by Priority_Score descending within each output set

## Known Issues & Gotchas

1. **NSE package**: The downloader uses BennyThadikaran's `nse` package
   with `server=True`; install the pinned `nse[server]` dependency through
   `requirements.txt`. `nsetools` is a different API and is not a substitute.

2. **TA-Lib on macOS**: Prebuilt wheel available for arm64 (ta_lib-0.7.1). No system TA-Lib library needed on Apple Silicon.

3. **Selenium ChromeDriver**: `webdriver-manager` auto-manages ChromeDriver. Headless mode enabled with `--headless=new`.

4. **ProcessPoolExecutor**: Screener runs as subprocess to avoid PicklingError on macOS.

5. **pathlib.Path.replace()**: Used atomically instead of `os.rename()` for portability.

6. **Saturday bhavcopy fix**: Weekend check added to skip Saturday (original code only skipped Sunday, causing 404 on Saturday and aborting entire sync loop).

7. **validate_fundamentals()**: Checks columns required by `add_priority_score()` before screening begins.

8. **Conda vs venv**: `Mark_Python.sh` uses conda env `mark_screener` from miniforge3. For new setups, the .venv approach works with standard pip.

## Git Workflow

- `.gitignore` ignores: CSVs, PNGs, logs, fundamental_files, Screener login, chart_images, qualified_stocks, NSE_EOD, metadata, .venv
- NEVER commit credentials or secrets
- Commit messages: descriptive, in English
- EOD data, CSV outputs, charts, and logs are NOT committed

## Review Checklist

When reviewing code (alice's role):
- [ ] All imports present and ordered correctly
- [ ] Config used for paths, no hard-coded paths
- [ ] Error handling present (try/except with logging)
- [ ] No bare `except:` without logging
- [ ] `shutil.rmtree` preceded by `relative_to(BASE_DIR)` check
- [ ] DataFrames use `pd.to_numeric(errors='coerce')` for safety
- [ ] NaN handling in scoring functions
- [ ] No stale imports or unused variables
- [ ] Features match the changes_guide.md spec
- [ ] Tests not required (no test framework in project)
