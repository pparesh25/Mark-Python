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
   DuckDB store through `eod_store.py`.  Missing sessions over the whole
   stored range are logged; `rebuild_eod_database.py --fill-missing` adds them.
   After the sync `eod_maintenance.py` refreshes the action lists and decides
   the corporate-action factors (`price_adjustments.py`, IST dates): on a
   version 3 store only the symbols whose decisions can have changed
   (`adjustment_ledger.py`), with a full verify/repair against the full rebuild
   once a week; a version 2 store is migrated on its first run.
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
| `eod_store.py` | DuckDB schema v3 (`raw_prices`, `eod_prices` table, `adjustment_ledger`, `gap_history`, `symbol_state`, `eod_data` view), upserts, retention |
| `price_adjustments.py` | The factor rules (`decide_exchange`) and the full rebuild (`rebuild`, the oracle) |
| `adjustment_ledger.py` | Incremental maintenance with its dependency rules, checks, full verify/repair, v2 -> v3 migration |
| `eod_maintenance.py` | After-sync step (retention, migration, lists, decisions) and `verify_store` (`--verify`) |
| `screening_readiness.py` | EOD freshness, sync-cursor rebase, retry/gap bookkeeping |
| `nse_eod_downloader/`, `bse_eod_downloader/` | Exchange downloaders and corporate-action adjustments |
| `fundamental_downloader.py` | Screener.in CSV download (HTTP) |
| `stock_analysis/src/screener.rs` | Mark Tight and Tight Flag rules |
| `stock_analysis/src/pipeline.rs` | Fundamentals join, ranking, CSV/watchlist/chart output |
| `stock_analysis/src/chart.rs` | PNG charts (plotters, embedded DejaVu fonts) |
| `install_rust_binary.py` | Installs the prebuilt engine from a `rust-v*` release |
| `run_fingerprint.py` | Per-run input/output fingerprint JSON and the two-run comparison CLI |
| `run_scheduled.sh`, `schedule_setup.sh`, `Mark_Python.sh` | macOS launchd schedule and interpreter selection |

### Config Details (config.py)
- `DATA_ROOT` = `~/Mark EMA and HTF` (or `MARK_DATA_ROOT` / `--data-root`)
- `DUCKDB_FILE` = `Database/NSE_BSE_EOD.duckdb`
- Run timestamp and dates use `MARKET_TIMEZONE` (Asia/Kolkata), not the machine clock
- `FLAG_LOOKBACK_DAYS` and `MAX_STALE_SESSIONS` are passed to the Rust engine
- Screener screen ID = 3427564
- Output: `qualified_stocks/<NSE|BSE>/<mark_tight|Tight Flag>/`
- Charts: `chart_images/<NSE|BSE>/<mark_tight|Tight Flag>/`
- Logs: `logs/screening_log_{timestamp}.txt` and `logs/run_fingerprint_{timestamp}.json`
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

