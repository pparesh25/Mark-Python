"""After every sync: keep two years, fetch the action lists around today (and the whole retained
history when it was last fetched more than 30 days ago), decide the factors.

    from eod_maintenance import maintain, verify_store
    maintain(logger)          # run_screener.py calls it after the NSE and BSE syncs
    verify_store(logger)      # run_screener.py --verify: the full verify and repair only

The prices the screener reads are the raw prices times the factors decided here
(price_adjustments.py), so this step is what keeps them right: a split NSE
lists late, a demerger, a renamed BSE scrip -- each is caught on the next run
however the sync went. On a version 3 store only the symbols whose decisions
can have changed are decided again, and once a week everything is compared
with a full rebuild and repaired from raw on a difference (adjustment_ledger.py);
a version 2 store is migrated to version 3 on its first run here (a copy is kept
beside it). A hand decision goes into
Database/metadata/corporate_action_overrides.csv (Exchange, Symbol, Date,
Factor, Note); the recent gaps nothing explains are written to
Database/corporate_action_review.csv for a look.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import duckdb

import adjustment_ledger
import config
import price_adjustments
from eod_store import apply_retention, create_schema, schema_version


def overrides_file():
    return config.EOD_METADATA_DIR / "corporate_action_overrides.csv"


def review_file():
    return config.DATABASE_DIR / "corporate_action_review.csv"


def open_nse(logger: logging.Logger):
    """An NSE session for the action lists, or None when the nse package or NSE is unavailable."""
    try:
        from nse import NSE
        folder = config.DATA_DIR / "temp"
        folder.mkdir(parents=True, exist_ok=True)
        return NSE(folder, server=True)
    except Exception as error:
        logger.warning(f"NSE session unavailable for the action list ({error}); using the stored list.")
        return None


def maintain(logger: logging.Logger, nse_factory=open_nse, today: date | None = None) -> dict:
    """Retention, the version 3 migration when due, action lists and factors on the live store; raises on an
    old (version 1) store.

    `today` is the market's date (config.MARKET_TIMEZONE), not the machine's: a run
    in another time zone must ask for the same list window and review period.
    """
    today = today or datetime.now(config.MARKET_TIMEZONE).date()
    path = config.DUCKDB_FILE
    conn = duckdb.connect(str(path))
    try:
        version = schema_version(conn)
        if version not in (2, 3):
            raise RuntimeError("the price store is version 1; run rebuild_eod_database.py first")
        create_schema(conn)
        dropped = apply_retention(conn, config.EOD_RETAIN_YEARS)
        conn.execute("CHECKPOINT")
    finally:
        conn.close()
    if version == 2:
        # A failed migration is logged loudly; the store stays version 2 and is decided as before.
        try:
            adjustment_ledger.migrate(path, overrides_file(), review_file(), today, logger)
        except Exception as error:                   # the backup copy or the file itself failed
            logger.error(f"PRICE STORE MIGRATION TO VERSION 3 STOPPED ({error}); the store is version "
                         f"{_version_of(path)} now.")
    conn = duckdb.connect(str(path))
    nse = None
    try:
        nse = nse_factory(logger)
        summary = price_adjustments.refresh(conn, nse, logger, overrides_file(), review_file(), today)
        conn.execute("CHECKPOINT")
        return {"raw_rows_dropped": dropped, "schema_version": schema_version(conn), **summary}
    finally:
        conn.close()
        if nse is not None:
            try:
                nse.exit()
            except Exception as error:               # closing the session is best effort
                logger.debug(f"NSE session did not close cleanly: {error}")


def _version_of(path) -> str:
    """The store's schema version as it is on disk, for a message after an error ("unknown" if unreadable)."""
    try:
        conn = duckdb.connect(str(path), read_only=True)
        try:
            return str(schema_version(conn))
        finally:
            conn.close()
    except Exception as error:  # noqa: BLE001 - only for a message
        return f"unknown ({error})"


def verify_store(logger: logging.Logger, today: date | None = None) -> dict:
    """The full verify of the live store: compare everything with a full rebuild, repair on a difference.

    Returns adjustment_ledger.verify's result ({"status": "ok" | "repaired", ...}); raises when the store is
    missing or not version 2 or 3.
    """
    today = today or datetime.now(config.MARKET_TIMEZONE).date()
    if not config.DUCKDB_FILE.exists():
        raise RuntimeError(f"no price store at {config.DUCKDB_FILE}")
    conn = duckdb.connect(str(config.DUCKDB_FILE))
    try:
        result = adjustment_ledger.verify(conn, overrides_file(), review_file(), today, logger, repair=True)
        conn.execute("CHECKPOINT")
        return {"schema_version": schema_version(conn), **result}
    finally:
        conn.close()
