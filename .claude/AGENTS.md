# AGENTS.md — Mark Python Project

## Project Overview

Cross-platform NSE/BSE stock screening pipeline. Python downloads EOD price
data (DuckDB) and Screener.in fundamentals; one precompiled Rust process then
runs the Mark Tight and Tight Flag screens and writes CSVs, charts, and
OpenTrader Pro watchlists.

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
1. **Step 1** — `screening_readiness.py` decides whether an EOD session is
   missing; if so `nse_eod_downloader/` and `bse_eod_downloader/` update the
   DuckDB store through `eod_store.py`.  Missing recent sessions are logged.
2. **Step 2** — `fundamental_downloader.py`: two-request HTTP export from
   screener.in (screen 3427564) into `all-company_YYYYMMDD.csv`.
3. **Steps 3-4** — `stock_analysis` (Rust) `pipeline` subcommand: ISIN join
   with fundamentals, NSE/BSE overlap removal, stale-series filter, screens,
   ranking, CSVs, watchlists, and charts.

### Key Files

| File | Purpose |
|------|---------|
| `run_screener.py` | Unified pipeline orchestrator |
| `config.py` | Centralized paths, parameters, strategy settings |
| `eod_store.py` | DuckDB schema (`securities`, `eod_prices`, `eod_data` view) and upserts |
| `screening_readiness.py` | EOD freshness, sync-cursor rebase, retry/gap bookkeeping |
| `nse_eod_downloader/`, `bse_eod_downloader/` | Exchange downloaders and corporate-action adjustments |
| `fundamental_downloader.py` | Screener.in CSV download (HTTP) |
| `stock_analysis/src/screener.rs` | Mark Tight and Tight Flag rules |
| `stock_analysis/src/pipeline.rs` | Fundamentals join, ranking, CSV/watchlist/chart output |
| `stock_analysis/src/chart.rs` | PNG charts (plotters, embedded DejaVu fonts) |
| `install_rust_binary.py` | Installs the prebuilt engine from a `rust-v*` release |

### Config Details (config.py)
- `DATA_ROOT` = `~/Mark EMA and HTF` (or `MARK_DATA_ROOT` / `--data-root`)
- `DUCKDB_FILE` = `Database/NSE_BSE_EOD.duckdb`
- Run timestamp and dates use `MARKET_TIMEZONE` (Asia/Kolkata), not the machine clock
- `FLAG_LOOKBACK_DAYS` and `MAX_STALE_SESSIONS` are passed to the Rust engine
- Screener screen ID = 3427564
- Output: `qualified_stocks/<NSE|BSE>/<mark_tight|Tight Flag>/`
- Charts: `chart_images/<NSE|BSE>/<mark_tight|Tight Flag>/`
- Logs: `logs/screening_log_{timestamp}.txt`
- OpenTrader Pro watchlists in `~/.opentrader-pro/databases/<NSE|BSE>_EOD/watchlists/`

## Setup & Running

### Environment
```bash
# Linux x86_64:
./setup_linux.sh

# Apple Silicon macOS:
./setup_macos.sh

# Windows x64 PowerShell:
.\setup_windows.ps1
```

