"""Mark Tight screener using the compact DuckDB price store."""

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

start_time = time.time()

# Parse arguments
parser = argparse.ArgumentParser()
parser.add_argument('--exchange', type=str, default='NSE', choices=['NSE', 'BSE'])
parser.add_argument('--max-date', type=str, default=None)
sme_group = parser.add_mutually_exclusive_group()
sme_group.add_argument('--include-sme', action='store_true', help="Include SME stocks in screening")
sme_group.add_argument('--only-sme', action='store_true', help="Screen ONLY SME stocks")
parser.add_argument('--data-root', type=Path, help="Override the Mark data root")
parser.add_argument('--watchlist-root', type=Path, help="Override the .opentrader-pro root")
parser.add_argument(
    '--watchlists',
    action=argparse.BooleanOptionalAction,
    default=None,
    help="Write OpenTrader Pro watchlists",
)
parser.add_argument(
    '--strip-sme-watchlist-suffix',
    action='store_true',
    default=None,
    help="Remove the internal _SME suffix from watchlist symbols",
)
args = parser.parse_args()
if args.data_root:
    config.configure_data_root(args.data_root)
watchlists_enabled = (
    config.WATCHLISTS_ENABLED if args.watchlists is None else args.watchlists
)
config.configure_watchlists(
    watchlists_enabled,
    args.watchlist_root,
    strip_sme_suffix=args.strip_sme_watchlist_suffix,
)
exchange = args.exchange.upper()
max_date = args.max_date
include_sme = args.include_sme
only_sme = args.only_sme

dirs = config.get_output_dirs(exchange)
MARK_TIGHT_OUT_DIR = dirs['mark_tight_out']
MARK_TIGHT_CHART_DIR = dirs['mark_tight_chart']

config.ensure_dirs()




