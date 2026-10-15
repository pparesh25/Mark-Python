# Mark Screener

Cross-platform NSE/BSE Mark Tight and Tight Flag pipeline. Python is retained
only for the three network downloaders (NSE EOD, BSE EOD and Screener.in
fundamentals). After downloads finish, one precompiled Rust process performs
all local screening, enrichment, ranking, CSV/watchlist output and charting.
Windows, Linux, and Apple Silicon Macs do not need Rust, Cargo, CMake, or a
C/C++ compiler.

## Supported runtime

- Windows x64 with 64-bit Python 3.10 or newer (Python 3.11 recommended)
- Linux x86_64 with Python 3.10 or newer (Ubuntu 22.04+ recommended)
- Apple Silicon macOS with Python 3.10 or newer (Python 3.11 recommended)

The published Rust executable does not currently support Windows/Linux ARM64,
Intel macOS, or Alpine/musl.

## One-time setup

The GitHub repository deliberately excludes credentials and market data.
Copy the production data into the target user's home directory:

```text
~/Mark EMA and HTF/
└── Database/
    ├── NSE_BSE_EOD.duckdb
    ├── NSE_EOD/
    ├── BSE_EOD/
    ├── fundamental_files/
    └── metadata/
        ├── meta.json
        ├── bse_meta.json
        ├── isin.csv
        └── bse_isin.csv
```

The metadata files are needed; the DuckDB is not. When the price store is
missing, or is an old (version 1) store, the first run builds it from NSE's and
BSE's own daily files with `rebuild_eod_database.py` -- two years, about 30-40
minutes once -- and keeps an old file beside it as `NSE_BSE_EOD.v1-backup.duckdb`.
It can also be run by hand at any time:

```bash
python rebuild_eod_database.py            # add --data-root "/path/to/Mark EMA and HTF" for another root
```

Create the credential file below the portable data root when automatic
fundamental downloads are required:

```text
~/Mark EMA and HTF/Screener login/screener.txt
```

On Windows the default resolves to:

```text
C:\Users\<username>\Mark EMA and HTF\Screener login\screener.txt
```

Line 1 is the Screener.in email and line 2 is the password.  Fundamentals are
downloaded directly over HTTP, so no browser is required.  The credential file
stays outside the downloaded repository/ZIP and must never be committed.

Because the repository and releases are private, set `GITHUB_TOKEN` or
`GH_TOKEN` to a token with repository Contents read permission before setup.
The variable is only needed while installing/updating the Rust executable.

### Windows PowerShell

```powershell
$env:GITHUB_TOKEN = "github-token"
.\setup_windows.ps1
Remove-Item Env:GITHUB_TOKEN
.\run_windows.cmd
```

### Linux

```bash
export GITHUB_TOKEN="github-token"
./setup_linux.sh
unset GITHUB_TOKEN
./run_linux.sh
```

### Apple Silicon macOS

```bash
export GITHUB_TOKEN="github-token"
./setup_macos.sh
unset GITHUB_TOKEN
./run_macos.sh
```

Pass `--skip-rust` to a setup script only when the correct prebuilt
binary was already installed manually.

### Scheduled runs (macOS)

```bash
./schedule_setup.sh       # install / reinstall the LaunchAgent
./schedule_uninstall.sh   # remove it
```

The pipeline runs **Monday-Friday at 19:30** machine local time (IST), and at
login.  19:30 rather than 18:00: 18:00 IST is the earliest time a day's EOD
may be downloaded, and the NSE/BSE files are often published later, so a run
at exactly 18:00 often screened the previous session from local data.
`schedule_setup.sh` generates the plist (label `com.paresh.markscreener`) from
its own directory -- the project can live anywhere -- writes it to
`~/Library/LaunchAgents` and loads it with `launchctl bootstrap` (falling back
to `launchctl load` on old macOS).  The time and weekdays are set once, at the
top of `run_scheduled.sh`.

`run_scheduled.sh` runs the pipeline only when the last successful run
(`logs/.last_scheduled_run` in the project) is older than the most recent
scheduled slot that has already passed: Monday 08:00 after Friday's run does
nothing, Monday 08:00 after a missed Friday runs at once, weekends never run.
`./run_scheduled.sh --force` runs now; `--dry-run` only prints the decision.
Scheduler output goes to `logs/scheduler_stdout.log` and `scheduler_stderr.log`.

`Mark_Python.sh` (used by the scheduler) picks the interpreter in this order and
logs which one it used; launchd's PATH is minimal, so `conda` is never needed
on PATH:

