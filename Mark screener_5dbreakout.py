"""Mark Screener with 5-day breakout window + Priority Score.

Derived from ``Mark screener.py`` with two changes applied per
``mark_screener_changes_guide.md``:

  §3  — Stage-2 "Mark Tight" now qualifies if the stock broke above the
        60-day Donchian base on ANY of the last ``BREAKOUT_WINDOW`` days
        (frozen base, excludes those days). ``N=1`` reproduces the original
        "today-only" rule.

  §4  — ``add_priority_score()`` runs inside ``process_results()`` so BOTH
        outputs (mark and mark_tight) carry ``Priority_Score``,
        ``Tradeable`` and ``Turnover_cr``, ranked within their own set.

The original ``Mark screener.py`` is left untouched for A/B comparison.
Run via the unified orchestrator ``run_screener.py``.
"""

import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd
import talib as ta

import config
from mark_screener.results import process_results
from config import (
    DATA_DIR, MARK_CHART_DIR, MARK_TIGHT_CHART_DIR,
    MARK_OUT_DIR, MARK_TIGHT_OUT_DIR,
    FUNDAMENTAL_FILE, AMIBROKER_WATCHLIST_DIR,
    BREAKOUT_WINDOW, CURRENT_DATE, CURRENT_TIMESTAMP,
)

# ---------------------------------------------------------------------------
# Timing + inputs (mirror the original module-level behaviour)
# ---------------------------------------------------------------------------
start_time = time.time()

# Create output folders up front (same as original lines 32-35)
config.ensure_dirs()

# List all the CSV files in the data directory
files = [f.name for f in DATA_DIR.glob('*.csv') if f.name != 'isin.csv']


# ===========================================================================
# Per-file processing
# ===========================================================================
def process_file(file):
    try:
        if file.endswith('.csv'):
            symbol = file.replace('.csv', '').upper()

            filepath = DATA_DIR / file
            # Optimization: keep only last 500 rows
            df = pd.read_csv(filepath, header=0)
            df = df.tail(500)

            df['symbol'] = symbol
            df.columns = df.columns.str.lower()
            df['date'] = pd.to_datetime(df['date'])

            # Moving averages
            df['EMA_50'] = ta.EMA(df['close'], timeperiod=50).round(2)
            df['EMA_150'] = ta.EMA(df['close'], timeperiod=150).round(2)
            df['EMA_200'] = ta.EMA(df['close'], timeperiod=200).round(2)

            # 52-week high / low
            df['52 week high'] = ta.MAX(df['high'], timeperiod=252)
            df['52 week low'] = ta.MIN(df['low'], timeperiod=252)

            # §3.2 — NEW frozen base column.
            # 60-day high of highs EXCLUDING the last BREAKOUT_WINDOW days.
            # At the last bar this equals max(high[-(60+N):-N]).
            df['Donchian_60_Base'] = ta.MAX(df['high'].shift(BREAKOUT_WINDOW), timeperiod=60)

            # Keep the original today-only column too (cheap, useful for
            # debugging / charting; the new logic uses Donchian_60_Base).
            df['Donchian_60_High'] = ta.MAX(df['high'].shift(1), timeperiod=60)

            # % down from 52w high
            df['Down from 52w high'] = (((df['52 week high'] - df['close'])
                                         / df['52 week high']) * 100).round(2)

            reversed_df = df.iloc[::-1]

            # 1. Mark Date (Stage 1: 20% buffer) — unchanged
            first_match_date_mark = pd.NaT
            for index, row in reversed_df.iterrows():
                if (pd.notna(row['EMA_50']) and pd.notna(row['EMA_150'])
                        and pd.notna(row['EMA_200'])
                        and pd.notna(row['52 week high'])
                        and pd.notna(row['52 week low'])):
                    ema_condition = row['close'] > row['EMA_50'] > row['EMA_150'] > row['EMA_200']
                    high_condition = row['close'] >= row['52 week high'] * 0.80
                    low_condition = row['close'] >= row['52 week low'] * 1.50

                    if ema_condition and high_condition and low_condition:
                        first_match_date_mark = row['date']
                    else:
                        break

            # §3.3 — Replace the Stage-2 reverse-walk with a direct window
            # computation. Qualifies if close broke above the frozen base on
            # ANY of the last BREAKOUT_WINDOW days; Mark_Tight_Date = the
            # most-recent such day.
            base_last = df['Donchian_60_Base'].iloc[-1]
            window = df.tail(BREAKOUT_WINDOW)
            bo_mask = window['close'] > base_last
            tight_breakout = bool(bo_mask.any())
            first_match_date_tight = (
                window.loc[bo_mask, 'date'].max() if tight_breakout else pd.NaT
            )

            # Stage-1 still gates entry — original behaviour preserved.
            if pd.isna(first_match_date_mark):
                return None

            last_row = df.iloc[[-1]].copy()
            last_row['Mark_Date'] = first_match_date_mark
            last_row['Mark_Tight_Date'] = first_match_date_tight
            last_row['Tight_Breakout'] = tight_breakout

            return last_row
    except Exception as e:
        logging.error(f"Error processing {file}: {e}")
        return None

