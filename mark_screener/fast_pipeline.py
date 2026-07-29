"""Single-pass Rust screening orchestration.

The legacy entry points start four Python interpreters and make four Rust
screening calls (Mark + Flag for both exchanges).  The Rust engine already
supports calculating both strategies in one pass, so this module runs one
combined call per exchange and performs the shared post-processing in the
orchestrator process.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import pandas as pd

import config
from .results import process_results


def _screen_all_exchanges(include_sme=False, only_sme=False):
    """Run both strategies and exchanges in one Rust process."""
    cmd = [
        str(config.RUST_BINARY),
        "screen",
        str(config.DUCKDB_FILE),
        str(config.FUNDAMENTAL_FILE),
        "ALL",
        str(config.OUT_DIR),
        "--flag-lookback",
        str(config.FLAG_LOOKBACK_DAYS),
    ]
    if include_sme:
        cmd.append("--include-sme")
    if only_sme:
        cmd.append("--only-sme")

    started = time.perf_counter()
    # The Rust engine emits UTF-8; decoding with the Windows locale codepage
    # raises UnicodeDecodeError inside subprocess' reader thread.
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Combined Rust screening failed: {detail}")

    datasets = {}
    for exchange in ("NSE", "BSE"):
        json_path = (
            config.OUT_DIR / exchange / f"{exchange}_rust_screened.json"
        )
        with json_path.open("r", encoding="utf-8") as handle:
            datasets[exchange] = json.load(handle)
    return datasets, elapsed


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
                "breakout_date",
                data_dir,
                200,
            )
        )

    return jobs


def _reset_chart_dirs():
    """Clear only the two supported strategy image directories."""
    for exchange in ("NSE", "BSE"):
        dirs = config.get_output_dirs(exchange)
        legacy_mark_dir = config.CHART_DIR / exchange / "mark"
        legacy_mark_dir.relative_to(config.CHART_DIR)
        if legacy_mark_dir.exists():
            shutil.rmtree(legacy_mark_dir)
        for key in ("mark_tight_chart", "tight_flag_chart"):
            chart_dir = Path(dirs[key])
            chart_dir.relative_to(config.CHART_DIR)
            if chart_dir.exists():
                shutil.rmtree(chart_dir)
            chart_dir.mkdir(parents=True, exist_ok=True)


def _render_combined_charts(rendered_jobs, logger):
    """Render every exchange/strategy chart through one Rust process."""
    started = time.perf_counter()
    task_frames = []
    for frame, job in rendered_jobs:
        if frame is None or frame.empty:
            continue
        tasks = frame.copy(deep=False)
        tasks["Chart Output"] = str(job[4])
        task_frames.append(tasks)
    if not task_frames:
        return

    all_tasks = pd.concat(task_frames, ignore_index=True, sort=False)
    with tempfile.NamedTemporaryFile(
        prefix="screen_chart_tasks_",
        suffix=".csv",
        dir=config.BASE_DIR,
        delete=False,
    ) as handle:
        task_path = Path(handle.name)
    try:
        all_tasks.to_csv(task_path, index=False)
        result = subprocess.run(
            [
                str(config.RUST_BINARY),
                "chart",
                str(config.DUCKDB_FILE),
                str(task_path),
                str(config.CHART_DIR),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Combined Rust chart rendering failed: {detail}")
        logger.info(
            f"  ✓ Generated {len(all_tasks)} charts in one Rust batch in "
            f"{time.perf_counter() - started:.2f}s."
        )
    finally:
        task_path.unlink(missing_ok=True)


def run_fast_screening(logger, include_sme=False, only_sme=False):
    """Screen NSE/BSE and create ranked CSV/chart outputs with minimal repeats."""
    if not config.RUST_BINARY.exists():
        return False

    started = time.perf_counter()
    datasets, screening_elapsed = _screen_all_exchanges(
        include_sme, only_sme
    )
    for exchange in ("NSE", "BSE"):
        mark_count = len(datasets[exchange]["mark_tight_qualified"])
        flag_count = len(datasets[exchange]["tight_flag_qualified"])
        logger.info(
            f"  ✓ {exchange} screened "
            f"{datasets[exchange]['selected_symbols']} symbols — "
            f"Mark Tight: {mark_count}, "
            f"Tight Flag: {flag_count}"
        )
        if (
            datasets[exchange]["selected_symbols"]
            and not datasets[exchange]["history_ready"]
        ):
            logger.warning(
                f"  {exchange} screening ran, but none of its selected symbols "
                f"has the required {config.MIN_HISTORY_DAYS} candles "
                f"(maximum available: {datasets[exchange]['max_history']}); "
                f"no {exchange} stock can qualify yet."
            )
    logger.info(
        f"  ✓ One Rust process screened both exchanges in "
        f"{screening_elapsed:.2f}s."
    )

    fundamental_df = pd.read_csv(config.FUNDAMENTAL_FILE)
    jobs = []
    for exchange in ("NSE", "BSE"):
        jobs.extend(_result_jobs(exchange, datasets[exchange], fundamental_df))

    _reset_chart_dirs()
    rendered_jobs = []
    output_started = time.perf_counter()
    if jobs:
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = {
                executor.submit(
                    process_results,
                    *job,
                    render_charts=False,
                ): job
                for job in jobs
            }
            for future in as_completed(futures):
                rendered_jobs.append((future.result(), futures[future]))
        logger.info(
            f"  ✓ Wrote four fundamental-enriched outputs in "
            f"{time.perf_counter() - output_started:.2f}s."
        )
        _render_combined_charts(rendered_jobs, logger)

    logger.info(
        f"  ✓ Combined screening and output generation completed in "
        f"{time.perf_counter() - started:.2f}s"
    )
    return True