1. `$MARK_PYTHON`, when set (it must be executable);
2. `<project>/.venv/bin/python` (made by `setup_macos.sh`);
3. a conda environment named `mark_screener` under `~/miniforge3`,
   `~/miniconda3`, `~/anaconda3` or `/opt/homebrew/Caskroom/miniforge/base`.

Otherwise it prints what it looked for and exits with an error.

## Daily commands

```text
run_windows.cmd
./run_linux.sh
./run_macos.sh
```

Both launchers pass extra arguments to `run_screener.py`:

```text
--include-sme
--only-sme
--data-root PATH
--watchlist-root PATH
--watchlists / --no-watchlists
--strip-sme-watchlist-suffix
--allow-stale-fundamentals
```

Examples:

```bash
./run_linux.sh --include-sme
./run_linux.sh --no-watchlists
./run_linux.sh --strip-sme-watchlist-suffix
./run_linux.sh --watchlist-root "/mnt/opentrader/.opentrader-pro"
./run_linux.sh --data-root "/mnt/market-data/Mark EMA and HTF"
```

```powershell
.\run_windows.cmd --include-sme
.\run_windows.cmd --no-watchlists
.\run_windows.cmd --strip-sme-watchlist-suffix
.\run_windows.cmd --watchlist-root "D:\OpenTrader\.opentrader-pro"
.\run_windows.cmd --data-root "D:\Market Data\Mark EMA and HTF"
```

Environment alternatives are also supported:

```text
MARK_DATA_ROOT
MARK_OPENTRADER_ROOT
MARK_WRITE_WATCHLISTS=0|1
MARK_STRIP_SME_WATCHLIST_SUFFIX=0|1
MARK_SCREENER_CREDS
MARK_RUST_BINARY
```

## Portable paths

Defaults are resolved with the active user's home directory on every OS:

```text
Market data:       ~/Mark EMA and HTF
Screener login:    ~/Mark EMA and HTF/Screener login/screener.txt
OpenTrader Pro:    ~/.opentrader-pro
NSE watchlists:    ~/.opentrader-pro/databases/NSE_EOD/watchlists
BSE watchlists:    ~/.opentrader-pro/databases/BSE_EOD/watchlists
```

Every qualified result set writes a deterministic, symbol-ordered watchlist
through the shared result pipeline, including the fast Rust path. Symbols preserve their database
names, including `_SME`, by default so OpenTrader Pro resolves the correct
chart.  `--strip-sme-watchlist-suffix` is an explicit compatibility option
for a database whose SME tickers do not use that suffix.

## Fundamental freshness

The normal run uses `all-company_YYYYMMDD.csv` for the current date.  If that
file is absent, the app downloads it directly from Screener.in with an
optimized two-request HTTP flow.  An older local file is used only when
`--allow-stale-fundamentals` is explicitly supplied.

## Pipeline

1. Inspect both exchanges in DuckDB and update only when an eligible EOD
   session is missing; then, every run, decide the corporate-action factors
   from the raw prices (below).
2. Select or download validated fundamentals.
3. Filter listed symbols by fundamental ISIN; remove NSE/BSE overlap from the
   BSE universe (only NSE listings that are still trading count), skip series
   whose last candle is more than `MAX_STALE_SESSIONS` (default 5) sessions
   old, and apply the requested SME policy.
4. Run one local Rust process that screens NSE/BSE, joins fundamentals, ranks
   by latest breakout and exports all CSVs, OpenTrader Pro watchlists and
   charts. Screening data is reused for charting, so DuckDB is not read again.

### Prices, splits, bonuses and demergers

The store keeps every session **as the exchange published it** (`raw_prices`,
with trades and delivered quantity) and never divides it in place. After every
sync, `eod_maintenance.py` fetches NSE's and BSE's corporate-action lists for
the days around today (60 back, 45 ahead -- an action listed late is still
caught), decides a factor for every split, bonus, consolidation and demerger
(`price_adjustments.py`), and the screener reads the raw prices times those
factors (`eod_data`, unchanged columns). So an action can never be applied
twice, a corrected list corrects the prices on the next run, and NSE and BSE
of the same company carry the same factor. In order:

1. a hand decision in `Database/metadata/corporate_action_overrides.csv`
   (`Exchange,Symbol,Date,Factor,Note`);
2. on BSE, the NSE twin's factor (same ISIN);
3. the exchange's own list -- NSE matched by ISIN, BSE by scrip code, never by
   today's name -- when the day's open confirms it (within 25%; a 1:10 bonus is
   taken from the list when the open did not gap);
