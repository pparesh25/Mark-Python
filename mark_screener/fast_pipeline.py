"""Single-pass Rust screening orchestration.

The legacy entry points start four Python interpreters and make four Rust
screening calls (Mark + Flag for both exchanges).  The Rust engine already
supports calculating both strategies in one pass, so this module runs one
combined call per exchange and performs the shared post-processing in the
orchestrator process.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import subprocess
import time

import pandas as pd

import config
from .results import process_results


def _symbol_allowed(symbol, include_sme, only_sme):
    is_sme = symbol.strip().upper().endswith("_SME")
    if only_sme:
        return is_sme
    return include_sme or not is_sme


def _load_filter_stats(include_sme=False, only_sme=False):
    """Reconcile the fundamental and exchange universes for visible run logs."""
    import duckdb

    valid_isins = set()
    with config.FUNDAMENTAL_FILE.open(
        "r", encoding="utf-8", errors="replace", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            isin = str(row.get("ISIN Code", "")).strip().upper()
            if isin and isin != "NAN":
                valid_isins.add(isin)

    conn = duckdb.connect(str(config.DUCKDB_FILE), read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT Exchange, Symbol, TRIM(UPPER(ISIN)) AS ISIN, COUNT(*) AS candles
            FROM eod_data
            WHERE Exchange IN ('NSE', 'BSE')
              AND NULLIF(TRIM(COALESCE(ISIN, '')), '') IS NOT NULL
            GROUP BY Exchange, Symbol, TRIM(UPPER(ISIN))
            """
        ).fetchall()
    finally:
        conn.close()

    nse_isins = {isin for exchange, _, isin, _ in rows if exchange == "NSE"}
    nse_matching = [
        row for row in rows if row[0] == "NSE" and row[2] in valid_isins
    ]
    bse_matching = [
        row for row in rows if row[0] == "BSE" and row[2] in valid_isins
    ]
    bse_overlap = [row for row in bse_matching if row[2] in nse_isins]
    bse_unique = [row for row in bse_matching if row[2] not in nse_isins]

    def symbol_history(records, apply_sme_filter=False):
        history = {}
        for _, symbol, _, candles in records:
            if apply_sme_filter and not _symbol_allowed(
                symbol, include_sme, only_sme
            ):
                continue
            clean_symbol = symbol.strip().upper()
            history[clean_symbol] = max(history.get(clean_symbol, 0), candles)
        return history

    nse_selected = symbol_history(nse_matching, apply_sme_filter=True)
    bse_selected = symbol_history(bse_unique, apply_sme_filter=True)

    return {
        "fundamental_isins": len(valid_isins),
        "nse_matching": len(symbol_history(nse_matching)),
        "bse_matching": len(symbol_history(bse_matching)),
        "bse_overlap": len(symbol_history(bse_overlap)),
        "bse_unique": len(symbol_history(bse_unique)),
        "bse_unique_isins": len({row[2] for row in bse_unique}),
        "NSE": {
            "selected": len(nse_selected),
            "history_ready": sum(
                candles >= config.MIN_HISTORY_DAYS
                for candles in nse_selected.values()
            ),
            "max_history": max(nse_selected.values(), default=0),
        },
        "BSE": {
            "selected": len(bse_selected),
            "history_ready": sum(
                candles >= config.MIN_HISTORY_DAYS
                for candles in bse_selected.values()
            ),
            "max_history": max(bse_selected.values(), default=0),
        },
    }


def _log_filter_stats(logger, stats):
    logger.info("=" * 64)
    logger.info("FILTERING LOGS & STOCK COUNT SUMMARY:")
    logger.info(
        f"  * Total unique ISINs in fundamental file: "
        f"{stats['fundamental_isins']}"
    )
    logger.info(
        f"  * NSE symbols matching fundamental ISIN: {stats['nse_matching']}"
    )
    logger.info(
        f"  * BSE symbols matching fundamental ISIN (total): "
        f"{stats['bse_matching']}"
    )
    logger.info(
        f"  * BSE symbols skipped because their ISIN exists in NSE: "
        f"{stats['bse_overlap']}"
    )
    logger.info(
        f"  * BSE-only symbols before SME filter: {stats['bse_unique']} "
        f"({stats['bse_unique_isins']} unique ISINs)"
    )
    logger.info(
        f"  * Final screening universe: NSE={stats['NSE']['selected']}, "
        f"BSE-only={stats['BSE']['selected']}"
    )
    logger.info(
        f"  * History-ready (minimum {config.MIN_HISTORY_DAYS} candles): "
        f"NSE={stats['NSE']['history_ready']}, "
        f"BSE-only={stats['BSE']['history_ready']}"
    )
    logger.info("=" * 64)


