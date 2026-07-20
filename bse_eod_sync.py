"""Standalone BSE EOD sync runner."""

import os
import sys
import time
import logging
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).resolve().parent))

import config
from bse_eod_downloader import eod_downloader


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ]
    )
    return logging.getLogger("bse_sync")


def main():
    logger = setup_logging()
    logger.info("Initializing BSE EOD Sync stand-alone...")
    
    # Ensure all directories (including new BSE EOD folders) exist
    config.ensure_dirs()
    
    t0 = time.time()
    try:
        success = eod_downloader.sync_database(
            data_dir=config.BSE_DATA_DIR,
            logger=logger,
            metadata_dir=config.EOD_METADATA_DIR,
            daily_bhavcopy_dir=config.BSE_DAILY_BHAVCOPY_DIR,
        )
        elapsed = time.time() - t0
        if success:
            logger.info(f"✓ Stand-alone BSE EOD Sync completed successfully in {elapsed:.1f}s")
        else:
            logger.error(f"✗ Stand-alone BSE EOD Sync failed after {elapsed:.1f}s")
            sys.exit(1)
    except Exception as e:
        logger.exception(f"✗ Stand-alone BSE EOD Sync crashed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
