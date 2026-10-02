"""After every sync: keep two years, fetch the action lists around today, decide the factors.

    from eod_maintenance import maintain
    maintain(logger)          # run_screener.py calls it after the NSE and BSE syncs

The prices the screener reads are the raw prices times the factors decided here
(price_adjustments.py), so this step is what keeps them right: a split NSE
lists late, a demerger, a renamed BSE scrip -- each is caught on the next run
however the sync went. A hand decision goes into
Database/metadata/corporate_action_overrides.csv (Exchange, Symbol, Date,
Factor, Note); the recent gaps nothing explains are written to
Database/corporate_action_review.csv for a look.
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb

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
    """Retention, action lists and factors on the live store; raises on an old (version 1) store."""
    conn = duckdb.connect(str(config.DUCKDB_FILE))
    nse = None
    try:
        if schema_version(conn) != 2:
            raise RuntimeError("the price store is version 1; run rebuild_eod_database.py first")
        create_schema(conn)
        dropped = apply_retention(conn, config.EOD_RETAIN_YEARS)
        nse = nse_factory(logger)
        summary = price_adjustments.refresh(conn, nse, logger, overrides_file(), review_file(), today)
        conn.execute("CHECKPOINT")
        return {"raw_rows_dropped": dropped, **summary}
    finally:
        conn.close()
        if nse is not None:
            try:
                nse.exit()
            except Exception:
                pass
