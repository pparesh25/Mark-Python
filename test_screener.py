from pathlib import Path
import duckdb
import config

def test_get_output_dirs():
    # Test NSE output directory structures
    nse_dirs = config.get_output_dirs("NSE")
    assert set(nse_dirs) == {
        "mark_tight_out", "tight_flag_out",
        "mark_tight_chart", "tight_flag_chart",
    }
    assert nse_dirs['tight_flag_chart'] == config.CHART_DIR / "NSE" / "Tight Flag"
    
    # Test BSE output directory structures
    bse_dirs = config.get_output_dirs("BSE")
    assert bse_dirs['mark_tight_out'] == config.OUT_DIR / "BSE" / "mark_tight"
    assert bse_dirs['tight_flag_chart'] == config.CHART_DIR / "BSE" / "Tight Flag"

def test_duckdb_file_exists():
    # Verify DuckDB database file exists and contains table
    db_file = Path(config.DUCKDB_FILE)
    assert db_file.exists(), f"DuckDB file {config.DUCKDB_FILE} does not exist"
    
    # Test query
    conn = duckdb.connect(str(db_file), read_only=True)
    try:
        # Compact schema provides eod_data as a compatibility view.
        conn.execute("SELECT 1 FROM eod_data LIMIT 1")
        
        # Check that we can fetch some rows
        row_count = conn.execute("SELECT count(*) FROM eod_data").fetchone()[0]
        assert row_count > 0, "eod_data table is empty"
    finally:
        conn.close()

def test_exchange_case_insensitivity():
    # Check that Exchange and exchange keys both resolve to the correct exchange name
    row_upper = {"symbol": "SBIN", "Exchange": "NSE"}
    row_lower = {"symbol": "SBIN", "exchange": "BSE"}
    
    val_upper = row_upper.get("Exchange", row_upper.get("exchange", "NSE"))
    val_lower = row_lower.get("Exchange", row_lower.get("exchange", "NSE"))
    
    assert val_upper == "NSE"
    assert val_lower == "BSE"

if __name__ == "__main__":
    print("Running tests...")
    test_get_output_dirs()
    print("✓ test_get_output_dirs passed")
    test_duckdb_file_exists()
    print("✓ test_duckdb_file_exists passed")
    test_exchange_case_insensitivity()
    print("✓ test_exchange_case_insensitivity passed")
    print("All tests passed successfully!")
