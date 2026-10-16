"""Copy the price store into a fresh file to give back the space deleted rows still hold.

    python compact_price_database.py            # a preview beside the store
    python compact_price_database.py --apply    # install it; the old file is kept as a backup

Every sync already keeps only two years of raw prices (config.EOD_RETAIN_YEARS),
but DuckDB does not shrink a file when rows are deleted. This copies every
table of the store (securities, raw_prices and, in version 3, eod_prices
within the window, exchange_actions, price_adjustments, adjustment_periods,
action_list_history, and in version 3 adjustment_ledger, gap_history,
symbol_state and maintenance_state) into a new file, recreates the views,
checks the row counts and only then swaps it in. Rows left out of a version 3
copy are taken out of symbol_state as retention does, so the next maintenance
checks what the shorter history changes.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

import config
from eod_store import create_schema, forget_rows_before, schema_version

TABLES = ("securities", "raw_prices", "exchange_actions", "price_adjustments", "adjustment_periods")
STORED_TABLES = ("eod_prices", "adjustment_ledger", "gap_history", "symbol_state", "maintenance_state")
OPTIONAL_TABLES = ("action_list_history",)          # absent from stores made before it existed
WINDOWED_TABLES = ("raw_prices", "eod_prices")      # cut to the retained window


def build_compact_database(source: Path, target: Path, years: float) -> None:
    """Create a compact copy of ``source`` at ``target`` without modifying ``source``."""
    if target.exists():
        target.unlink()
    conn = duckdb.connect(str(target))
    try:
        conn.execute(f"ATTACH '{str(source).replace(chr(39), chr(39) * 2)}' AS source_db (READ_ONLY)")
        present = dict(conn.execute(
            "SELECT table_name, table_type FROM information_schema.tables WHERE table_catalog = 'source_db'"
        ).fetchall())
        if "raw_prices" not in present:
            raise RuntimeError("the store is version 1; run rebuild_eod_database.py instead")
        stored = present.get("eod_prices") == "BASE TABLE" and "adjustment_ledger" in present
        create_schema(conn, 3 if stored else 2)
        last = conn.execute("SELECT max(Date) FROM source_db.raw_prices").fetchone()[0]
        cutoff = (pd.Timestamp(last) - pd.Timedelta(days=round(365.25 * years))).date() if last else None
        tables = TABLES + (STORED_TABLES if stored else ()) + tuple(t for t in OPTIONAL_TABLES if t in present)
        for table in tables:
            where = " WHERE Date >= ?" if table in WINDOWED_TABLES and cutoff else ""
            conn.execute(f"INSERT INTO {table} BY NAME SELECT * FROM source_db.{table}{where}",
                         [cutoff] if where else [])
        if stored and cutoff:
            forget_rows_before(conn, cutoff, "source_db.raw_prices")
        conn.execute("DETACH source_db")
        conn.execute("CHECKPOINT")
    finally:
        conn.close()


def install_compact_database(source: Path, years: float) -> Path:
    """Build, check, back up, and swap in the compact store."""
    build = source.with_name(f"{source.stem}.compact-build{source.suffix}")
    build_compact_database(source, build, years)
    conn = duckdb.connect(str(build), read_only=True)
    try:
        if schema_version(conn) not in (2, 3) or conn.execute("SELECT count(*) FROM eod_data").fetchone()[0] == 0:
            raise RuntimeError("Compact database validation failed")
    finally:
        conn.close()
    backup = source.with_name(f"{source.stem}.pre_compaction_{datetime.now():%Y%m%d_%H%M%S}{source.suffix}")
    source.replace(backup)
    build.replace(source)
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, default=config.DUCKDB_FILE)
    parser.add_argument("--years", type=float, default=config.EOD_RETAIN_YEARS)
    parser.add_argument("--apply", action="store_true", help="install after keeping a backup")
    args = parser.parse_args()
    if args.apply:
        backup = install_compact_database(args.database, args.years)
        print(f"Installed compact database: {args.database}\nBackup kept at: {backup}")
    else:
        target = args.database.with_name(f"{args.database.stem}.compact-preview{args.database.suffix}")
        build_compact_database(args.database, target, args.years)
        print(f"Compact preview created: {target}")


if __name__ == "__main__":
    main()
