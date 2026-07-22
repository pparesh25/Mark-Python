"""Mark Screener with 5-day breakout window + Priority Score using DuckDB database."""

import argparse
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from datetime import date

import pandas as pd
import talib as ta
import duckdb

import config
from mark_screener.results import process_results
from config import (
    FUNDAMENTAL_FILE, AMIBROKER_WATCHLIST_DIR,
    BREAKOUT_WINDOW, CURRENT_TIMESTAMP,
)

start_time = time.time()

# Parse arguments
parser = argparse.ArgumentParser()
parser.add_argument('--exchange', type=str, default='NSE', choices=['NSE', 'BSE'])
args = parser.parse_args()
exchange = args.exchange.upper()

dirs = config.get_output_dirs(exchange)
MARK_OUT_DIR = dirs['mark_out']
MARK_TIGHT_OUT_DIR = dirs['mark_tight_out']
MARK_CHART_DIR = dirs['mark_chart']
MARK_TIGHT_CHART_DIR = dirs['mark_tight_chart']

config.ensure_dirs()




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
        
        # Donchian base
        df['Donchian_60_Base'] = ta.MAX(df['high'].shift(BREAKOUT_WINDOW), timeperiod=60)
        df['Donchian_60_High'] = ta.MAX(df['high'].shift(1), timeperiod=60)
        
        df['Down from 52w high'] = (((df['52 week high'] - df['close'])
                                     / df['52 week high']) * 100).round(2)
                                     
        reversed_df = df.iloc[::-1]
        
        # Stage 1: Mark Date (20% buffer)
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
                    
        # Stage 2: Tight breakout computation
        base_last = df['Donchian_60_Base'].iloc[-1]
        window = df.tail(BREAKOUT_WINDOW)
        bo_mask = window['close'] > base_last
        tight_breakout = bool(bo_mask.any())
        first_match_date_tight = (
            window.loc[bo_mask, 'date'].max() if tight_breakout else pd.NaT
        )
        
        if pd.isna(first_match_date_mark):
            return None
            
        last_row = df.iloc[[-1]].copy()
        last_row['mark_date'] = first_match_date_mark
        last_row['mark_tight_date'] = first_match_date_tight
        last_row['tight_breakout'] = tight_breakout
        
        return last_row
    except Exception as e:
        logging.error(f"Error processing symbol {symbol}: {e}")
        return None


def main():
    # If run under the unified orchestrator, use raw message format to avoid double timestamps
    if os.environ.get('MARK_SCREENER_TIMESTAMP'):
        log_format = '%(message)s'
    else:
        log_format = '%(asctime)s  %(levelname)-7s  %(message)s'
        
    logging.basicConfig(level=logging.INFO, format=log_format, datefmt='%H:%M:%S')
    logging.info(f"Starting Mark Screener for {exchange} using DuckDB...")
    
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
        logging.info("No stocks matched the initial technical conditions.")
        return
        
    results_all = pd.concat(results_list, ignore_index=True)
    
    # Process Stage 1 (Mark)
    logging.info("Processing Stage 1 results...")
    t_stage1_start = time.time()
    df_mark = process_results(results_all, query_df, f"{exchange.lower()}_mark", MARK_OUT_DIR,
                              MARK_CHART_DIR, "mark_date", config.DATA_DIR, eod_data_dict=eod_data_dict)
    logging.info(f"✓ Processed Stage 1 results in {time.time() - t_stage1_start:.2f}s")
                              
    watchlist_dir = config.AMIBROKER_WATCHLIST_DIR
    if exchange == 'BSE':
        watchlist_dir = Path('/Users/paresh/.opentrader-pro/databases/BSE_EOD/watchlists')
    watchlist_dir.mkdir(parents=True, exist_ok=True)
    
    if df_mark is not None and not df_mark.empty:
        # Save mark watchlist
        mark_watchlist_filename = f"{CURRENT_TIMESTAMP}_{exchange.lower()}_mark.txt"
        mark_watchlist_filepath = watchlist_dir / mark_watchlist_filename
        with open(mark_watchlist_filepath, 'w') as f:
            for symbol in df_mark['symbol']:
                clean_symbol = symbol.replace('_SME', '').upper()
                f.write(f"{clean_symbol}\n")
        logging.info(f"Watchlist created: {mark_watchlist_filepath}")
        
    if df_mark is not None:
        # Stage 2: Mark Tight Screening
        logging.info("Stage 2: Running Mark Tight Screening (5-day breakout window)...")
        t_stage2_start = time.time()
        results_stage2 = results_all[
            (results_all['close'] >= results_all['52 week high'] * 0.85) &
            (results_all['tight_breakout']) &
            (~results_all['mark_tight_date'].isna())
        ].copy()
        
        if not results_stage2.empty:
            logging.info(f"Found {len(results_stage2)} stocks matching Mark Tight criteria.")
            process_results(results_stage2, query_df, f"{exchange.lower()}_mark_tight", MARK_TIGHT_OUT_DIR,
                            MARK_TIGHT_CHART_DIR, "mark_tight_date", config.DATA_DIR, eod_data_dict=eod_data_dict)
                            
            # Save mark_tight watchlist
            tight_watchlist_filename = f"{CURRENT_TIMESTAMP}_{exchange.lower()}_mark_tight.txt"
            tight_watchlist_filepath = watchlist_dir / tight_watchlist_filename
            with open(tight_watchlist_filepath, 'w') as f:
                for symbol in results_stage2['symbol']:
                    clean_symbol = symbol.replace('_SME', '').upper()
                    f.write(f"{clean_symbol}\n")
            logging.info(f"Watchlist created: {tight_watchlist_filepath}")
        else:
            logging.info("No stocks matched the Mark Tight criteria.")
        logging.info(f"✓ Processed Stage 2 results in {time.time() - t_stage2_start:.2f}s")
            
    end_time = time.time()
    logging.info(f"Total execution time: {round(end_time - start_time, 2)} seconds")


if __name__ == '__main__':
    main()
