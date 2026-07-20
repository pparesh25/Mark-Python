"""Script to migrate existing NSE and BSE EOD CSV files into a DuckDB database."""

import sys
from pathlib import Path
import pandas as pd
import duckdb

# Add project root to sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import config

def main():
    db_file = config.DUCKDB_FILE
    print(f"Connecting to DuckDB database: {db_file}")
    
    # Connect (this will create the file if it doesn't exist)
    conn = duckdb.connect(str(db_file))
    
    # Create EOD table with primary key to prevent duplicate entries
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eod_data (
            Date DATE,
            Symbol VARCHAR,
            Exchange VARCHAR,
            ISIN VARCHAR,
            Open DOUBLE,
            High DOUBLE,
            Low DOUBLE,
            Close DOUBLE,
            Volume BIGINT,
            Series VARCHAR,
            Total_Trades INTEGER,
            Qty_Per_Trade DOUBLE,
            Dlv_Qty BIGINT,
            PRIMARY KEY (Date, Symbol, Exchange)
        );
    """)
    
    # Load ISIN mappings
    print("Loading ISIN mappings...")
    nse_isin_file = config.EOD_METADATA_DIR / "isin.csv"
    nse_isin_map = {}
    if nse_isin_file.is_file():
        df_nse_isin = pd.read_csv(nse_isin_file)
        # mapping symbol to ISIN
        nse_isin_map = dict(zip(df_nse_isin['SYMBOL'].str.upper(), df_nse_isin['ISIN']))
        
    bse_isin_file = config.EOD_METADATA_DIR / "bse_isin.csv"
    bse_isin_map = {}
    if bse_isin_file.is_file():
        df_bse_isin = pd.read_csv(bse_isin_file)
        bse_isin_map = dict(zip(df_bse_isin['SYMBOL'].str.upper(), df_bse_isin['ISIN']))

    # 1. Migrate NSE EOD CSV files
    nse_dir = config.DATA_DIR
    print(f"Scanning NSE EOD files in {nse_dir}...")
    nse_files = list(nse_dir.glob("*.csv"))
    nse_files = [f for f in nse_files if f.name != "isin.csv"]
    
    print(f"Found {len(nse_files)} NSE CSV files. Starting migration...")
    
    count = 0
    total_rows = 0
    
    for f in nse_files:
        symbol = f.stem.upper()
        # Handle SME suffix if present
        is_sme = symbol.endswith("_SME")
        clean_symbol = symbol[:-4] if is_sme else symbol
        
        isin = nse_isin_map.get(clean_symbol, "")
        
        try:
            df = pd.read_csv(f)
            df['Date'] = pd.to_datetime(df['Date']).dt.date
            df['Symbol'] = symbol
            df['Exchange'] = 'NSE'
            df['ISIN'] = isin
            
            # Map column names to match table schema
            df = df.rename(columns={
                'TOTAL_TRADES': 'Total_Trades',
                'QTY_PER_TRADE': 'Qty_Per_Trade',
                'DLV_QTY': 'Dlv_Qty'
            })
            
            # Select and reorder columns
            df = df[['Date', 'Symbol', 'Exchange', 'ISIN', 'Open', 'High', 'Low', 'Close', 'Volume', 'Series', 'Total_Trades', 'Qty_Per_Trade', 'Dlv_Qty']]
            
            # Insert into DuckDB
            conn.execute("INSERT OR IGNORE INTO eod_data SELECT * FROM df")
            
            count += 1
            total_rows += len(df)
            if count % 500 == 0:
                print(f"  Processed {count}/{len(nse_files)} NSE files...")
                
        except Exception as e:
            print(f"  Error migrating NSE symbol {symbol}: {e}")
            
    print(f"✓ Migrated {count} NSE files successfully ({total_rows} rows inserted).")

    # 2. Migrate BSE EOD CSV files
    bse_dir = config.BSE_DATA_DIR
    print(f"Scanning BSE EOD files in {bse_dir}...")
    bse_files = list(bse_dir.glob("*.csv"))
    
    print(f"Found {len(bse_files)} BSE CSV files. Starting migration...")
    
    count = 0
    total_bse_rows = 0
    
    for f in bse_files:
        symbol = f.stem.upper()
        is_sme = symbol.endswith("_SME")
        clean_symbol = symbol[:-4] if is_sme else symbol
        
        isin = bse_isin_map.get(clean_symbol, "")
        
        try:
            df = pd.read_csv(f)
            df['Date'] = pd.to_datetime(df['Date']).dt.date
            df['Symbol'] = symbol
            df['Exchange'] = 'BSE'
            df['ISIN'] = isin
            
            # Map column names to match table schema
            df = df.rename(columns={
                'TOTAL_TRADES': 'Total_Trades',
                'QTY_PER_TRADE': 'Qty_Per_Trade',
                'DLV_QTY': 'Dlv_Qty'
            })
            
            # Select and reorder columns
            df = df[['Date', 'Symbol', 'Exchange', 'ISIN', 'Open', 'High', 'Low', 'Close', 'Volume', 'Series', 'Total_Trades', 'Qty_Per_Trade', 'Dlv_Qty']]
            
            # Insert into DuckDB
            conn.execute("INSERT OR IGNORE INTO eod_data SELECT * FROM df")
            
            count += 1
            total_bse_rows += len(df)
            if count % 500 == 0:
                print(f"  Processed {count}/{len(bse_files)} BSE files...")
                
        except Exception as e:
            print(f"  Error migrating BSE symbol {symbol}: {e}")
            
    print(f"✓ Migrated {count} BSE files successfully ({total_bse_rows} rows inserted).")
    
    # Check total rows in database
    db_total = conn.execute("SELECT COUNT(*) FROM eod_data").fetchone()[0]
    print(f"Migration completed! Total rows in DuckDB: {db_total}")
    
    # Close connection
    conn.close()

if __name__ == "__main__":
    main()
