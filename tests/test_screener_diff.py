"""Standalone Differential Testing Framework: Python Screeners vs Rust Screener Calculator.

Compares output row-by-row on identical DuckDB dataset to verify 100% logic accuracy.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pandas as pd

# Add parent directory to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config

def run_python_screeners(exchange, max_date=None, logger=print):
    """Run existing Python screeners and return qualification lists."""
    logger(f"▶ Running Python screeners for {exchange} (max_date: {max_date or 'Latest'})...")
    t0 = time.time()
    
    # Run Mark Screener
    mark_script = BASE_DIR / 'Mark_screener.py'
    cmd_mark = [sys.executable, str(mark_script), '--exchange', exchange]
    if max_date:
        cmd_mark.extend(['--max-date', max_date])
    res_mark = subprocess.run(cmd_mark, capture_output=True, text=True)
    if res_mark.returncode != 0:
        logger(f"✗ Python Mark screener failed: {res_mark.stderr}")
        return None
        
    # Run Flag Screener
    flag_script = BASE_DIR / 'flag_screener.py'
    cmd_flag = [sys.executable, str(flag_script), '--exchange', exchange]
    if max_date:
        cmd_flag.extend(['--max-date', max_date])
    res_flag = subprocess.run(cmd_flag, capture_output=True, text=True)
    if res_flag.returncode != 0:
        logger(f"✗ Python Flag screener failed: {res_flag.stderr}")
        return None

    elapsed = time.time() - t0
    logger(f"✓ Python screeners completed in {elapsed:.2f}s")
    
    # Read generated CSVs
    dirs = config.get_output_dirs(exchange)
    def get_latest_csv(directory):
        csv_files = list(directory.glob("*_qualified.csv"))
        if not csv_files:
            return pd.DataFrame()
        latest = max(csv_files, key=lambda p: p.stat().st_mtime)
        return pd.read_csv(latest)

    py_data = {
        'mark_tight': get_latest_csv(dirs['mark_tight_out']),
        'tight_flag': get_latest_csv(dirs['tight_flag_out']),
        'time': elapsed,
    }
    return py_data


def run_rust_screener(exchange, max_date=None, logger=print):
    """Run new Rust screener calculator and return parsed dataset."""
    logger(f"▶ Running Rust Screener Calculator for {exchange} (max_date: {max_date or 'Latest'})...")
    rust_bin = BASE_DIR / 'stock_analysis' / 'stock_analysis'
    if not rust_bin.exists():
        rust_bin = BASE_DIR / 'stock_analysis' / 'target' / 'release' / 'stock_analysis'
        
    if not rust_bin.exists():
        logger(f"Building Rust binary...")
        subprocess.run(['cargo', 'build', '--release'], cwd=BASE_DIR / 'stock_analysis', check=True)
        rust_bin = BASE_DIR / 'stock_analysis' / 'target' / 'release' / 'stock_analysis'

    temp_out_dir = BASE_DIR / 'scratch' / 'rust_test_out'
    temp_out_dir.mkdir(parents=True, exist_ok=True)
    
    t0 = time.time()
    cmd_rust = [
        str(rust_bin),
        "screen",
        str(config.DUCKDB_FILE),
        str(config.FUNDAMENTAL_FILE),
        exchange,
        str(temp_out_dir)
    ]
    if max_date:
        cmd_rust.append(max_date)

    res = subprocess.run(cmd_rust, capture_output=True, text=True)
    
    elapsed = time.time() - t0
    if res.returncode != 0:
        logger(f"✗ Rust screener failed: {res.stderr}\n{res.stdout}")
        return None
        
    logger(f"✓ Rust screener completed in {elapsed:.2f}s")
    
    json_path = temp_out_dir / f"{exchange}_rust_screened.json"
    with open(json_path, 'r', encoding='utf-8') as f:
        rust_data = json.load(f)
        
    rust_data['time'] = elapsed
    return rust_data


fn_clean_date = lambda d: str(d).split()[0].split('T')[0].strip() if pd.notna(d) and str(d).strip().lower() not in ('nan', 'nat', 'none', '') else ''

def compare_results(py_data, rust_data, exchange):
    """Compare Python vs Rust results and calculate accuracy."""
    print("\n" + "="*70)
    print(f"📊 DIFFERENTIAL VERIFICATION REPORT ({exchange})")
    print("="*70)
    
    categories = [
        ('Mark Tight Screener', py_data['mark_tight'], rust_data['mark_tight_qualified'], 'mark_tight_date', 'mark_tight_date'),
        ('Tight Flag Screener', py_data['tight_flag'], rust_data['tight_flag_qualified'], 'tight_flag_date', 'tight_flag_date'),
    ]
    
    overall_match = True
    
    for cat_name, df_py, list_rust, py_date_col, rust_date_col in categories:
        py_symbols = set(df_py['symbol'].str.strip().str.upper()) if not df_py.empty else set()
        rust_symbols = set(r['symbol'].strip().upper() for r in list_rust)
        
        common = py_symbols.intersection(rust_symbols)
        only_py = py_symbols - rust_symbols
        only_rust = rust_symbols - py_symbols
        
        match_pct = (len(common) / len(py_symbols) * 100.0) if py_symbols else 100.0
        
        print(f"\n🔹 {cat_name}:")
        print(f"   Python Count : {len(py_symbols)}")
        print(f"   Rust Count   : {len(rust_symbols)}")
        print(f"   Common Count : {len(common)}")
        print(f"   Match Rate   : {match_pct:.2f}%")
        
        if only_py:
            print(f"   ⚠️ Only in Python ({len(only_py)}): {sorted(list(only_py))[:10]}")
            overall_match = False
        if only_rust:
            print(f"   ⚠️ Only in Rust ({len(only_rust)}): {sorted(list(only_rust))[:10]}")
            overall_match = False
            
        # Verify date fields on common symbols
        if not df_py.empty and py_date_col in df_py.columns and common:
            py_date_map = dict(zip(df_py['symbol'].str.strip().str.upper(), df_py[py_date_col].apply(fn_clean_date)))
            rust_date_map = {r['symbol'].strip().upper(): fn_clean_date(r.get(rust_date_col)) for r in list_rust}
            
            date_mismatches = 0
            for sym in common:
                if py_date_map.get(sym) != rust_date_map.get(sym):
                    date_mismatches += 1
                    
            if date_mismatches > 0:
                print(f"   ⚠️ Date field mismatches on common symbols: {date_mismatches}")
                overall_match = False
            else:
                print(f"   ✓ All date fields match 100% on common symbols!")

    print("\n" + "-"*70)
    py_time = py_data['time']
    rust_time = rust_data['time']
    speedup = py_time / rust_time if rust_time > 0 else 0
    print(f"⏱️ PERFORMANCE TIMING:")
    print(f"   Python Total Time : {py_time:.2f}s")
    print(f"   Rust Total Time   : {rust_time:.2f}s")
    print(f"   Speedup Factor    : {speedup:.1f}x Faster 🚀")
    print("-"*70)

    if overall_match:
        print("✅ VERIFICATION SUCCESS: Rust and Python outputs match 100%!")
    else:
        print("❌ VERIFICATION WARNING: Discrepancies detected between Python and Rust outputs.")
        
    return overall_match


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exchange', type=str, default='NSE', choices=['NSE', 'BSE'])
    parser.add_argument('--max-date', type=str, default=None)
    args = parser.parse_args()
    
    exchange = args.exchange.upper()
    max_date = args.max_date
    py_data = run_python_screeners(exchange, max_date=max_date)
    if py_data is None:
        sys.exit(1)
        
    rust_data = run_rust_screener(exchange, max_date=max_date)
    if rust_data is None:
        sys.exit(1)
        
    compare_results(py_data, rust_data, exchange)


if __name__ == '__main__':
    main()
