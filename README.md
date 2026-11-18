# Mark Screener

Cross-platform NSE/BSE Mark Tight and Tight Flag pipeline. Python is retained
only for the three network downloaders (NSE EOD, BSE EOD and Screener.in
fundamentals). After downloads finish, one precompiled Rust process performs
all local screening, enrichment, ranking, CSV/watchlist output and charting.
Windows, Linux, and Apple Silicon Macs do not need Rust, Cargo, CMake, or a
C/C++ compiler.

## Daily use in one minute

| What | Command / place |
|------|-----------------|
| Run everything now (Mac) | `./Mark_Python.sh` (the scheduler runs it Mon-Fri 19:30 IST) |
| Run on Windows / Linux | `run_windows.cmd` / `./run_linux.sh` |
| Check the price store against a full rebuild | `./Mark_Python.sh --verify` (0 equal, 5 repaired, 6 failed) |
| Today's lists | `~/Mark EMA and HTF/qualified_stocks/<NSE|BSE>/<mark_tight|Tight Flag>/` |
| Charts | `~/Mark EMA and HTF/chart_images/<NSE|BSE>/...` |
| Log of a run | `~/Mark EMA and HTF/logs/screening_log_<timestamp>.txt` |
| BSE corporate actions (weekly, by hand) | save BSE's CSV in `~/Mark EMA and HTF/Database/BSE corporate actions/` -- see below |
| Gaps to look at | `~/Mark EMA and HTF/Database/corporate_action_review.csv` |
| Compare two machines | `python -m run_fingerprint A.json B.json` |

A run keeps qualified CSVs and run logs for 60 days (`MARK_OUTPUT_RETAIN_DAYS`,
0 keeps everything); charts are replaced on every run.

### BSE corporate actions by hand

Since 29 October 2026 BSE's bot protection refuses the scripted download of
its corporate-action list (HTTP 403), while its website still serves the same
CSV. NSE's list still arrives on its own and covers every company listed on
both exchanges; BSE's own list matters for BSE-only scrips. Once a week (and
whenever the log says `BSE CORPORATE-ACTION LIST IS STALE`):

1. open <https://www.bseindia.com/corporates/corporate_act>;
2. Segment **Equity**, Select Date **Ex Date**, From = 60 days ago, To = 45
   days ahead (the warning prints both dates), **every other filter left at
   All** (Purpose, industry, security), Submit, then **Download** (CSV);
3. save the file, unopened, in `~/Mark EMA and HTF/Database/BSE corporate actions/`
   (a file re-saved by Excel changes its dates and is rejected). Each machine
   that runs the screener needs the file in its own folder.

