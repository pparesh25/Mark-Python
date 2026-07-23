"""Thomas Bulkowski's High and Tight Flag & Flag stock screeners using DuckDB database."""

import argparse
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd
import talib as ta
import duckdb

import config
from mark_screener.results import process_results
from config import (
    FUNDAMENTAL_FILE, AMIBROKER_WATCHLIST_DIR,
    CURRENT_TIMESTAMP,
)

start_time = time.time()

# Parse arguments
parser = argparse.ArgumentParser()
parser.add_argument('--exchange', type=str, default='NSE', choices=['NSE', 'BSE'])
parser.add_argument('--max-date', type=str, default=None)
args = parser.parse_args()
exchange = args.exchange.upper()
max_date = args.max_date

dirs = config.get_output_dirs(exchange)
FLAG_OUT_DIR = dirs['flag_out']
TIGHT_FLAG_OUT_DIR = dirs['tight_flag_out']
FLAG_CHART_DIR = dirs['flag_chart']
TIGHT_FLAG_CHART_DIR = dirs['tight_flag_chart']

config.ensure_dirs()


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
    
    flag_start_date = None
    flag_peak_date = None
    tight_start_date = None
    tight_peak_date = None
    breakout_date = None
    
    for j in range(n - 1, -1, -1):
        start = max(0, j - 45)
        for i in range(start, j):
            run_up = (highs[j] - lows[i]) / lows[i]
            if run_up >= 0.90:
                if not is_flag:
                    is_flag = True
                    flag_date = dates[j]
                    flag_start_date = dates[i]
                    flag_peak_date = dates[j]
                
                if (n - 1 - j <= 15) and (j < n - 1) and (closes[-1] > highs[j]):
                    if not is_tight_flag:
                        is_tight_flag = True
                        tight_flag_date = dates[j]
                        tight_start_date = dates[i]
                        tight_peak_date = dates[j]
                        
                        # Find the breakout date: the first day after the peak (j) where close > peak_high
                        for k in range(j + 1, n):
                            if closes[k] > highs[j]:
                                breakout_date = dates[k]
                                break
                
                if is_flag and is_tight_flag:
                    break
        if is_flag and is_tight_flag:
            break
            
    return is_flag, flag_date, flag_start_date, flag_peak_date, is_tight_flag, tight_flag_date, tight_start_date, tight_peak_date, breakout_date


def process_symbol_data(args_tuple):
    symbol, df_sym = args_tuple
    try:
        if len(df_sym) < 10:
            return None
            
        df = df_sym.copy()
        df.columns = df.columns.str.lower()
        df['date'] = pd.to_datetime(df['date'])
        
        # Calculate moving averages
        df['EMA_50'] = ta.EMA(df['close'], timeperiod=50).round(2)
        df['EMA_150'] = ta.EMA(df['close'], timeperiod=150).round(2)
        df['EMA_200'] = ta.EMA(df['close'], timeperiod=200).round(2)
        
        # 52-week high / low (fallback to max available days if history is slightly less than 252)
        period_52w = min(252, len(df))
        df['52 week high'] = ta.MAX(df['high'], timeperiod=period_52w)
        df['52 week low'] = ta.MIN(df['low'], timeperiod=period_52w)
        
        df_screening = df.tail(90).copy()
        (is_flag, flag_date, flag_start_date, flag_peak_date, 
         is_tight_flag, tight_flag_date, tight_start_date, 
         tight_peak_date, breakout_date) = check_flag_patterns(df_screening)
        
        if not is_flag and not is_tight_flag:
            return None
            
        last_row = df.iloc[[-1]].copy()
        last_row['flag_date'] = flag_date
        last_row['flag_start_date'] = flag_start_date
        last_row['flag_peak_date'] = flag_peak_date
        
        last_row['tight_flag_date'] = tight_flag_date
        last_row['tight_start_date'] = tight_start_date
        last_row['tight_peak_date'] = tight_peak_date
        last_row['breakout_date'] = breakout_date
        
        last_row['is_flag'] = is_flag
        last_row['is_tight_flag'] = is_tight_flag
        
        return last_row
    except Exception as e:
        logging.error(f"Error processing symbol {symbol}: {e}")
        return None


