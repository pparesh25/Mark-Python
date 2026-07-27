# Mark Screener

Cross-platform NSE/BSE Mark Tight and Tight Flag pipeline.  The normal runtime
uses a precompiled Rust engine, so Windows and Linux machines need Python but
do not need Rust, Cargo, CMake, or a C/C++ compiler.

## Supported runtime

- Windows x64 with 64-bit Python 3.10 or newer (Python 3.11 recommended)
- Linux x86_64 with Python 3.10 or newer (Ubuntu 22.04+ recommended)

The published Rust executable does not currently support ARM64 or Alpine/musl.

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

The minimum metadata files and the production DuckDB must be copied together.
The BSE corporate-action ledger is stored inside that DuckDB.

Create the credential file below the portable data root when automatic
fundamental downloads are required:

```text
~/Mark EMA and HTF/Screener login/screener.txt
```

On Windows the default resolves to:

```text
C:\Users\<username>\Mark EMA and HTF\Screener login\screener.txt
```

Line 1 is the Screener.in email and line 2 is the password.  Google Chrome
must also be installed for that download.  The credential file stays outside
the downloaded repository/ZIP and must never be committed.

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

Pass `--skip-rust` to either setup script only when the correct prebuilt
binary was already installed manually.

## Daily commands

```text
run_windows.cmd
./run_linux.sh
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

Every qualified result set writes a ranked watchlist through the shared result
pipeline, including the fast Rust path.  Symbols preserve their database
names, including `_SME`, by default so OpenTrader Pro resolves the correct
chart.  `--strip-sme-watchlist-suffix` is an explicit compatibility option
for a database whose SME tickers do not use that suffix.

## Fundamental freshness

The normal run uses `all-company_YYYYMMDD.csv` for the current date.  If that
file is absent, Selenium downloads it from Screener.in.  An older local file is
used only when `--allow-stale-fundamentals` is explicitly supplied.

## Pipeline

1. Inspect both exchanges in DuckDB and update only when an eligible EOD
   session is missing.
2. Select or download validated fundamentals.
3. Filter listed symbols by fundamental ISIN; remove NSE/BSE overlap from the
   BSE universe and apply the requested SME policy.
4. Screen NSE and BSE in parallel with Rust, rank results, export CSVs,
   OpenTrader Pro watchlists, charts, and a timestamped log.

Daily NSE/BSE updates retain full price history in DuckDB and the maintained
BSE text backup; they do not delete an older candle when a new session is
added.  The current production store starts with its previously compacted 600
rows and will grow to 601, 602, and onward.  The manual
`compact_price_database.py --retain-candles 600` utility remains available
only for an explicitly requested future compaction.

An EOD network failure is logged and screening may continue against local
data.  Always check the run log for `EOD update incomplete/unavailable` before
treating the database as current.

## Outputs

All ordinary outputs live below the configured data root:

```text
qualified_stocks/<exchange>/<strategy>/
chart_images/<exchange>/<strategy>/
logs/screening_log_YYYYMMDD_HHMMSS.txt
```

CSV files are timestamped.  Strategy chart directories are regenerated on
each run.

## Optional legacy fallback

The precompiled Rust engine is the supported fast path.  To install the larger
Python calculation/chart fallback as well:

```bash
python -m pip install -r requirements-legacy.txt
```
