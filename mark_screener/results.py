"""Fundamental merge, ranking, CSV export and chart dispatch."""

import logging
import os
import shutil
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

import config
from config import CURRENT_TIMESTAMP
from .scoring import add_priority_score


def write_watchlist(frame: pd.DataFrame, suffix: str) -> Path | None:
    """Write one ranked OpenTrader Pro watchlist for a result category."""
    if not config.WATCHLISTS_ENABLED or frame.empty:
        return None

    exchange = ""
    if "Exchange" in frame.columns and not frame["Exchange"].dropna().empty:
        exchange = str(frame["Exchange"].dropna().iloc[0]).strip().upper()
    if exchange not in {"NSE", "BSE"}:
        exchange = suffix.split("_", 1)[0].strip().upper()
    if exchange not in {"NSE", "BSE"}:
        raise ValueError(f"Cannot determine watchlist exchange from {suffix!r}")

    watchlist_dir = config.get_watchlist_dir(exchange)
    watchlist_dir.mkdir(parents=True, exist_ok=True)
    watchlist_path = (
        watchlist_dir / f"{CURRENT_TIMESTAMP}_{suffix.lower()}.txt"
    )
    symbols = (
        frame["symbol"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .loc[lambda values: values.ne("")]
    )
    if config.STRIP_SME_WATCHLIST_SUFFIX:
        symbols = symbols.str.replace(r"_SME$", "", regex=True)
    symbols = symbols.drop_duplicates()
    watchlist_path.write_text(
        "".join(f"{symbol}\n" for symbol in symbols),
        encoding="utf-8",
    )
    logging.info(f"Watchlist created: {watchlist_path} ({len(symbols)} symbols)")
    return watchlist_path


def process_results(df_results, fundamental_df, suffix, out_dir, chart_dir, date_col, data_dir, plot_days=500, eod_data_dict=None):
    df_results['isin'] = df_results['isin'].astype(str).str.strip().str.upper()
    fundamental_df['ISIN Code'] = fundamental_df['ISIN Code'].astype(str).str.strip().str.upper()
    
    merged_df = pd.merge(df_results, fundamental_df, left_on="isin", right_on="ISIN Code", how="left")
    merged_df = merged_df.dropna(subset=["ISIN Code"])
    if merged_df.empty:
        logging.info(f"No stocks matched for {suffix}.")
        return None
 
    merged_df = add_priority_score(merged_df)
    merged_df = merged_df[~merged_df[date_col].isna()]
    df_sorted = merged_df.sort_values(by="Priority_Score", ascending=False)
    df_sorted["Plot Start"] = pd.to_datetime(df_sorted[date_col]) - pd.to_timedelta(plot_days, unit="D")
    df_sorted["Plot End"] = pd.to_datetime(date.today())
    df_sorted["Days_In_Trend"] = (pd.to_datetime(df_sorted["date"]) - pd.to_datetime(df_sorted[date_col])).dt.days
    if "EMA_50" in df_sorted.columns and "EMA_200" in df_sorted.columns:
        df_sorted["Trend_Strength"] = ((df_sorted["EMA_50"] - df_sorted["EMA_200"]) / df_sorted["EMA_200"] * 100).round(2)
    else:
        df_sorted["Trend_Strength"] = 0.0
    df_sorted = df_sorted.reset_index(drop=True)
    df_sorted["Index"] = df_sorted.index
 
    csv_columns = ["Index", "symbol", "Priority_Score", "Tradeable", "Turnover_cr", "Days_In_Trend", "Trend_Strength"]
    excluded = {
        "Index", "symbol", "date", "close", "EMA_50", "EMA_150", "EMA_200",
        "52 week high", "52 week low",
        "Down from 52w high_x", "Donchian_60_High", "Donchian_60_Base", "Tight_Breakout",
        "Plot Start", "Plot End", "Days_In_Trend", "Trend_Strength",
        "Priority_Score", "Tradeable", "Turnover_cr",
        "flag_start_date", "flag_peak_date", "tight_start_date", "tight_peak_date", "breakout_date",
    }
    csv_columns.extend(col for col in df_sorted.columns if col not in excluded)
    csv_filepath = Path(out_dir) / f"{CURRENT_TIMESTAMP}_{suffix}_qualified.csv"
    df_sorted[csv_columns].to_csv(csv_filepath, index=False)
    logging.info(f"CSV exported: {csv_filepath} ({len(df_sorted)} stocks)")
    write_watchlist(df_sorted, suffix)
 
    logging.info(f"Generating charts for {suffix} in {chart_dir}...")
    try:
        Path(chart_dir).relative_to(config.CHART_DIR)
    except ValueError:
        logging.error(f"Refusing to rmtree {chart_dir} — not under CHART_DIR")
        return None
    if Path(chart_dir).exists():
        shutil.rmtree(chart_dir)
    Path(chart_dir).mkdir(parents=True, exist_ok=True)
    
    import time
    t0_charts = time.time()

    # Try Rust charting if enabled
    if getattr(config, "USE_RUST_CHARTS", False):
        import subprocess
        rust_bin = config.RUST_BINARY
        if rust_bin.exists():
            temp_csv = config.BASE_DIR / f'temp_chart_tasks_{suffix}.csv'
            try:
                logging.info(f"Using RUST charting engine for {suffix}...")
                df_sorted.to_csv(temp_csv, index=False)
                
                # Command arguments: tasks_csv duckdb_file output_dir
                result = subprocess.run([
                    str(rust_bin),
                    str(temp_csv),
                    str(config.DUCKDB_FILE),
                    str(chart_dir)
                ], capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
                
                if result.stderr:
                    logging.warning(f"Rust charting stderr:\n{result.stderr}")
                
                if temp_csv.exists():
                    temp_csv.unlink()
                    
                if result.returncode == 0:
                    elapsed_charts = time.time() - t0_charts
                    logging.info(f"✓ Generated {len(df_sorted)} charts using RUST for {suffix} in {elapsed_charts:.2f}s")
                    return df_sorted
                else:
                    logging.error(f"Rust charting failed with exit code {result.returncode}: {result.stderr}")
                    logging.warning("Falling back to Python charting...")
            except Exception as e:
                logging.error(f"Error during Rust charting execution: {e}")
                if temp_csv.exists():
                    temp_csv.unlink()
                logging.warning("Falling back to Python charting...")
        else:
            logging.warning(f"Rust binary not found at {rust_bin}. Falling back to Python charting...")

    # Fallback / Default Python Charting
    # Matplotlib and mplfinance are expensive imports.  Keep them out of the
    # normal Rust-backed pipeline and import the Python renderer only if the
    # Rust chart command is unavailable or fails.
    from .charts import plot_save_chart

    # Prepare task arguments, passing the pre-loaded EOD DataFrame if available
    tasks = []
    for _, row in df_sorted.iterrows():
        sym = row['symbol'].upper()
        sym_df = eod_data_dict.get(sym) if eod_data_dict is not None else None
        tasks.append((row, chart_dir, date_col, data_dir, sym_df))
        
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        list(executor.map(plot_save_chart, tasks))
    elapsed_charts = time.time() - t0_charts
    logging.info(f"✓ Generated {len(df_sorted)} charts for {suffix} in {elapsed_charts:.2f}s")
    return df_sorted