def _screen_exchange(exchange, include_sme=False, only_sme=False):
    """Run both strategies for one exchange and return its decoded dataset."""
    out_dir = config.OUT_DIR / exchange
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(config.RUST_BINARY),
        "screen",
        str(config.DUCKDB_FILE),
        str(config.FUNDAMENTAL_FILE),
        exchange,
        str(out_dir),
        "--flag-lookback",
        str(config.FLAG_LOOKBACK_DAYS),
    ]
    if include_sme:
        cmd.append("--include-sme")
    if only_sme:
        cmd.append("--only-sme")

    started = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{exchange} Rust screening failed: {detail}")

    json_path = out_dir / f"{exchange}_rust_screened.json"
    with json_path.open("r", encoding="utf-8") as handle:
        return exchange, json.load(handle), elapsed


def _result_jobs(exchange, dataset, fundamental_df):
    """Build post-processing jobs for the non-empty result categories."""
    dirs = config.get_output_dirs(exchange)
    data_dir = config.DATA_DIR if exchange == "NSE" else config.BSE_DATA_DIR
    jobs = []

    mark_tight = pd.DataFrame(dataset["mark_tight_qualified"])
    if not mark_tight.empty:
        mark_tight["Exchange"] = exchange
        mark_tight = mark_tight.rename(
            columns={
                "ema_50": "EMA_50",
                "ema_150": "EMA_150",
                "ema_200": "EMA_200",
                "high_52w": "52 week high",
                "low_52w": "52 week low",
                "mark_date": "Mark_Date",
                "mark_tight_date": "Mark_Tight_Date",
                "down_52w_high": "Down from 52w high",
            }
        )
        jobs.append(
            (
                mark_tight,
                fundamental_df.copy(deep=False),
                f"{exchange.lower()}_mark_tight",
                dirs["mark_tight_out"],
                dirs["mark_tight_chart"],
                "Mark_Tight_Date",
                data_dir,
                500,
            )
        )

    tight_flag = pd.DataFrame(dataset["tight_flag_qualified"])
    if not tight_flag.empty:
        tight_flag["Exchange"] = exchange
        jobs.append(
            (
                tight_flag,
                fundamental_df.copy(deep=False),
                f"{exchange.lower()}_tight_flag",
                dirs["tight_flag_out"],
                dirs["tight_flag_chart"],
                "tight_flag_date",
                data_dir,
                200,
            )
        )

    return jobs


def run_fast_screening(logger, include_sme=False, only_sme=False):
    """Screen NSE/BSE and create ranked CSV/chart outputs with minimal repeats."""
    if not config.RUST_BINARY.exists():
        return False

    started = time.perf_counter()
    datasets = {}
    timings = {}

    # DuckDB supports concurrent read-only connections.  NSE is substantially
    # larger than BSE-only, so overlapping both scans hides most BSE latency.
    with ThreadPoolExecutor(max_workers=3) as executor:
        stats_future = executor.submit(
            _load_filter_stats, include_sme, only_sme
        )
        futures = [
            executor.submit(_screen_exchange, exchange, include_sme, only_sme)
            for exchange in ("NSE", "BSE")
        ]
        for future in as_completed(futures):
            exchange, dataset, elapsed = future.result()
            datasets[exchange] = dataset
            timings[exchange] = elapsed

        stats = stats_future.result()

    _log_filter_stats(logger, stats)
    for exchange in ("NSE", "BSE"):
        mark_count = len(datasets[exchange]["mark_tight_qualified"])
        flag_count = len(datasets[exchange]["tight_flag_qualified"])
        logger.info(
            f"  ✓ {exchange} screened {stats[exchange]['selected']} symbols "
            f"in {timings[exchange]:.2f}s — Mark Tight: {mark_count}, "
            f"Tight Flag: {flag_count}"
        )
        if stats[exchange]["selected"] and not stats[exchange]["history_ready"]:
            logger.warning(
                f"  {exchange} screening ran, but none of its selected symbols "
                f"has the required {config.MIN_HISTORY_DAYS} candles "
                f"(maximum available: {stats[exchange]['max_history']}); "
                f"no {exchange} stock can qualify yet."
            )

    fundamental_df = pd.read_csv(config.FUNDAMENTAL_FILE)
    jobs = []
    for exchange in ("NSE", "BSE"):
        jobs.extend(_result_jobs(exchange, datasets[exchange], fundamental_df))

    # Each job writes to a distinct strategy/exchange directory.  Rust chart
    # rendering is CPU-bound, so qualified categories can safely overlap.
    if jobs:
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = [
                executor.submit(process_results, *job)
                for job in jobs
            ]
            for future in as_completed(futures):
                future.result()

    logger.info(
        f"  ✓ Combined screening and output generation completed in "
        f"{time.perf_counter() - started:.2f}s"
    )
    return True
