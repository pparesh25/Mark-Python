"""Build and atomically install an explicitly capped DuckDB price store."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import duckdb

import config
from eod_store import create_compact_schema


def build_compact_database(source: Path, target: Path, retain_candles: int) -> None:
    """Create a compact database from ``source`` without modifying it."""
    if target.exists():
        target.unlink()

    # The window operation needs a temporary materialisation.  Do that in a
    # separate database, then copy only the final two tables into ``target``.
    # Dropping a large DuckDB table does not necessarily release its blocks,
    # so building the final file from a clean database is what keeps it small.
    stage = target.with_name(f"{target.stem}.stage{target.suffix}")
    if stage.exists():
        stage.unlink()

    source_sql = str(source).replace("'", "''")
    stage_sql = str(stage).replace("'", "''")
    conn = duckdb.connect(str(stage))
    try:
        conn.execute(f"ATTACH '{source_sql}' AS source_db (READ_ONLY)")
        # DuckDB 1.1 can reorder projected columns when a window relation is
        # filtered in the same CTAS.  Persist the rank first, then filter it
        # in a separate statement so Symbol/Exchange can never be swapped.
        conn.execute(
            f"""
            CREATE TABLE ranked_prices AS
            SELECT Date, Symbol, Exchange, ISIN, Open, High, Low, Close, Volume, Series,
                   ROW_NUMBER() OVER (
                       PARTITION BY Exchange, Symbol ORDER BY Date DESC
                   ) AS retention_rank
            FROM source_db.eod_data
            """
        )
        conn.execute(
            f"""
            CREATE TABLE retained_prices AS
            SELECT Date, Symbol, Exchange, ISIN, Open, High, Low, Close, Volume, Series
            FROM ranked_prices
            WHERE retention_rank <= {retain_candles}
            """
        )
        conn.execute(
            """
            CREATE TABLE securities AS
            SELECT Exchange, Symbol,
                   arg_max(ISIN, Date) AS ISIN,
                   arg_max(Series, Date) AS Series
            FROM retained_prices
            GROUP BY Exchange, Symbol
            """
        )
        conn.execute(
            """
            CREATE TABLE eod_prices AS
            SELECT Date, Symbol, Exchange, Open, High, Low, Close, Volume
            FROM retained_prices
            """
        )
        conn.execute("DROP TABLE ranked_prices")
        conn.execute("DROP TABLE retained_prices")
        conn.execute("CHECKPOINT")
    finally:
        conn.close()

    conn = duckdb.connect(str(target))
    try:
        conn.execute(f"ATTACH '{stage_sql}' AS stage_db (READ_ONLY)")
        conn.execute("CREATE TABLE securities AS SELECT * FROM stage_db.securities")
        conn.execute("CREATE TABLE eod_prices AS SELECT * FROM stage_db.eod_prices")
        create_compact_schema(conn)
        conn.execute("CHECKPOINT")
    finally:
        conn.close()
        # This is an explicitly named, self-created staging database.
        if stage.exists():
            stage.unlink()


def install_compact_database(source: Path, retain_candles: int) -> Path:
    """Build, validate, back up, and atomically install the compact database."""
    build = source.with_name(f"{source.stem}.compact-build{source.suffix}")
    build_compact_database(source, build, retain_candles)

    conn = duckdb.connect(str(build), read_only=True)
    try:
        price_rows = conn.execute("SELECT COUNT(*) FROM eod_prices").fetchone()[0]
        view_rows = conn.execute("SELECT COUNT(*) FROM eod_data").fetchone()[0]
        if price_rows == 0 or price_rows != view_rows:
            raise RuntimeError("Compact database validation failed")
    finally:
        conn.close()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = source.with_name(f"{source.stem}.pre_compaction_{timestamp}{source.suffix}")
    source.replace(backup)
    build.replace(source)
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=config.DUCKDB_FILE)
    parser.add_argument(
        "--retain-candles",
        type=int,
        default=config.EOD_COMPACT_CANDLES,
    )
    parser.add_argument("--apply", action="store_true", help="Install after creating a backup")
    args = parser.parse_args()
    if args.retain_candles < 252:
        parser.error("--retain-candles must be at least 252")

    if args.apply:
        backup = install_compact_database(args.database, args.retain_candles)
        print(f"Installed compact database: {args.database}")
        print(f"Backup retained at: {backup}")
    else:
        target = args.database.with_name(f"{args.database.stem}.compact-preview{args.database.suffix}")
        build_compact_database(args.database, target, args.retain_candles)
        print(f"Compact preview created: {target}")


if __name__ == "__main__":
    main()
