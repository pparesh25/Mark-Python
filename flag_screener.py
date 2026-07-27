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
import polars as pl

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
TIGHT_FLAG_OUT_DIR = dirs['tight_flag_out']
TIGHT_FLAG_CHART_DIR = dirs['tight_flag_chart']

config.ensure_dirs()


def check_flag_patterns(df):
    if isinstance(df, pl.DataFrame):
        opens = df['open'].to_numpy()
        highs = df['high'].to_numpy()
        lows = df['low'].to_numpy()
        closes = df['close'].to_numpy()
        dates = df['date'].cast(pl.String).to_numpy()
    else:
        opens = df['open'].values
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
    
    for i in range(n - 1):
        max_j = min(n - 1, i + 45)
        if max_j <= i:
            continue
            
        # Find peak high in range [i + 1, max_j]
        j_peak = i + 1
        max_h = highs[j_peak]
        for candidate_j in range(i + 1, max_j + 1):
            if highs[candidate_j] > max_h:
                max_h = highs[candidate_j]
                j_peak = candidate_j
                
        if lows[i] <= 0.0:
            continue

        run_up = (highs[j_peak] - lows[i]) / lows[i]
        if run_up >= 0.90:
            if not is_flag:
                is_flag = True
                flag_date = dates[j_peak]
                flag_start_date = dates[i]
                flag_peak_date = dates[j_peak]
            
            # Stage 2: reject short poles, extreme gaps and circuit traps,
            # then require a sustained breakout within 15 sessions.
            if j_peak - i > 3:
                has_extreme_gap = any(
                    closes[p_idx - 1] > 0
                    and abs(opens[p_idx] - closes[p_idx - 1]) / closes[p_idx - 1] > 0.50
                    for p_idx in range(i + 1, j_peak + 1)
                )
                circuit_count = sum(
                    abs(highs[p_idx] - lows[p_idx]) < 1e-4
                    for p_idx in range(i, j_peak + 1)
                )
                circuit_pct = circuit_count / (j_peak - i + 1) * 100

                if not has_extreme_gap and circuit_pct <= 40.0:
                    breakout_k = None
                    max_breakout_k = min(n, j_peak + 16)
                    for k in range(j_peak + 1, max_breakout_k):
                        if closes[k] > highs[j_peak]:
                            breakout_k = k
                            break

                    if breakout_k is not None and closes[-1] > highs[j_peak]:
                        if not is_tight_flag:
                            is_tight_flag = True
                            tight_flag_date = dates[j_peak]
                            tight_start_date = dates[i]
                            tight_peak_date = dates[j_peak]
                            breakout_date = dates[breakout_k]
                            
            if is_flag and is_tight_flag:
                break
            
    return is_flag, flag_date, flag_start_date, flag_peak_date, is_tight_flag, tight_flag_date, tight_start_date, tight_peak_date, breakout_date


