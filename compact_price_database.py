"""Copy the price store into a fresh file to give back the space deleted rows still hold.

    python compact_price_database.py            # a preview beside the store
    python compact_price_database.py --apply    # install it; the old file is kept as a backup

Every sync already keeps only two years of raw prices (config.EOD_RETAIN_YEARS),
but DuckDB does not shrink a file when rows are deleted. This copies every
table of the version 2 store (securities, raw_prices within the window,
exchange_actions, price_adjustments, adjustment_periods) into a new file,
recreates the views, checks the row counts and only then swaps it in.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

import config
from eod_store import create_schema, schema_version

TABLES = ("securities", "raw_prices", "exchange_actions", "price_adjustments", "adjustment_periods")


def build_compact_database(source: Path, target: Path, years: float) -> None:
    """Create a compact copy of ``source`` at ``target`` without modifying ``source``."""
    if target.exists():
        target.unlink()
    conn = duckdb.connect(str(target))
    try:
        conn.execute(f"ATTACH '{str(source).replace(chr(39), chr(39) * 2)}' AS source_db (READ_ONLY)")
        version = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_catalog = 'source_db' AND table_name = 'raw_prices'"
        ).fetchone()[0]
        if not version:
            raise RuntimeError("the store is version 1; run rebuild_eod_database.py instead")
        create_schema(conn)
        last = conn.execute("SELECT max(Date) FROM source_db.raw_prices").fetchone()[0]
        cutoff = (pd.Timestamp(last) - pd.Timedelta(days=round(365.25 * years))).date() if last else None
        for table in TABLES:
            where = " WHERE Date >= ?" if table == "raw_prices" and cutoff else ""
            conn.execute(f"INSERT INTO {table} SELECT * FROM source_db.{table}{where}",
                         [cutoff] if where else [])
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
        if schema_version(conn) != 2 or conn.execute("SELECT count(*) FROM eod_data").fetchone()[0] == 0:
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