# Compare two runs (e.g. Windows vs Mac) -- see README "Comparing two machines":
python -m run_fingerprint run_fingerprint_A.json run_fingerprint_B.json
```

### Scheduler (macOS)
- `schedule_setup.sh` generates the LaunchAgent plist (`com.paresh.markscreener`)
  from its own directory and loads it (`launchctl bootstrap gui/$(id -u)`,
  fallback `load`); `schedule_uninstall.sh` removes it.  No static plist is tracked.
- Runs Monday-Friday at 19:30 IST plus at login; `SCHEDULE_HOUR`/`SCHEDULE_MINUTE`/
  `SCHEDULE_WEEKDAYS`/`SCHEDULE_TZ` and the retry window live only in
  `run_scheduled.sh`, which decides in IST; `schedule_setup.sh` converts the slot
  to the machine's local time for launchd.  It runs when the last successful
  run started before the latest passed slot; `--force`, `--dry-run`,
  `MARK_SCHEDULER_NOW` (epoch, also the simulated clock for retries) for tests.
- `run_screener.py` exit codes: 3 = screened data not current (only with
  `MARK_SCHEDULED=1`), 4 = another run holds `<data root>/mark_screener.lock`.
  The scheduler retries both every 30 minutes until 23:30 IST.  `--verify`
  (under the same lock) only runs the full verify/repair: 0 equal, 5 repaired,
  6 failed.
- `Mark_Python.sh` picks `$MARK_PYTHON`, then `.venv/bin/python`, then a conda env
  `mark_screener` under `~/miniforge3`, `~/miniconda3`, `~/anaconda3`,
  `/opt/homebrew/Caskroom/miniforge/base`; it never relies on `conda` on PATH.

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
The latest breakout that still holds inside the active Mark run.  A breakout
is a candle whose close crossed above the prior 60-session Donchian high
(close above it while the previous close was not, or the run's first candle
when it is already above); each pullback below the level followed by a new
cross starts a new breakout.  The reported breakout is the most recent one
whose level is still below the latest close, so a newer breakout the price
has fallen back from does not hide an older one that holds.  With none
holding the symbol does not qualify.  That candle's date = `Mark_Tight_Date`.
The run's first candle counts as a crossing only when the run really starts
there (the candle before it had all indicators and failed Mark); when the
walk-back stops at the indicator warm-up (index 251, the first 52-week value),
that candle must pass the normal test against the candle before it.
Every symbol the first-breakout rule (0.3.0) qualified still qualifies, with
the same or a later date, apart from differences the history anchor makes.

### Tight Flag
Gates on the latest candle: close >= 5, >= EMA 200 and within 45% of the 52w
high.  Inside the last `FLAG_LOOKBACK_DAYS` candles every candle j is a
candidate peak:
- start i = the lowest low (low > 0) among candles j-45 ..= j-4 inside the
  window (ties: the later candle), so a pole is 4 to 45 candles long;
- no high in i ..= j above high[j];
- run-up (high[j] - low[i]) / low[i] >= 90%;
- no open gap > 50% in i+1 ..= j, circuit candles (high == low) <= 40% of
  i ..= j;
- breakout k = the first candle in j+1 ..= j+15 with close > high[j]; no
  high in j+1 ..= k-1 above high[j] (a higher candle is its own candidate);
- flag depth: every low in j+1 ..= k-1 >= (1 - `MAX_FLAG_DEPTH`) * high[j]
  (20%; the peak and breakout candles' lows do not count);
- latest close > high[j].
The qualifying candidate with the latest k is reported (ties: the later j).
`flag_*` columns describe the earliest candidate whose pole (as defined above)
has a 90% run-up, whatever the later checks (stage-1 gate); `tight_*`,
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

5. **Two machines, different lists**: the engine is deterministic; differences
   come from each machine's store (latest session, missing sessions, v1 store,
   history start) or fundamentals file.  Compare the runs' fingerprint JSONs
   with `python -m run_fingerprint`; a `RESULTS MAY DIFFER` warning in the log
   means NSE/BSE as-of dates differed or the run fell back to local data.
   `requirements.txt` pins duckdb/numpy/pandas exactly for the same reason.

6. **Existing data is not repaired automatically**: gaps from older syncs and
   pre-fix corporate-action or ticker-reuse damage stay in an existing DuckDB
   until repaired or rebuilt (`rebuild_eod_database.py --fill-missing` adds
   missing sessions in place).

6. **Special sessions**: meta.json learns special sessions only from NSE
   circulars and keeps the current year's holiday list; sessions from before
   that are in `screening_readiness.KNOWN_SPECIAL_SESSIONS` (add new budget or
   Muhurat sessions there when a rebuild must know them). A Muhurat day is in
   NSE's holiday list as "Laxmi Pujan" and is still a session on both exchanges.

7. **Corporate-action overrides**: `Database/metadata/corporate_action_overrides.csv`
   rows need Date as `YYYY-MM-DD` and Factor as a plain number in [0.001, 1000]; bad or unmatched rows
   are skipped with a warning, and a non-finite or non-positive factor is
   refused before anything is written.

8. **Stored adjustments (store v3)**: `eod_prices` is a table, not a view.
   Anything that writes `raw_prices` should keep it in step: `upsert_daily_prices`
   writes the day's adjusted rows itself, renames move every derived table
   (`eod_store.DERIVED_TABLES`), a reused ticker drops them, `apply_retention`
   updates `symbol_state` (one transaction); another writer calls
   `eod_store.rematerialize` for its rows, or leaves it to the next maintenance:
   `symbol_state` sums a hash of every stored raw column per symbol, so any
   change to a stored row is found -- a day, open or close decides the symbol
   again, any other column only rewrites its adjusted rows. Correctness is defined as
   "equal to `price_adjustments.rebuild()`": a new dependency between symbols or
   days needs a rule in `adjustment_ledger.py` (see its docstring) and a case in
   `tests/test_adjustment_ledger.py`'s simulation; when unsure, decide more
   symbols. Bump `adjustment_ledger.RULE_VERSION` when a decision rule changes,
   so the next run rebuilds in full. The migration needs free disk for a copy
   of the store plus ~1.5x its size (v3 is ~80% larger than v2); a failed one is
   retried at most once a day (`<stem>.migration-failed.json`).

9. **Rollout of store v3**: code before v3 breaks on a v3 file (CatalogException:
   eod_prices is a table; its upserts write raw only and eod_data goes stale).
   Merge into `main` (the scheduler's checkout) before the new code runs on the
   default data root, and update all machines sharing it together. Roll back
   with `python -m eod_store --downgrade-to-v2 [PATH]` before starting old code.

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