# ===========================================================================
# Main
# ===========================================================================
def main():
    logging.info(f"Stage 1: Running Mark Screening (BREAKOUT_WINDOW={BREAKOUT_WINDOW})...")
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        results_list = list(executor.map(process_file, files))

    results_list = [r for r in results_list if r is not None]
    if not results_list:
        logging.info("No stocks matched the initial technical conditions.")
        return

    results_all = pd.concat(results_list, ignore_index=True)

    # Load fundamental data once
    try:
        query_df = pd.read_csv(FUNDAMENTAL_FILE)
    except FileNotFoundError:
        logging.error(f"Fundamental file not found: {FUNDAMENTAL_FILE}")
        return

    # Process Stage 1 (Mark)
    logging.info("Processing Stage 1 results...")
    df_mark = process_results(results_all, query_df, "mark", MARK_OUT_DIR,
                              MARK_CHART_DIR, "Mark_Date", DATA_DIR)

    if df_mark is not None and not df_mark.empty:
        # Save mark watchlist
        mark_watchlist_filename = f"{CURRENT_TIMESTAMP}_mark.txt"
        mark_watchlist_filepath = AMIBROKER_WATCHLIST_DIR / mark_watchlist_filename
        with open(mark_watchlist_filepath, 'w') as f:
            for symbol in df_mark['symbol']:
                f.write(f"{symbol.upper()}\n")
        logging.info(f"Watchlist created: {mark_watchlist_filepath}")

    if df_mark is not None:
        # Stage 2: Mark Tight Screening
        logging.info("Stage 2: Running Mark Tight Screening (5-day breakout window)...")
        # §3.4 — filter uses Tight_Breakout flag instead of today's Donchian.
        results_stage2 = results_all[
            (results_all['close'] >= results_all['52 week high'] * 0.85) &
            (results_all['Tight_Breakout']) &
            (~results_all['Mark_Tight_Date'].isna())
        ].copy()

        if not results_stage2.empty:
            logging.info(f"Found {len(results_stage2)} stocks matching Mark Tight criteria.")
            process_results(results_stage2, query_df, "mark_tight", MARK_TIGHT_OUT_DIR,
                            MARK_TIGHT_CHART_DIR, "Mark_Tight_Date", DATA_DIR)

            # Save mark_tight watchlist
            tight_watchlist_filename = f"{CURRENT_TIMESTAMP}_mark_tight.txt"
            tight_watchlist_filepath = AMIBROKER_WATCHLIST_DIR / tight_watchlist_filename
            with open(tight_watchlist_filepath, 'w') as f:
                for symbol in results_stage2['symbol']:
                    f.write(f"{symbol.upper()}\n")
            logging.info(f"Watchlist created: {tight_watchlist_filepath}")
        else:
            logging.info("No stocks matched the Mark Tight criteria.")

    end_time = time.time()
    logging.info(f"Total execution time: {round(end_time - start_time, 2)} seconds")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-7s  %(message)s', datefmt='%H:%M:%S')
    main()
