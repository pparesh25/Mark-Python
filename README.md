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
python rebuild_eod_database.py --fill-missing   # add the sessions an existing store lacks, in place
```

A rebuild asks NSE about every weekend day too, so a Saturday or Sunday
session no calendar listed (the 2025-02-01 Union Budget session was missing
from the first rebuild) is stored. `--fill-missing` downloads into the existing
store the sessions it lacks on either exchange over its whole range, under the
symbols it has today, and decides the factors again; do not run it while a
scheduled sync is writing the store.

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
of the same company carry the same factor. A list fetch that succeeds replaces
the stored rows it covered (an exchange that revised an ex-date or a ratio
leaves no stale row behind); a failed or empty fetch deletes nothing. Each
exchange's whole two-year list is fetched again when its last full fetch is
more than 30 days old (`action_list_history`). Dates are India's (IST). In order:

1. a hand decision in `Database/metadata/corporate_action_overrides.csv`
   (`Exchange,Symbol,Date,Factor,Note`): Exchange NSE or BSE, a Date that
   parses, and Factor a plain number from 0.001 to 1000 (`0.5`, not `1/2`,
   `50%` or `0.5x`). Any other row, and a row that matches no stored trading
   day of that symbol, is skipped with a warning in the run log;
2. on BSE, the NSE twin's factor (same ISIN), on BSE's first trading day on or
   after NSE's day however late, flagged when BSE's open does not show it; BSE's
   own list adds no second factor within 30 days of one the twin brings;
3. the exchange's own list -- NSE matched by ISIN, BSE by scrip code only, never
   by today's name -- when the day's open confirms it (within 25%; a 1:10 bonus
   is taken from the list when the open did not gap);
4. a listed demerger or capital reduction: the open over the previous close;
5. an open below 0.55x the previous close that nothing explains, snapped to
   the nearest common split/bonus ratio; 0.55-0.80x or above 1.8x is left
   alone and written to `Database/corporate_action_review.csv` for a look.

A symbol back after more than 366 days without a trade is a new listing (no
gap, no factor) and is flagged; so is a listed action that matched no trading
day within 10 days although the symbol traded again later. Before the factors
are written each must be finite and above 0, or the previous ones stay.

Volume and delivery are adjusted like prices. Two years of raw prices are kept
(`config.EOD_RETAIN_YEARS`); a thinly traded stock with fewer than 252 sessions
in that window is skipped by the screener. `compact_price_database.py --apply`
copies the store into a fresh file to give back the space deleted rows hold.
`run_screener.py --skip-sync` screens a store made elsewhere without touching it.

An EOD network failure is logged and screening may continue against local
data.  A session whose file could not be downloaded is retried on the next run
instead of being skipped; it is treated as an unlisted holiday only after
three failed runs and three days.  Always check the run log for
`EOD update incomplete/unavailable` and for `price data is missing ...
session(s)` (checked over the whole stored range) before treating the database
as current.  A delivery report NSE publishes late is written into the stored
day when it comes; the day stays pending until the store has taken it.  The log also prints the
Rust engine version and sha256, so two machines can confirm they run the same
engine.

## Outputs

All ordinary outputs live below the configured data root:

```text
qualified_stocks/<exchange>/<strategy>/
chart_images/<exchange>/<strategy>/
logs/screening_log_YYYYMMDD_HHMMSS.txt
```

CSV files are timestamped.  Strategy chart directories are regenerated on
each run.

There is no Python calculation or chart fallback. A missing or failed Rust
binary stops local processing instead of silently switching to a second rule
implementation.