### Running
```bash
# Full pipeline:
./run_linux.sh        # or ./run_macos.sh, run_windows.cmd

# Tests:
python -m pytest -q tests
(cd stock_analysis && cargo test --release)

# Fundamentals download only (needs Screener login/screener.txt):
python fundamental_downloader.py
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
- **Symbols vs ISIN**: Price history is keyed by ticker; ISIN is the stable
  identity used for fundamentals and renames. A ticker handed to another
  issuer never keeps the previous owner's candles.
- **Charts**: Rust `plotters`; chart EMAs reuse `screener::ema_series`
- **Threading**: `sync_lock = threading.Lock()` for EOD sync
- **Retries**: `@retry()` decorator with exponential backoff in eod_downloader;
  a failed EOD file keeps the sync cursor on that session (see
  `presume_session_closed`)
- **Safety**: Always check `chart_dir.relative_to(BASE_DIR)` before `shutil.rmtree`
- **Dirs**: `config.ensure_dirs()` creates all output directories

## Screening Strategy Details

### Universe
Rows are joined to fundamentals by ISIN (ETFs and indices drop out).  A BSE
symbol is removed when its ISIN is actively trading on NSE.  A series whose
last candle is more than `MAX_STALE_SESSIONS` sessions behind its exchange is
skipped.  SME tickers (`_SME`) are excluded unless `--include-sme`/`--only-sme`.

### Mark setup (internal)
Conditions: close > EMA 50 > EMA 150 > EMA 200 AND close >= 80% of 52w high
AND close >= 150% of 52w low.  EMAs are SMA-seeded and rounded to 2 decimals.
Walk back from the latest candle to find the start of the active setup.

### History anchor
All indicators, rules and charts use only candles dated on or after the
exchange's latest session minus `HISTORY_DAYS` (700 calendar days, a Rust
constant in `screener.rs`).  The store keeps 730 days, so machines whose stores
start on different days still seed the EMAs on the same candle.  A series
needs at least 252 candles in that window.

### Mark Tight
The latest breakout inside the active Mark run: the most recent candle whose
close crossed above the prior 60-session Donchian high (close above it while
the previous close was not, or the run's first candle when it is already
above).  Each pullback below the level followed by a new cross starts a new
breakout.  Only the latest close must still be above that breakout's level.
That candle's date = `Mark_Tight_Date`.

### Tight Flag
Within the last `FLAG_LOOKBACK_DAYS` candles: a 90%+ run-up within 45
candles (pole longer than 3 candles, no >50% gap, <=40% circuit candles), a
close above the pole high within 15 candles of the peak, no low between the
peak and that breakout candle more than `MAX_FLAG_DEPTH` (20%) below the pole
high, and the latest close still above the pole high.  Latest close >= 5,
>= EMA 200 and within 45% of the 52w high.  Every candidate pole is checked;
the qualifying pole with the latest breakout candle is reported (ties: later
peak, then the start with the lowest low, then the later start).
`flag_*` columns describe the first 90% pole (stage-1 gate); `tight_*`,
`breakout_date` and the chart lines describe the reported pole.

### Ranking
Each output is sorted by `Days_Since_Breakout` (calendar days from the
exchange's latest stored session to the breakout, so a weekend or holiday run
gives the same numbers as the last trading day), then newer breakout, then
symbol.  Charts end on the symbol's last candle.  CSV and watchlist file names
still use the run timestamp.  CSVs add `Last_Date` and all Screener.in
columns; `EOD_Down_From_52w_High` is computed from EOD data.

These rules live in the Rust engine (0.4.0 and later); a machine gets them
only after installing a `rust-v*` release built from it.

## Known Issues & Gotchas

1. **NSE package**: The downloader uses BennyThadikaran's `nse` package
   with `server=True`; install the pinned `nse[server]` dependency through
   `requirements.txt`. `nsetools` is a different API and is not a substitute.

2. **Rust engine version**: users run the binary from the newest `rust-v*`
   GitHub Release, so Rust changes reach them only after a new tag.  Bump
   `stock_analysis/Cargo.toml` with each release; the run log prints the
   engine version and sha256 so machines can be compared.

3. **pathlib.Path.replace()**: Used atomically instead of `os.rename()` for portability.

4. **Saturday bhavcopy fix**: Weekend check added to skip Saturday (original code only skipped Sunday, causing 404 on Saturday and aborting entire sync loop).

5. **Existing data is not repaired automatically**: gaps from older syncs and
   pre-fix corporate-action or ticker-reuse damage stay in an existing DuckDB
   until repaired or rebuilt.

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
- [ ] NaN handling in indicator and screening code
- [ ] No stale imports or unused variables
- [ ] `python -m pytest -q tests` and `cargo test --release` pass
