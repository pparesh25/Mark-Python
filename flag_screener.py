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
    FLAG_CHART_DIR, TIGHT_FLAG_CHART_DIR,
    FLAG_OUT_DIR, TIGHT_FLAG_OUT_DIR,
    FUNDAMENTAL_FILE, AMIBROKER_WATCHLIST_DIR,
    CURRENT_TIMESTAMP,
)

start_time = time.time()
config.ensure_dirs()

# Parse arguments
parser = argparse.ArgumentParser()
parser.add_argument('--exchange', type=str, default='NSE', choices=['NSE', 'BSE'])
args = parser.parse_args()
exchange = args.exchange.upper()


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
    
    for j in range(n - 1, -1, -1):
        start = max(0, j - 45)
        for i in range(start, j):
            run_up = (highs[j] - lows[i]) / lows[i]
            if run_up >= 0.90:
                if not is_flag:
                    is_flag = True
                    flag_date = dates[j]
                
                if (n - 1 - j <= 15) and (j < n - 1) and (closes[-1] > highs[j]):
                    if not is_tight_flag:
                        is_tight_flag = True
                        tight_flag_date = dates[j]
                
                if is_flag and is_tight_flag:
                    break
        if is_flag and is_tight_flag:
            break
            
    return is_flag, flag_date, is_tight_flag, tight_flag_date


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
        
        df['52 week high'] = ta.MAX(df['high'], timeperiod=252)
        df['52 week low'] = ta.MIN(df['low'], timeperiod=252)
        
        df_screening = df.tail(90).copy()
        is_flag, flag_date, is_tight_flag, tight_flag_date = check_flag_patterns(df_screening)
        
        if not is_flag and not is_tight_flag:
            return None
            
        last_row = df.iloc[[-1]].copy()
        last_row['flag_date'] = flag_date
        last_row['tight_flag_date'] = tight_flag_date
        last_row['is_flag'] = is_flag
        last_row['is_tight_flag'] = is_tight_flag
        
        return last_row
    except Exception as e:
        logging.error(f"Error processing symbol {symbol}: {e}")
        return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.info(f"Starting Flag Screener for {exchange} using DuckDB...")
    
    # 1. Load fundamental list to screen only listed equity symbols
    try:
        query_df = pd.read_csv(FUNDAMENTAL_FILE)
    except FileNotFoundError:
        logging.error(f"Fundamental file not found: {FUNDAMENTAL_FILE}")
        return
        
    valid_isins = set(query_df['ISIN Code'].dropna().str.strip().str.upper())
    
    # Connect to DuckDB and retrieve valid symbols list
    conn = duckdb.connect(str(config.DUCKDB_FILE), read_only=True)
    try:
        if exchange == 'NSE':
            # For NSE, select symbols present in fundamental file (NSE Code)
            nse_codes = set(query_df['NSE Code'].dropna().str.strip().str.upper())
            df_symbols = conn.execute("""
                SELECT DISTINCT Symbol, ISIN FROM eod_data 
                WHERE Exchange = 'NSE' AND Symbol IN (SELECT column0 FROM (SELECT UNNEST(?) as column0))
            """, (list(nse_codes),)).fetchdf()
        else:
            # For BSE, select symbols present in fundamental file (ISIN Code)
            # If BSE_ONLY_STOCKS is True, exclude symbols present in NSE
            if config.BSE_ONLY_STOCKS:
                df_symbols = conn.execute("""
                    SELECT DISTINCT Symbol, ISIN FROM eod_data 
                    WHERE Exchange = 'BSE' 
                      AND ISIN IN (SELECT column0 FROM (SELECT UNNEST(?) as column0))
                      AND ISIN NOT IN (SELECT DISTINCT ISIN FROM eod_data WHERE Exchange = 'NSE')
                """, (list(valid_isins),)).fetchdf()
            else:
                df_symbols = conn.execute("""
                    SELECT DISTINCT Symbol, ISIN FROM eod_data 
                    WHERE Exchange = 'BSE' 
                      AND ISIN IN (SELECT column0 FROM (SELECT UNNEST(?) as column0))
                """, (list(valid_isins),)).fetchdf()
    finally:
        conn.close()
        
    if df_symbols.empty:
        logging.info(f"No valid symbols found in DuckDB for {exchange} screening.")
        return
        
    symbols_list = list(df_symbols['Symbol'].str.upper())
    logging.info(f"Screening {len(symbols_list)} symbols for {exchange}...")
    
    # Load EOD data for these symbols from DuckDB
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
        
    if df_all.empty:
        logging.info("No EOD data found in database.")
        return
        
    # Group by Symbol
    grouped = df_all.groupby('Symbol')
    tasks = [(symbol, group) for symbol, group in grouped]
    
    logging.info(f"Running Technical Analysis across {len(tasks)} symbols...")
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        results_list = list(executor.map(process_symbol_data, tasks))
        
    results_list = [r for r in results_list if r is not None]
    if not results_list:
        logging.info("No stocks matched Flag or Tight Flag criteria.")
        return
        
    results_all = pd.concat(results_list, ignore_index=True)
    
    # 1. Process Flag results
    results_flag = results_all[results_all['is_flag'] == True].copy()
    if not results_flag.empty:
        logging.info(f"Found {len(results_flag)} Flag stocks. Generating results and charts...")
        process_results(
            results_flag, query_df, f"{exchange.lower()}_flag", FLAG_OUT_DIR,
            FLAG_CHART_DIR, "flag_date", config.DATA_DIR, plot_days=200
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
        
    # 2. Process Tight Flag results
    results_tight = results_all[results_all['is_tight_flag'] == True].copy()
    if not results_tight.empty:
        logging.info(f"Found {len(results_tight)} Tight Flag stocks. Generating results and charts...")
        process_results(
            results_tight, query_df, f"{exchange.lower()}_tight_flag", TIGHT_FLAG_OUT_DIR,
            TIGHT_FLAG_CHART_DIR, "tight_flag_date", config.DATA_DIR, plot_days=200
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
        
    logging.info(f"Screener finished in {time.time() - start_time:.1f}s")


if __name__ == '__main__':
    main()