def process_symbol_data(args_tuple):
    symbol, df_sym = args_tuple
    try:
        if len(df_sym) < config.MIN_HISTORY_DAYS:
            return None
            
        df = df_sym.copy()
        df.columns = df.columns.str.lower()
        df['date'] = pd.to_datetime(df['date'])
        
        # Calculate moving averages
        df['EMA_50'] = ta.EMA(df['close'], timeperiod=50).round(2)
        df['EMA_150'] = ta.EMA(df['close'], timeperiod=150).round(2)
        df['EMA_200'] = ta.EMA(df['close'], timeperiod=200).round(2)
        
        # A full 52-week history is mandatory for every qualified strategy.
        period_52w = config.MIN_HISTORY_DAYS
        df['52 week high'] = ta.MAX(df['high'], timeperiod=period_52w)
        df['52 week low'] = ta.MIN(df['low'], timeperiod=period_52w)
        df['Donchian_60_High'] = (
            df['high'].shift(1).rolling(window=60, min_periods=60).max()
        )
        
        df['Down from 52w high'] = (((df['52 week high'] - df['close'])
                                     / df['52 week high']) * 100).round(2)

        # Find the start of the active Mark setup.  This is only an internal
        # setup date; the screener exports Mark Tight candidates exclusively.
        mark_index = None
        for position in range(len(df) - 1, -1, -1):
            row = df.iloc[position]
            if (pd.notna(row['EMA_50']) and pd.notna(row['EMA_150'])
                    and pd.notna(row['EMA_200'])
                    and pd.notna(row['52 week high'])
                    and pd.notna(row['52 week low'])):
                ema_condition = row['close'] > row['EMA_50'] > row['EMA_150'] > row['EMA_200']
                high_condition = row['close'] >= row['52 week high'] * 0.80
                low_condition = row['close'] >= row['52 week low'] * 1.50
                
                if ema_condition and high_condition and low_condition:
                    mark_index = position
                else:
                    break
            else:
                break
                    
        if mark_index is None:
            return None

        # The true Mark Tight date is the first rolling 60-session Donchian
        # breakout inside the current uninterrupted Mark trend.
        breakout_index = next(
            (
                position
                for position in range(mark_index, len(df))
                if pd.notna(df['Donchian_60_High'].iloc[position])
                and df['close'].iloc[position]
                > df['Donchian_60_High'].iloc[position]
            ),
            None,
        )
        if breakout_index is None:
            return None
        breakout_level = df['Donchian_60_High'].iloc[breakout_index]
        if df['close'].iloc[-1] <= breakout_level:
            return None

        last_row = df.iloc[[-1]].copy()
        last_row['mark_tight_date'] = df['date'].iloc[breakout_index]
        
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
    
    # Mode 1: High-Speed Rust Screening + Fundamental Merge & Charting
    if getattr(config, "USE_RUST_CALCULATION", False):
        import subprocess
        import json
        rust_bin = config.RUST_BINARY
        if rust_bin.exists():
            logging.info(f"▶ Mode 1: Executing High-Speed Rust Screener for {exchange}...")
            t0_rust = time.time()
            out_temp = config.OUT_DIR / exchange
            out_temp.mkdir(parents=True, exist_ok=True)
            
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
            cmd.append("--mark-tight-only")
            if include_sme:
                cmd.append("--include-sme")
            if only_sme:
                cmd.append("--only-sme")

            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                logging.info(f"✓ Rust screening completed in {time.time() - t0_rust:.2f}s. Processing fundamental merge and charts...")
                json_path = out_temp / f"{exchange}_rust_screened.json"
                with open(json_path, 'r', encoding='utf-8') as f:
                    rust_dataset = json.load(f)

                fundamental_df = pd.read_csv(config.FUNDAMENTAL_FILE)
                dirs = config.get_output_dirs(exchange)

                # Mark is an internal setup only.  Export the final fixed-Donchian
                # breakout candidates as Mark Tight.
                df_mark_tight = pd.DataFrame(rust_dataset['mark_tight_qualified'])
                if not df_mark_tight.empty:
                    df_mark_tight['Exchange'] = exchange
                    df_mark_tight = df_mark_tight.rename(columns={'ema_50': 'EMA_50', 'ema_150': 'EMA_150', 'ema_200': 'EMA_200', 'high_52w': '52 week high', 'low_52w': '52 week low', 'mark_tight_date': 'Mark_Tight_Date', 'down_52w_high': 'Down from 52w high'})
                    process_results(df_mark_tight, fundamental_df, f"{exchange.lower()}_mark_tight", dirs['mark_tight_out'], dirs['mark_tight_chart'], 'Mark_Tight_Date', config.DATA_DIR if exchange == 'NSE' else config.BSE_DATA_DIR, plot_days=500)

                return
            else:
                logging.error(f"Rust pipeline error: {res.stderr}\nFalling back to Python mode...")

    # Mode 2 & Mode 3: Python Calculation Mode
    # 1. Load fundamental list to screen only listed equity symbols
    try:
        query_df = pd.read_csv(config.FUNDAMENTAL_FILE)
    except FileNotFoundError:
        logging.error(f"Fundamental file not found: {config.FUNDAMENTAL_FILE}")
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

    symbol_is_sme = df_symbols['Symbol'].astype(str).str.strip().str.upper().str.endswith('_SME')
    if only_sme:
        df_symbols = df_symbols[symbol_is_sme].copy()
    elif not include_sme:
        df_symbols = df_symbols[~symbol_is_sme].copy()
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
        logging.info("No stocks matched the initial technical conditions.")
        return
        
    results_all = pd.concat(results_list, ignore_index=True)
    
    # Every returned row already contains a confirmed, held Mark Tight breakout.
    logging.info("Exporting rolling-Donchian Mark Tight matches...")
    t_stage2_start = time.time()
    results_stage2 = results_all[
        ~results_all['mark_tight_date'].isna()
    ].copy()

    if not results_stage2.empty:
        logging.info(f"Found {len(results_stage2)} stocks matching Mark Tight criteria.")
        process_results(results_stage2, query_df, f"{exchange.lower()}_mark_tight", MARK_TIGHT_OUT_DIR,
                        MARK_TIGHT_CHART_DIR, "mark_tight_date", config.DATA_DIR, eod_data_dict=eod_data_dict)
    else:
        logging.info("No stocks matched the Mark Tight criteria.")
    logging.info(f"✓ Processed Mark Tight results in {time.time() - t_stage2_start:.2f}s")
            
    end_time = time.time()
    logging.info(f"Total execution time: {round(end_time - start_time, 2)} seconds")


if __name__ == '__main__':
    main()
