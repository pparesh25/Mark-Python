"""
Screener.in CSV Downloader using Selenium
Downloads all-company.csv automatically after login

Usage:
    python3 screener_all_fundamental.py              # Normal mode
    python3 screener_all_fundamental.py --headless   # Headless mode (faster)
    python3 screener_all_fundamental.py --output /path/to/dir
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from pathlib import Path
import argparse
import time
import os
import logging
import config


def download_screener_csv(output_dir, headless=False):
    # Configuration
    LOGIN_URL = "https://www.screener.in/login/"
    SCREEN_URL = f"https://www.screener.in/screens/{config.SCREENER_SCREEN_ID}/all-company/"
    DOWNLOAD_DIR = Path(output_dir)
    OUTPUT_FILE = DOWNLOAD_DIR / "all-company.csv"

    # Read credentials from external file (security improvement)
    credentials_file = config.SCREENER_CREDS
    try:
        with open(credentials_file, 'r') as f:
            lines = f.readlines()
            if len(lines) < 2:
                logging.error(f"❌ Credentials file incomplete: {credentials_file}")
                logging.error("   File must have email on line 1 and password on line 2")
                return False
            EMAIL = lines[0].strip()
            PASSWORD = lines[1].strip()
    except FileNotFoundError:
        logging.error(f"❌ Credentials file not found: {credentials_file}")
        logging.error("   Create file with email on line 1 and password on line 2")
        return False
    
    # Remove existing file
    if OUTPUT_FILE.exists():
        os.remove(OUTPUT_FILE)
        logging.info(f"🗑️ Removed existing file: {OUTPUT_FILE}")

    # Clean up any stale .crdownload temp files from previous failed runs.
    # These leftover files break the download-complete check below (which
    # waits for ALL .crdownload files to disappear), causing false timeouts.
    stale_crdownload = list(DOWNLOAD_DIR.glob('*.crdownload'))
    for f in stale_crdownload:
        try:
            os.remove(f)
            logging.info(f"🗑️ Removed stale temp file: {f.name}")
        except OSError:
            pass
    
    # Setup Chrome options
    chrome_options = Options()
    
    # Headless mode for faster execution
    if headless:
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--window-size=1920,1080')
        logging.info("🔇 Running in HEADLESS mode (no browser window)")
    
    chrome_options.add_experimental_option("prefs", {
        "download.default_directory": str(DOWNLOAD_DIR),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True
    })
    
    logging.info("🌐 Starting browser...")
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
    except Exception as e:
        logging.error(f"❌ Failed to initialize Chrome: {e}")
        return False
    
    # For headless mode, enable downloads explicitly
    if headless:
        driver.execute_cdp_cmd('Page.setDownloadBehavior', {
            'behavior': 'allow',
            'downloadPath': str(DOWNLOAD_DIR)
        })
    else:
        driver.maximize_window()
    
    wait = WebDriverWait(driver, 15)
    
    try:
        # 1. Login
        logging.info("🔑 Logging in...")
        driver.get(LOGIN_URL)
        
        # Wait for login form and fill credentials
        email_input = wait.until(EC.presence_of_element_located((By.NAME, "username")))
        password_input = driver.find_element(By.NAME, "password")
        
        email_input.send_keys(EMAIL)
        password_input.send_keys(PASSWORD)
        
        # Submit form using JavaScript to avoid click interception
        login_button = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'].button-primary"))
        )
        driver.execute_script("arguments[0].click();", login_button)
        
        # Wait for login to complete
        wait.until(lambda d: d.current_url != LOGIN_URL)
        logging.info("✅ Login successful!")
        
        # 2. Navigate to screen
        logging.info(f"📊 Loading screen: {SCREEN_URL}")
        driver.get(SCREEN_URL)
        
        # Wait for page to be fully loaded
        wait.until(lambda d: d.execute_script('return document.readyState') == 'complete')
        time.sleep(2)  # Extra wait for dynamic content
        
        # 3. Click Export button
        logging.info("📥 Clicking Export button...")
        export_button = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[aria-label="Download Results"]'))
        )
        driver.execute_script("arguments[0].click();", export_button)
        logging.info("✅ Export button clicked!")
        
        # Wait for download to complete
        logging.info("⏳ Waiting for download to complete...")
        timeout = 120  # 2 minutes
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            elapsed = int(time.time() - start_time)
            # Chrome writes a .crdownload temp file while the download is in
            # progress; only treat the file as complete when that temp file is
            # gone AND the final file is non-empty.
            crdownload_files = list(DOWNLOAD_DIR.glob('*.crdownload'))
            if (OUTPUT_FILE.exists() and OUTPUT_FILE.stat().st_size > 0
                    and not crdownload_files):
                logging.info(f"✅ Download complete: {OUTPUT_FILE}")
                logging.info(f"   ⏱️ Time taken: {elapsed}s")
                logging.info(f"   📦 File size: {OUTPUT_FILE.stat().st_size:,} bytes")

                # Verify it's CSV
                with open(OUTPUT_FILE, 'r') as f:
                    first_line = f.readline()
                    logging.info(f"   📋 First line: {first_line[:80]}...")
                return True

            # Progress indicator
            if elapsed % 10 == 0 and elapsed > 0:
                logging.info(f"   Waiting... {elapsed}s elapsed")
            time.sleep(1)
        
        logging.warning("⚠️ Download timeout - file may not have been created")
        logging.warning(f"   Waited for {timeout} seconds")
        return False
        
    except Exception as e:
        logging.error(f"❌ Error: {e}")
        # Save screenshot for debugging
        try:
            screenshot_path = DOWNLOAD_DIR / "error_screenshot.png"
            driver.save_screenshot(str(screenshot_path))
            logging.info(f"📸 Error screenshot saved to: {screenshot_path}")
        except Exception as ss_err:
            logging.error(f"   (also failed to save screenshot: {ss_err})")
        return False
    
    finally:
        driver.quit()
        logging.info("🔒 Browser closed.")


def main():
    parser = argparse.ArgumentParser(
        description='Download all-company.csv from Screener.in'
    )
    parser.add_argument(
        '--output',
        default=str(config.FUNDAMENTAL_DIR),
        help=f'Output directory for download (default: {config.FUNDAMENTAL_DIR})'
    )
    parser.add_argument(
        '--headless',
        action='store_true',
        help='Run in headless mode (no browser window) for faster execution'
    )
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-7s  %(message)s', datefmt='%H:%M:%S')
    
    logging.info("=" * 60)
    logging.info("📊 Screener.in All-Company CSV Downloader")
    logging.info("=" * 60)
    logging.info(f"📂 Output directory: {args.output}")
    if args.headless:
        logging.info("🔇 Mode: Headless")
    logging.info("=" * 60 + "\n")
    
    success = download_screener_csv(
        output_dir=args.output,
        headless=args.headless
    )
    
    if success:
        logging.info("\n🎉 Download completed successfully!")
    else:
        logging.info("\n❌ Download failed!")


if __name__ == "__main__":
    main()
