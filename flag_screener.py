"""Thomas Bulkowski's High and Tight Flag & Flag stock screeners.

Runs parallelized technical screening across the stock database.
- Flag: 90%+ rise in <=45 trading sessions within the last 90 candles.
- Tight Flag: Same flagpole peaking within the last 15 candles, and latest Close is above the flagpole High.
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
    DATA_DIR, FLAG_CHART_DIR, TIGHT_FLAG_CHART_DIR,
    FLAG_OUT_DIR, TIGHT_FLAG_OUT_DIR,
    FUNDAMENTAL_FILE, AMIBROKER_WATCHLIST_DIR,
    CURRENT_TIMESTAMP,
)

start_time = time.time()
config.ensure_dirs()

# List all the CSV files in the data directory
files = [f.name for f in DATA_DIR.glob('*.csv') if f.name != 'isin.csv']


def check_flag_patterns(df):
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    dates = df['date'].values
    n = len(df)
    
    is_flag = False
    is_tight_flag = False
    flag_date = None
    tight_flag_date = None
    
    # We scan all possible peak index j from n-1 down to 0
    # To optimize and prioritize the most recent flagpole, we scan backwards
    for j in range(n - 1, -1, -1):
        start = max(0, j - 45)
        for i in range(start, j):
            run_up = (highs[j] - lows[i]) / lows[i]
            if run_up >= 0.90:
                # Valid flagpole found!
                if not is_flag:
                    is_flag = True
                    flag_date = dates[j]
                
                # Check Tight Flag condition:
                # 1. Peak j occurred within the last 15 candles: (n-1) - j <= 15
                # 2. Peak j is not the latest candle: j < n-1 (at least 1 day consolidation)
                # 3. Latest Close is above the breakout point: closes[-1] > highs[j]
                if (n - 1 - j <= 15) and (j < n - 1) and (closes[-1] > highs[j]):
                    if not is_tight_flag:
                        is_tight_flag = True
                        tight_flag_date = dates[j]
                
                # Break inner loop if both found
                if is_flag and is_tight_flag:
                    break
        if is_flag and is_tight_flag:
            break
            
    return is_flag, flag_date, is_tight_flag, tight_flag_date


def process_file(file):
    try:
        if file.endswith('.csv'):
            symbol = file.replace('.csv', '').upper()
            filepath = DATA_DIR / file
            
            # Read full file to calculate moving averages properly, then use last 90 for screening
            df = pd.read_csv(filepath, header=0)
            if len(df) < 10:
                return None
                
            df['symbol'] = symbol
            df.columns = df.columns.str.lower()
            df['date'] = pd.to_datetime(df['date'])
            
            # Indicators needed for plotting and scoring
            df['EMA_50'] = ta.EMA(df['close'], timeperiod=50).round(2)
            df['EMA_150'] = ta.EMA(df['close'], timeperiod=150).round(2)
            df['EMA_200'] = ta.EMA(df['close'], timeperiod=200).round(2)
            
            df['52 week high'] = ta.MAX(df['high'], timeperiod=252)
            df['52 week low'] = ta.MIN(df['low'], timeperiod=252)
            
            # Truncate to the last 90 rows for screening
            df_screening = df.tail(90).copy()
            is_flag, flag_date, is_tight_flag, tight_flag_date = check_flag_patterns(df_screening)
            
            if not is_flag and not is_tight_flag:
                return None
                
            # Return last row with flag markers
            last_row = df.iloc[[-1]].copy()
            last_row['Flag_Date'] = flag_date
            last_row['Tight_Flag_Date'] = tight_flag_date
            last_row['Is_Flag'] = is_flag
            last_row['Is_Tight_Flag'] = is_tight_flag
            
            return last_row
    except Exception as e:
        logging.error(f"Error processing {file} in flag screener: {e}")
        return None


def main():
    logging.info(f"Running Flag Screening across {len(files)} files...")
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        results_list = list(executor.map(process_file, files))
        
    results_list = [r for r in results_list if r is not None]
    if not results_list:
        logging.info("No stocks matched Flag or Tight Flag criteria.")
        return
        
    results_all = pd.concat(results_list, ignore_index=True)
    
    try:
        query_df = pd.read_csv(FUNDAMENTAL_FILE)
    except FileNotFoundError:
        logging.error(f"Fundamental file not found: {FUNDAMENTAL_FILE}")
        return
        
    # 1. Process Flag results
    results_flag = results_all[results_all['Is_Flag'] == True].copy()
    if not results_flag.empty:
        logging.info(f"Found {len(results_flag)} Flag stocks. Generating results and charts...")
        process_results(
            results_flag, query_df, "flag", FLAG_OUT_DIR,
            FLAG_CHART_DIR, "Flag_Date", DATA_DIR, plot_days=200
        )
        
        # Save watchlist
        watchlist_filepath = AMIBROKER_WATCHLIST_DIR / f"{CURRENT_TIMESTAMP}_flag.txt"
        with open(watchlist_filepath, 'w') as f:
            for symbol in results_flag['symbol']:
                f.write(f"{symbol.upper()}\n")
        logging.info(f"Watchlist created: {watchlist_filepath}")
        
    # 2. Process Tight Flag results
    results_tight = results_all[results_all['Is_Tight_Flag'] == True].copy()
    if not results_tight.empty:
        logging.info(f"Found {len(results_tight)} Tight Flag stocks. Generating results and charts...")
        process_results(
            results_tight, query_df, "tight_flag", TIGHT_FLAG_OUT_DIR,
            TIGHT_FLAG_CHART_DIR, "Tight_Flag_Date", DATA_DIR, plot_days=200
        )
        
        # Save watchlist
        watchlist_filepath = AMIBROKER_WATCHLIST_DIR / f"{CURRENT_TIMESTAMP}_tight_flag.txt"
        with open(watchlist_filepath, 'w') as f:
            for symbol in results_tight['symbol']:
                f.write(f"{symbol.upper()}\n")
        logging.info(f"Watchlist created: {watchlist_filepath}")
        
    end_time = time.time()
    logging.info(f"Flag screener finished in {round(end_time - start_time, 2)} seconds")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-7s  %(message)s', datefmt='%H:%M:%S')
    main()