4. a listed demerger or capital reduction: the open over the previous close;
5. an open below 0.55x the previous close that nothing explains, snapped to
   the nearest common split/bonus ratio; 0.55-0.80x or above 1.8x is left
   alone and written to `Database/corporate_action_review.csv` for a look.

Volume and delivery are adjusted like prices. Two years of raw prices are kept
(`config.EOD_RETAIN_YEARS`); a thinly traded stock with fewer than 252 sessions
in that window is skipped by the screener. `compact_price_database.py --apply`
copies the store into a fresh file to give back the space deleted rows hold.
`run_screener.py --skip-sync` screens a store made elsewhere without touching it.

An EOD network failure is logged and screening may continue against local
data.  A session whose file could not be downloaded is retried on the next run
instead of being skipped; it is treated as an unlisted holiday only after
three failed runs and three days.  Always check the run log for
`EOD update incomplete/unavailable` and for `price data is missing ... recent
session(s)` before treating the database as current.  The log also prints the
Rust engine version and sha256, so two machines can confirm they run the same
engine.  When NSE and BSE end on different sessions, or the run screens local
data because a sync stopped, the log shows a `RESULTS MAY DIFFER` warning where
it happens and again in the final summary.

## Comparing two machines

Two machines on the same commit and engine can still produce different lists:
the engine is deterministic, but each machine screens its own price store.  A
run before the day's files are published screens the previous session (one
session changed 35 of 293 qualifiers in October 2026); missing sessions, an old
version 1 store, a different history start or a fundamentals file downloaded at
another time do the same.  Every run therefore writes
`logs/run_fingerprint_YYYYMMDD_HHMMSS.json` next to its log and prints a short
`Run fingerprint` block.  To compare:

1. run both machines after 19:30 IST on the same trading day (the schedule does
   this), so both have the day's NSE and BSE files;
2. copy one fingerprint to the other machine and run

```bash
python -m run_fingerprint run_fingerprint_A.json run_fingerprint_B.json        # --all also lists times/paths
```

It prints only the fields that differ, names the sessions two stores disagree
on and the symbols only one machine qualified, and exits 1 when anything that
can change results differs.  The fields:

| Field | Meaning |
|-------|---------|
| `run` | run timestamp (IST), the machine's local time and zone, `platform.platform()`, the Python executable -- expected to differ |
| `packages` | Python, pandas, numpy and duckdb versions (pinned in `requirements.txt`) |
| `engine` | Rust engine version, full sha256 and path |
| `settings` | `FLAG_LOOKBACK_DAYS`, `MAX_STALE_SESSIONS`, SME options, the 18:00 EOD cutoff |
| `data_as_of` | NSE/BSE latest local session, the eligible target, `as_of_mismatch`, `local_fallback` (screened older data than the target), `sync_stopped`, and the warnings |
| `fundamentals` | file name, sha256, modified time (IST), rows and distinct ISINs |
| `overrides` | whether `corporate_action_overrides.csv` exists, and its sha256 |
| `store.schema_version` | 2 for the current store, 1 for an old store that still needs `rebuild_eod_database.py` |
| `store.exchanges` | per exchange: rows, sessions, first/last date, symbols, and `content_hash` of exactly what the engine reads (`eod_data`: symbol, date, OHLC to 1/10000, volume) |
| `store.tables` | rows and a content hash of each table (factors, actions, securities); `raw_prices` is only counted |
| `store.exchange_actions` | rows of the exchanges' corporate-action lists per exchange |
| `store.sessions` | rows and hash per session -- the comparison names the sessions that differ |
| `outputs` | per list (`NSE/mark_tight`, ...): count, hash and the (symbol, breakout date) pairs from the written CSVs |
| `elapsed_seconds` | time the fingerprint took (about 4-8 s on 3.8 million rows) |

The hashes use text whose form does not depend on the OS or the DuckDB
version, and they ignore row order, so equal hashes mean equal data on Windows,
macOS and Linux alike.  A failure while fingerprinting only logs a warning.

## Outputs

All ordinary outputs live below the configured data root:

```text
qualified_stocks/<exchange>/<strategy>/
chart_images/<exchange>/<strategy>/
logs/screening_log_YYYYMMDD_HHMMSS.txt
logs/run_fingerprint_YYYYMMDD_HHMMSS.json
```

CSV files are timestamped.  Strategy chart directories are regenerated on
each run.

There is no Python calculation or chart fallback. A missing or failed Rust
binary stops local processing instead of silently switching to a second rule
implementation.