The next run imports it exactly like a downloaded list (rows of the scrips it
lists inside its ex-date range are replaced, so a revised date or ratio is not
applied twice), decides the factors again, and moves the file to `imported/`
(a file that is not BSE's list goes to `rejected/`). The automatic download is
still tried on every run and is used again as soon as BSE allows it. A file
whose rows are all one kind (a Purpose filter was set) is only added, never
used to replace rows; a file that is open elsewhere stays and is tried again.
A full rebuild keeps the stored BSE rows when BSE refuses its list.

## Supported runtime

- Windows x64 with 64-bit Python 3.10-3.13 (Python 3.11 recommended)
- Linux x86_64 with Python 3.10-3.13 (Ubuntu 22.04+ recommended)
- Apple Silicon macOS with Python 3.10-3.13 (Python 3.11 recommended)

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
symbols it has today, and decides every factor again (the full rebuild); do not
run it while a scheduled sync is writing the store. A rebuild makes a version 3
store directly.

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

The pipeline runs **Monday-Friday at 19:30 India time (IST)**, and at login.
19:30 rather than 18:00: 18:00 IST is the earliest time a day's EOD may be
downloaded, and the NSE/BSE files are often published later, so a run at
exactly 18:00 often screened the previous session from local data.
`schedule_setup.sh` generates the plist (label `com.paresh.markscreener`) from
its own directory -- the project can live anywhere -- writes it to
`~/Library/LaunchAgents`, loads it with `launchctl bootstrap` (falling back to
`launchctl load`) and checks with `launchctl print` that it is loaded, failing
otherwise.  It refuses to reinstall while a run is in progress unless given
`--force`.  launchd fires calendar slots in the machine's local time, so 19:30
IST is converted to local time when the plist is generated and printed (on an
IST machine it stays 19:30); rerun `schedule_setup.sh` after changing the
machine's time zone or across a local daylight-saving change.  The time, the
weekdays and the retry window are set once, at the top of `run_scheduled.sh`.

`run_scheduled.sh` decides in IST whatever the machine's zone.  It runs the
pipeline only when the last successful run (`logs/.last_scheduled_run` in the
project: start time as epoch seconds plus IST text) started before the most
recent scheduled slot that has already passed: Monday 08:00 after Friday's run
does nothing, Monday 08:00 after a missed Friday runs at once.  Weekends never
have a slot of their own; a Saturday or Sunday start runs only to catch up a
missed Friday slot.  A run that screened data that is not current (exit 3: a
sync stopped, data older than the target, or NSE/BSE as-of dates differ) or
found another run holding the run lock (exit 4) is not recorded as done: it is
retried every 30 minutes until 23:30 IST, after which that slot is given up and
the next slot runs normally.  A manual `run_macos.sh` run always exits 0 in
those cases (the codes are only reported when `MARK_SCHEDULED=1`, which the
scheduler sets).  `./run_scheduled.sh --force` runs now; `--dry-run` only prints
the decision.  Scheduler output goes to `logs/scheduler_stdout.log` and
`scheduler_stderr.log`.

Only one run at a time: `run_screener.py` holds `<data root>/mark_screener.lock`
(PID, host and start time) while it runs.  A second run -- a manual run during
the scheduled one, or two launchers at once -- logs who holds it and exits 4.
A lock whose process is gone, or older than six hours, is taken over.

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

`./Mark_Python.sh` (macOS, used by the scheduler) picks the Python itself
(`$MARK_PYTHON`, `.venv`, or the conda env `mark_screener`). All launchers pass extra arguments to `run_screener.py`:

```text
--include-sme
--only-sme
--data-root PATH
--watchlist-root PATH
--watchlists / --no-watchlists
--strip-sme-watchlist-suffix
--allow-stale-fundamentals
--verify            (only compare the price store with a full rebuild, repair it, exit)
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

### Breakout rules (Rust engine 0.4.0 and later)

- **Mark Tight** reports the latest breakout of the current Mark trend that
  still holds: of the days the close crossed above the prior 60-session high,
  the most recent one whose level is still below the latest close. A newer
  breakout the price fell back from does not hide an older one that holds.
- **Tight Flag** treats every candle of the last 90 as a possible pole peak.
  The pole starts at the lowest low 4 to 45 candles before the peak, no candle
  of the pole trades above the peak, and the peak is at least 90% above that
  low (no >50% opening gap, at most 40% circuit candles). The breakout is the
  first close above the peak within 15 candles; until then no candle trades
  above the peak or more than 20% below it, and the latest close is still
  above the peak. Of the qualifying poles, the one with the latest breakout
  (then the later peak) is reported.
- A Mark trend that already began before the first candle with a 52-week
  value has no breakout on that candle; only a later crossing counts.
- **Days_Since_Breakout** counts calendar days from the exchange's latest
  stored session, so a weekend or holiday run ranks like the last trading day.
  Charts end on the symbol's last candle.
- Indicators use only the last 700 calendar days before each exchange's latest
  session, so two machines with the same latest session get the same EMAs
  even when their stores begin on different days.

These rules are in the precompiled engine, so a machine gets them only after
installing a `rust-v*` release built from version 0.4.0 or later (see
`RUST_BINARIES.md`); the run log prints the engine version.

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
the stored rows it covered of the securities it lists (an exchange that revised
an ex-date or a ratio leaves no stale row behind); a security the answer leaves
out keeps its rows (BSE drops delisted and amalgamated scrips), and a failed or
empty fetch deletes nothing. Each
exchange's whole two-year list is fetched again when its last full fetch is
more than 30 days old (`action_list_history`). Dates are India's (IST). In order:

1. a hand decision in `Database/metadata/corporate_action_overrides.csv`
   (`Exchange,Symbol,Date,Factor,Note`): Exchange NSE or BSE, Date written
   `YYYY-MM-DD` (`2026-02-01`; `01/02/2026` is refused, it would read as 2
   January), and Factor a plain number from 0.001 to 1000 (`0.5`, not `1/2`,
   `50%` or `0.5x`). Any other row, and a row that matches no stored trading
   day of that symbol, is skipped with a warning in the run log;
2. on BSE, the NSE twin's factor (same ISIN), on BSE's first trading day on or
   after NSE's day however late, flagged when BSE's open does not show it. A
   BSE listing of the same ratio within 30 days is the same event, applied once
   (on the listing's day when only BSE's open there shows it); a listing of
   another ratio nearby is decided by the list rule and flagged. When NSE was
   away around the event (halted, or days missing from the store) BSE's own
   gap or list takes it, and the twin factor that arrives when NSE is back --
   on a BSE day whose open does not show it -- is set against the product of
   BSE's own factors since NSE's last day: refused when it is that product (one
   1:1 split had become 0.25, a bonus and a split 0.0625), reduced to the rest
   when BSE's open shows the rest (an event on NSE's first day back), refused
   otherwise; always flagged;
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
(`config.EOD_RETAIN_YEARS`); the engine reads the last 700 days of them, and a
thinly traded stock with fewer than 252 sessions in that window is skipped by
the screener. `compact_price_database.py --apply`
copies the store into a fresh file to give back the space deleted rows hold.
`run_screener.py --skip-sync` screens a store made elsewhere without touching it.

#### Stored adjustments (price store version 3)

Deciding every factor and recomputing 3.8 million adjusted rows took 12-24 s of
each run. The store now keeps what was decided: `adjustment_ledger` holds every
applied factor (one per symbol and day -- a second one, or a factor of 0, below
0 or not finite, cannot be stored), `eod_prices` holds the adjusted rows, and
`gap_history` the sure gaps of days that left the store (so the gap-noise rule
still counts the whole listing). An adjusted row is always the raw row times
the product of the symbol's later ledger factors, written from `raw_prices`,
never from another adjusted row. After a sync only the symbols whose decisions
can have changed are decided again -- a new day with a large gap, a day after a
halt, a backfilled or re-sent day, a changed list row or override, an NSE twin
whose factors changed, a renamed or reused ticker, rows aged out next to a
decision (`adjustment_ledger.py` lists every rule) -- with the same code as a
full rebuild, and their rows are rewritten in one transaction: about 1.5 s on
the live store. Before it commits, every touched row is checked against raw x
ledger and every factor day's adjusted gap against the raw gap over the factor
(a factor applied twice shows there); a failed check rolls everything back,
names the symbols in `corporate_action_review.csv` and runs the full rebuild.
Once a week the whole store is compared with a full rebuild and, on any
difference, logged and repaired from raw; `run_screener.py --verify` does just
that and exits (0 equal, 5 repaired, 6 failed, 4 another run holds the lock).

A version 2 store is migrated by its first run: the file is first copied to
`NSE_BSE_EOD.v2-backup.duckdb` beside it, then the new tables are filled and
checked in one transaction (about 30 s); the raw prices are not touched, and on
any failure the store stays version 2 and the log says so. The migration needs
free disk for the copy plus about 1.5 times the store (the adjusted rows make
the file about 80% larger: 124 MB became 221 MB, stored in day order like the
raw prices so that the days retention removes free whole blocks again; over 40
simulated daily runs it moved within 205-211 MB); with less it waits and says
so. The screener reads the same `eod_data` as before -- on a copy of the live
store the released engine wrote byte-identical CSVs and charts from the version
2 and the migrated version 3 file. A migration that fails is rolled back and
tried again at most once a day (`NSE_BSE_EOD.migration-failed.json` says when
and why; every run warns until it succeeds).

**Rollout order.** Code from before version 3 cannot use a version 3 file (its
`create_schema` stops on the `eod_prices` table; its sync would store raw rows
that `eod_data` never shows, so scheduled runs would screen stale data). So:
merge this into `main` -- the checkout the scheduler runs -- before any run of
the new code on the default data root, and update every machine that shares
the data root at the same time. To go back to older code, first turn the store
back into version 2 with the old code stopped:

```bash
python -m eod_store --downgrade-to-v2                 # the data root's store; or give a path
```

It drops the version 3 tables in one transaction, recreates the views and
decides the factors with the full rebuild; the raw prices are not touched.

An EOD network failure is logged and screening may continue against local
data.  A session whose file could not be downloaded is retried on the next run
instead of being skipped; it is treated as an unlisted holiday only after
three failed runs and three days.  Always check the run log for
`EOD update incomplete/unavailable` and for `price data is missing ...
session(s)` (checked over the whole stored range) before treating the database
as current.  A delivery report NSE publishes late is written into the stored
day when it comes; the day stays pending until the store has taken it.  The log also prints the
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
| `packages` | pandas, numpy and duckdb versions (pinned in `requirements.txt`); python, nse and httpx versions are recorded but informational |
| `engine` | Rust engine version, full sha256 and path |
| `settings` | `FLAG_LOOKBACK_DAYS`, `MAX_STALE_SESSIONS`, SME options, the 18:00 EOD cutoff |
| `data_as_of` | NSE/BSE latest local session, the eligible target, `as_of_mismatch`, `local_fallback` (screened older data than the target), `sync_stopped`, and the warnings |
| `fundamentals` | file name, sha256, modified time (IST), rows and distinct ISINs |
| `overrides` | whether `corporate_action_overrides.csv` exists, and its sha256 |
| `store.kind`, `store.schema_version` | `v3` / 3 for the current store (adjusted prices stored with a ledger); `v2` / 2 for a store not yet migrated; `v1` / 1 for an old store that still needs `rebuild_eod_database.py`; `external` for a store screened with `--skip-sync` (the lake export keeps adjusted prices in an `eod_prices` table) |
| `store.engine_input_hash` | one hash of everything the `eod_data` view the engine reads is computed from (v3: the stored `eod_prices` and `securities`; v2: raw prices, `adjustment_periods`, `securities`) -- equal values mean the engine saw the same data |
| `store.exchanges` | per exchange: rows, sessions, first/last date, symbols, and `content_hash` of the price table (`eod_prices` in v3 and external stores -- what the engine reads --, `raw_prices` in v2): symbol, OHLC to 1/10^6, volume, per session |
| `store.tables` | rows and a content hash of each other table (the ledger without its `Decided_At` time, `gap_history`, periods, actions, securities; NaN and infinity hashed as text); `maintenance_state` records when, not what, and is only counted |
| `store.exchange_actions` | rows of the exchanges' corporate-action lists per exchange |
| `store.sessions` | rows and hash per session -- the comparison names the sessions that differ |
| `outputs` | per list (`NSE/mark_tight`, ...): count, hash and the (symbol, breakout date) pairs from the written CSVs |
| `elapsed_seconds` | time the fingerprint took (about 1 s on 3.8 million rows) |

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
