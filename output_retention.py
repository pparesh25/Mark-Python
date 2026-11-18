"""Delete run outputs older than config.OUTPUT_RETAIN_DAYS so the data folder does not grow forever.

Only files whose name carries a run timestamp are touched:
    qualified_stocks/<NSE|BSE>/<folder>/YYYYMMDD_HHMMSS_*_qualified.csv
    logs/screening_log_YYYYMMDD_HHMMSS.txt and logs/run_fingerprint_YYYYMMDD_HHMMSS.json
Charts are replaced on every run and OpenTrader watchlists are left to the user.
A file that cannot be removed is logged and skipped; pruning never stops a run.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path

import config

QUALIFIED = re.compile(r"^(\d{8})_\d{6}_.*_qualified\.csv$")
RUN_LOG = re.compile(r"^(?:screening_log|run_fingerprint)_(\d{8})_\d{6}\.(?:txt|json)$")


def _stamp(name: str, pattern: re.Pattern) -> date | None:
    match = pattern.match(name)
    if not match:
        return None
    try:
        return date(int(match[1][:4]), int(match[1][4:6]), int(match[1][6:]))
    except ValueError:
        return None


def old_outputs(out_dir: Path, log_dir: Path, today: date, keep_days: int) -> list[Path]:
    """Run outputs dated more than `keep_days` days before `today`."""
    cutoff = today - timedelta(days=keep_days)
    found = []
    candidates = [(out_dir.glob("*/*/*.csv"), QUALIFIED), (log_dir.glob("*"), RUN_LOG)]
    for paths, pattern in candidates:
        for path in paths:
            stamp = _stamp(path.name, pattern)
            if path.is_file() and stamp is not None and stamp < cutoff:
                found.append(path)
    return sorted(found)


def prune(logger: logging.Logger, today: date, keep_days: int | None = None) -> int:
    """Remove old run outputs under the configured data root; returns how many files were removed."""
    keep_days = config.OUTPUT_RETAIN_DAYS if keep_days is None else keep_days
    if keep_days <= 0:
        return 0
    removed = 0
    for path in old_outputs(config.OUT_DIR, config.LOG_DIR, today, keep_days):
        try:
            path.relative_to(config.DATA_ROOT)       # never outside the data root
            path.unlink()
            removed += 1
        except (OSError, ValueError) as error:
            logger.warning(f"  Old output {path} not removed: {error}")
    if removed:
        logger.info(f"  Removed {removed} run outputs older than {keep_days} days (qualified CSVs, logs).")
    return removed