def process_symbol_data(args_tuple):
    symbol, df = args_tuple
    if df is None or len(df) < config.MIN_HISTORY_DAYS:
        return None

    try:
        if isinstance(df, pl.DataFrame):
            closes = df['close'].to_numpy()
            highs = df['high'].to_numpy()
            lows = df['low'].to_numpy()
            dates = df['date'].cast(pl.String).to_numpy()
            last_close = closes[-1]
            last_low = lows[-1]
        else:
            df.columns = df.columns.str.lower()
            closes = df['close'].values
            highs = df['high'].values
            lows = df['low'].values
            dates = df['date'].values
            last_close = closes[-1]
            last_low = lows[-1]

        # Tight Flag indicators use full, non-shortened windows.
        n = len(closes)
        ema_200 = ta.EMA(closes, timeperiod=config.EMA_PERIOD)
        high_52w = ta.MAX(highs, timeperiod=config.MIN_HISTORY_DAYS)

        last_ema200 = ema_200[-1]
        last_52w_high = high_52w[-1]

        df_screening = df.tail(config.FLAG_LOOKBACK_DAYS)
        if not isinstance(df_screening, pl.DataFrame):
            df_screening = df_screening.copy()
        (is_flag, flag_date, flag_start_date, flag_peak_date,
         is_tight_flag, tight_flag_date, tight_start_date,
         tight_peak_date, breakout_date) = check_flag_patterns(df_screening)
        
        # The 90%-in-45-days Flag condition is an internal candidate gate.
        # Only complete Tight Flag matches continue to result processing.
        if not is_tight_flag:
            return None

        dist_52w = ((last_52w_high - last_close) / last_52w_high * 100
                     if last_52w_high > 0 else float('inf'))
        if (last_close < 5.0
                or np.isnan(last_ema200)
                or last_close < last_ema200
                or dist_52w > 45.0):
            return None

        res_dict = {
            'symbol': symbol,
            'date': dates[-1],
            'open': df['open'][-1] if isinstance(df, pl.DataFrame) else df['open'].iloc[-1],
            'high': highs[-1],
            'low': lows[-1],
            'close': last_close,
            'volume': df['volume'][-1] if isinstance(df, pl.DataFrame) else df['volume'].iloc[-1],
            'EMA_200': round(last_ema200, 2) if not np.isnan(last_ema200) else 0.0,
            '52 week high': round(last_52w_high, 2) if not np.isnan(last_52w_high) else 0.0,
            'flag_date': flag_date,
            'flag_start_date': flag_start_date,
            'flag_peak_date': flag_peak_date,
            'tight_flag_date': tight_flag_date,
            'tight_start_date': tight_start_date,
            'tight_peak_date': tight_peak_date,
            'breakout_date': breakout_date,
            'is_flag': is_flag,
            'is_tight_flag': is_tight_flag
        }
        return pd.DataFrame([res_dict])
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
        rust_bin = config.RUST_BINARY
        out_temp = config.OUT_DIR / exchange
        out_temp.mkdir(parents=True, exist_ok=True)
        json_path = out_temp / f"{exchange}_rust_screened.json"
        rust_succeeded = False

        # Always run with this invocation's filters.  Reusing a previous JSON
        # could silently apply the wrong SME selection.
        if rust_bin.exists():
            logging.info(f"▶ Mode 1: Executing High-Speed Rust Screener for {exchange}...")
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
            cmd.extend(["--tight-flag-only", "--flag-lookback", str(config.FLAG_LOOKBACK_DAYS)])
            if include_sme:
                cmd.append("--include-sme")
            if only_sme:
                cmd.append("--only-sme")

            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                logging.error(f"Rust pipeline error: {res.stderr}\nFalling back to Python mode...")
            else:
                rust_succeeded = True

        if rust_succeeded and json_path.exists():
            logging.info(f"✓ Processing Rust Flag screening results for {exchange}...")
            with open(json_path, 'r', encoding='utf-8') as f:
                rust_dataset = json.load(f)

            fundamental_df = pd.read_csv(config.FUNDAMENTAL_FILE)
            dirs = config.get_output_dirs(exchange)

            # Flag is an internal 90%-in-45-days candidate gate. Only Tight
            # Flag candidates receive ranked output, charts, and a watchlist.
            df_tight_flag = pd.DataFrame(rust_dataset['tight_flag_qualified'])
            if not df_tight_flag.empty:
                df_tight_flag['Exchange'] = exchange
                process_results(df_tight_flag, fundamental_df, f"{exchange.lower()}_tight_flag", dirs['tight_flag_out'], dirs['tight_flag_chart'], 'tight_flag_date', config.DATA_DIR if exchange == 'NSE' else config.BSE_DATA_DIR, plot_days=200)

            return

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
    logging.info(f"✓ Retrieved valid symbols list in {time.time() - t_symbols_start:.2f}s")
        
    if df_symbols.empty:
        logging.info(f"No valid symbols found in DuckDB for {exchange} screening.")
        return
        
    symbols_list = list(df_symbols['Symbol'].str.upper())
    logging.info(f"Screening {len(symbols_list)} symbols for {exchange}...")
    
    # Load EOD data for these symbols from DuckDB via Arrow
    t_eod_start = time.time()
    conn = duckdb.connect(str(config.DUCKDB_FILE), read_only=True)
    try:
        arrow_table = conn.execute("""
            SELECT Date, Symbol, Exchange, ISIN, Open, High, Low, Close, Volume, Series
            FROM eod_data 
            WHERE Exchange = ? AND Symbol IN (SELECT column0 FROM (SELECT UNNEST(?) as column0))
            ORDER BY Symbol, Date ASC
        """, (exchange, symbols_list)).to_arrow_table()
    finally:
        conn.close()

    df_all = pl.from_arrow(arrow_table).rename({
        'Date': 'date', 'Symbol': 'symbol', 'Open': 'open',
        'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'
    })
    df_all = df_all.with_columns(pl.col('symbol').str.strip_chars())
    symbol_is_sme = pl.col('symbol').str.to_uppercase().str.ends_with('_SME')
    if only_sme:
        df_all = df_all.filter(symbol_is_sme)
    elif not include_sme:
        df_all = df_all.filter(~symbol_is_sme)
    logging.info(f"✓ Loaded EOD data for {len(symbols_list)} symbols in {time.time() - t_eod_start:.2f}s")
        
    if df_all.is_empty():
        logging.info("No EOD data found in database.")
        return
        
    if max_date:
        df_all = df_all.filter(pl.col('date').cast(pl.String).str.slice(0, 10) <= max_date)

    # Group by Symbol with Polars partition_by
    tasks = [(df_sym['symbol'][0], df_sym) for df_sym in df_all.partition_by('symbol')]
    eod_data_dict = {sym: df_sym.to_pandas() for sym, df_sym in tasks}
    
    logging.info(f"Running Technical Analysis across {len(tasks)} symbols...")
    t_ta_start = time.time()
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        results_list = list(executor.map(process_symbol_data, tasks))
    logging.info(f"✓ Completed Technical Analysis in {time.time() - t_ta_start:.2f}s")
        
    results_list = [r for r in results_list if r is not None]
    if not results_list:
        logging.info("No stocks matched the Tight Flag criteria.")
        return
        
    results_all = pd.concat(results_list, ignore_index=True)
    
    results_tight = results_all[results_all['is_tight_flag'] == True].copy()
    if not results_tight.empty:
        logging.info(f"Found {len(results_tight)} Tight Flag stocks. Generating results and charts...")
        t_tight_start = time.time()
        process_results(
            results_tight, query_df, f"{exchange.lower()}_tight_flag", TIGHT_FLAG_OUT_DIR,
            TIGHT_FLAG_CHART_DIR, "tight_flag_date", config.DATA_DIR, plot_days=200, eod_data_dict=eod_data_dict
        )
        logging.info(f"✓ Processed Tight Flag results in {time.time() - t_tight_start:.2f}s")
        
    logging.info(f"Screener finished in {time.time() - start_time:.1f}s")


if __name__ == '__main__':
    main()