def main():
    # If run under the unified orchestrator, use raw message format to avoid double timestamps
    if os.environ.get('MARK_SCREENER_TIMESTAMP'):
        log_format = '%(message)s'
    else:
        log_format = "%(asctime)s [%(levelname)s] %(message)s"
        
    logging.basicConfig(level=logging.INFO, format=log_format)
    logging.info(f"Starting Flag Screener for {exchange} using DuckDB...")
    
    # Mode 1: High-Speed Rust Screening + Fundamental Merge & Charting
    if getattr(config, "USE_RUST_CALCULATION", False):
        import subprocess
        import json
        rust_bin = config.BASE_DIR / 'stock_analysis' / 'stock_analysis'
        out_temp = config.OUT_DIR / exchange
        out_temp.mkdir(parents=True, exist_ok=True)
        json_path = out_temp / f"{exchange}_rust_screened.json"

        # Check if single-pass Rust engine already executed for this exchange during Step 3
        if not json_path.exists():
            if rust_bin.exists():
                logging.info(f"▶ Mode 1: Executing High-Speed Rust Screener for {exchange}...")
                t0_rust = time.time()
                cmd = [
                    str(rust_bin),
                    "screen",
                    str(config.DUCKDB_FILE),
                    str(config.FUNDAMENTAL_FILE),
                    exchange,
                    str(out_temp)
                ]
                if max_date:
                    cmd.append(max_date)

                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    logging.error(f"Rust pipeline error: {res.stderr}\nFalling back to Python mode...")

        if json_path.exists():
            logging.info(f"✓ Processing Rust Flag screening results for {exchange}...")
            with open(json_path, 'r', encoding='utf-8') as f:
                rust_dataset = json.load(f)

            fundamental_df = pd.read_csv(FUNDAMENTAL_FILE)
            dirs = config.get_output_dirs(exchange)

            # Process Stage 1 Flag Results
            df_flag = pd.DataFrame(rust_dataset['flag_qualified'])
            if not df_flag.empty:
                df_flag['Exchange'] = exchange
                process_results(df_flag, fundamental_df, f"{exchange.lower()}_flag", dirs['flag_out'], dirs['flag_chart'], 'flag_date', config.DATA_DIR if exchange == 'NSE' else config.BSE_DATA_DIR, plot_days=200)

            # Process Stage 2 Tight Flag Results
            df_tight_flag = pd.DataFrame(rust_dataset['tight_flag_qualified'])
            if not df_tight_flag.empty:
                df_tight_flag['Exchange'] = exchange
                process_results(df_tight_flag, fundamental_df, f"{exchange.lower()}_tight_flag", dirs['tight_flag_out'], dirs['tight_flag_chart'], 'tight_flag_date', config.DATA_DIR if exchange == 'NSE' else config.BSE_DATA_DIR, plot_days=200)

            return

    # Mode 2 & Mode 3: Python Calculation Mode
    # 1. Load fundamental list to screen only listed equity symbols
    try:
        query_df = pd.read_csv(FUNDAMENTAL_FILE)
    except FileNotFoundError:
        logging.error(f"Fundamental file not found: {FUNDAMENTAL_FILE}")
        return
        
    valid_isins = set(query_df['ISIN Code'].dropna().str.strip().str.upper())
    
    # Connect to DuckDB and retrieve valid symbols list
    t_symbols_start = time.time()
    conn = duckdb.connect(str(config.DUCKDB_FILE), read_only=True)
    try:
        # Calculate filtering statistics for logging
        total_fundamental_isins = len(valid_isins)
        
        # NSE symbols present in eod_data matching fundamental ISIN
        nse_symbols_db = conn.execute("""
            SELECT DISTINCT Symbol, ISIN FROM eod_data 
            WHERE Exchange = 'NSE' AND ISIN IN (SELECT column0 FROM (SELECT UNNEST(?) as column0))
        """, (list(valid_isins),)).fetchdf()
        
        # BSE symbols present in eod_data matching fundamental ISIN (before NSE exclusion)
        bse_symbols_db = conn.execute("""
            SELECT DISTINCT Symbol, ISIN FROM eod_data 
            WHERE Exchange = 'BSE' AND ISIN IN (SELECT column0 FROM (SELECT UNNEST(?) as column0))
        """, (list(valid_isins),)).fetchdf()
        
        # BSE symbols matching fundamental ISIN but present in NSE (skipped)
        bse_skipped_db = conn.execute("""
            SELECT DISTINCT Symbol, ISIN FROM eod_data 
            WHERE Exchange = 'BSE' 
              AND ISIN IN (SELECT column0 FROM (SELECT UNNEST(?) as column0))
              AND ISIN IN (SELECT DISTINCT ISIN FROM eod_data WHERE Exchange = 'NSE')
        """, (list(valid_isins),)).fetchdf()
        
        # BSE-only symbols (matching fundamental ISIN and not present in NSE)
        bse_only_db = conn.execute("""
            SELECT DISTINCT Symbol, ISIN FROM eod_data 
            WHERE Exchange = 'BSE' 
              AND ISIN IN (SELECT column0 FROM (SELECT UNNEST(?) as column0))
              AND ISIN NOT IN (SELECT DISTINCT ISIN FROM eod_data WHERE Exchange = 'NSE')
        """, (list(valid_isins),)).fetchdf()
        
        # Print logs to verify the counts
        logging.info("=" * 64)
        logging.info("FILTERING LOGS & STOCK COUNT SUMMARY:")
        logging.info(f"  * Total unique ISINs in fundamental file: {total_fundamental_isins}")
        logging.info(f"  * NSE symbols matching fundamental ISIN (screened in NSE): {len(nse_symbols_db)}")
        logging.info(f"  * BSE symbols matching fundamental ISIN (total): {len(bse_symbols_db)}")
        logging.info(f"  * BSE symbols skipped because they exist in NSE (by ISIN): {len(bse_skipped_db)}")
        logging.info(f"  * BSE-only symbols to be screened (not in NSE): {len(bse_only_db)}")
        logging.info(f"  * Unique BSE-only ISINs verified: {bse_only_db['ISIN'].nunique()}")
        logging.info("=" * 64)
        
        if exchange == 'NSE':
            df_symbols = nse_symbols_db
        else:
            if config.BSE_ONLY_STOCKS:
                df_symbols = bse_only_db
            else:
                df_symbols = bse_symbols_db
    finally:
        conn.close()
    logging.info(f"✓ Retrieved valid symbols list in {time.time() - t_symbols_start:.2f}s")
        
    if df_symbols.empty:
        logging.info(f"No valid symbols found in DuckDB for {exchange} screening.")
        return
        
    symbols_list = list(df_symbols['Symbol'].str.upper())
    logging.info(f"Screening {len(symbols_list)} symbols for {exchange}...")
    
    # Load EOD data for these symbols from DuckDB
    t_eod_start = time.time()
    conn = duckdb.connect(str(config.DUCKDB_FILE), read_only=True)
    try:
        df_all = conn.execute("""
            SELECT Date, Symbol, Exchange, ISIN, Open, High, Low, Close, Volume, Series
            FROM eod_data 
            WHERE Exchange = ? AND Symbol IN (SELECT column0 FROM (SELECT UNNEST(?) as column0))
            ORDER BY Symbol, Date ASC
        """, (exchange, symbols_list)).fetchdf()
    finally:
        conn.close()
    logging.info(f"✓ Loaded EOD data for {len(symbols_list)} symbols in {time.time() - t_eod_start:.2f}s")
        
    if df_all.empty:
        logging.info("No EOD data found in database.")
        return
        
    if max_date:
        df_all = df_all[df_all['Date'].astype(str).str.split('T').str[0] <= max_date].copy()

    # Group by Symbol
    grouped = df_all.groupby('Symbol')
    tasks = [(symbol, group) for symbol, group in grouped]
    eod_data_dict = {symbol.upper(): group for symbol, group in grouped}
    
    logging.info(f"Running Technical Analysis across {len(tasks)} symbols...")
    t_ta_start = time.time()
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        results_list = list(executor.map(process_symbol_data, tasks))
    logging.info(f"✓ Completed Technical Analysis in {time.time() - t_ta_start:.2f}s")
        
    results_list = [r for r in results_list if r is not None]
    if not results_list:
        logging.info("No stocks matched Flag or Tight Flag criteria.")
        return
        
    results_all = pd.concat(results_list, ignore_index=True)
    
    # 1. Process Flag results
    results_flag = results_all[results_all['is_flag'] == True].copy()
    if not results_flag.empty:
        logging.info(f"Found {len(results_flag)} Flag stocks. Generating results and charts...")
        t_flag_start = time.time()
        process_results(
            results_flag, query_df, f"{exchange.lower()}_flag", FLAG_OUT_DIR,
            FLAG_CHART_DIR, "flag_date", config.DATA_DIR, plot_days=200, eod_data_dict=eod_data_dict
        )
        
        # Save watchlist to OpenTrader Pro database watchlist directory
        watchlist_dir = config.AMIBROKER_WATCHLIST_DIR
        if exchange == 'BSE':
            watchlist_dir = Path('/Users/paresh/.opentrader-pro/databases/BSE_EOD/watchlists')
        watchlist_dir.mkdir(parents=True, exist_ok=True)
        
        watchlist_filepath = watchlist_dir / f"{CURRENT_TIMESTAMP}_flag.txt"
        with open(watchlist_filepath, 'w') as f:
            for symbol in results_flag['symbol']:
                clean_symbol = symbol.replace('_SME', '').upper()
                f.write(f"{clean_symbol}\n")
        logging.info(f"Watchlist created: {watchlist_filepath}")
        logging.info(f"✓ Processed Flag results in {time.time() - t_flag_start:.2f}s")
        
    # 2. Process Tight Flag results
    results_tight = results_all[results_all['is_tight_flag'] == True].copy()
    if not results_tight.empty:
        logging.info(f"Found {len(results_tight)} Tight Flag stocks. Generating results and charts...")
        t_tight_start = time.time()
        process_results(
            results_tight, query_df, f"{exchange.lower()}_tight_flag", TIGHT_FLAG_OUT_DIR,
            TIGHT_FLAG_CHART_DIR, "tight_flag_date", config.DATA_DIR, plot_days=200, eod_data_dict=eod_data_dict
        )
        
        watchlist_dir = config.AMIBROKER_WATCHLIST_DIR
        if exchange == 'BSE':
            watchlist_dir = Path('/Users/paresh/.opentrader-pro/databases/BSE_EOD/watchlists')
        watchlist_dir.mkdir(parents=True, exist_ok=True)
        
        watchlist_filepath = watchlist_dir / f"{CURRENT_TIMESTAMP}_tight_flag.txt"
        with open(watchlist_filepath, 'w') as f:
            for symbol in results_tight['symbol']:
                clean_symbol = symbol.replace('_SME', '').upper()
                f.write(f"{clean_symbol}\n")
        logging.info(f"Watchlist created: {watchlist_filepath}")
        logging.info(f"✓ Processed Tight Flag results in {time.time() - t_tight_start:.2f}s")
        
    logging.info(f"Screener finished in {time.time() - start_time:.1f}s")


if __name__ == '__main__':
    main()
