"""
Balance Sheet Exporter for Screener.in
Downloads balance sheet Excel files for all NSE symbols
Based on screener_downloader_selenium.py
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from pathlib import Path
import time
import os
import argparse
from datetime import datetime

class BalanceSheetExporter:
    def __init__(self, symbols_file, download_dir, test_mode=False):
        # Configuration
        self.LOGIN_URL = "https://www.screener.in/login/"
        self.SYMBOLS_FILE = Path(symbols_file)
        self.DOWNLOAD_DIR = Path(download_dir)
        self.PROGRESS_FILE = self.DOWNLOAD_DIR / "progress.txt"
        self.ERROR_LOG = self.DOWNLOAD_DIR / "errors.log"
        self.TEST_MODE = test_mode
        
        # Credentials
        self.EMAIL = "p.paresh25@gmail.com"
        self.PASSWORD = "m5.jpShuPQ]9#5K"
        
        # Create download directory
        self.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        
        # Statistics
        self.stats = {
            'total': 0,
            'processed': 0,
            'successful': 0,
            'failed': 0,
            'skipped': 0
        }
        
    def read_symbols(self):
        """Read NSE symbols from file"""
        with open(self.SYMBOLS_FILE, 'r') as f:
            symbols = [line.strip().replace('\r', '') for line in f if line.strip()]
        
        if self.TEST_MODE:
            symbols = symbols[:5]  # Test mode: only first 5 symbols
            
        self.stats['total'] = len(symbols)
        return symbols
    
    def get_processed_symbols(self):
        """Get list of already processed symbols"""
        if not self.PROGRESS_FILE.exists():
            return set()
        
        with open(self.PROGRESS_FILE, 'r') as f:
            return set(line.strip() for line in f if line.strip())
    
    def mark_as_processed(self, symbol):
        """Mark symbol as processed"""
        with open(self.PROGRESS_FILE, 'a') as f:
            f.write(f"{symbol}\n")
        self.stats['processed'] += 1
    
    def log_error(self, symbol, error):
        """Log error to file"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.ERROR_LOG, 'a') as f:
            f.write(f"[{timestamp}] {symbol}: {error}\n")
        self.stats['failed'] += 1
    
    def wait_for_download(self, symbol, timeout=45):
        """Wait for Excel file to be downloaded and handle duplicates"""
        start_time = time.time()
        
        print(f" Downloading...", end='', flush=True)
        
        # Wait for file to appear
        downloaded_files = []
        
        while time.time() - start_time < timeout:
            # Get all Excel files
            all_files = list(self.DOWNLOAD_DIR.glob("*.xlsx")) + list(self.DOWNLOAD_DIR.glob("*.xls"))
            
            # Find files created/modified after start_time
            for file_path in all_files:
                # Skip if already in our list
                if file_path in downloaded_files:
                    continue
                
                # Skip if it's already renamed with our pattern
                if file_path.name.startswith(f"{symbol}_balance_sheet"):
                    continue
                
                # Check if file is new and has content
                if file_path.stat().st_size > 0 and file_path.stat().st_mtime > start_time:
                    downloaded_files.append(file_path)
                    print(".", end='', flush=True)
            
            # If we found files, wait a bit more to catch duplicates, then break
            if downloaded_files:
                time.sleep(2)  # Wait to catch potential duplicates
                # Do one more check for duplicates
                all_files = list(self.DOWNLOAD_DIR.glob("*.xlsx")) + list(self.DOWNLOAD_DIR.glob("*.xls"))
                for file_path in all_files:
                    if file_path not in downloaded_files and not file_path.name.startswith(f"{symbol}_balance_sheet"):
                        if file_path.stat().st_size > 0 and file_path.stat().st_mtime > start_time:
                            downloaded_files.append(file_path)
                break
                
            time.sleep(0.5)
        
        if not downloaded_files:
            print(" ✗")
            return False
        
        # Sort by modification time (earliest first)
        downloaded_files.sort(key=lambda f: f.stat().st_mtime)
        
        # Keep the first file, delete all others
        primary_file = downloaded_files[0]
        
        # Generate new filename
        new_name = self.DOWNLOAD_DIR / f"{symbol}_balance_sheet{primary_file.suffix}"
        
        # If target name already exists, add timestamp
        if new_name.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            new_name = self.DOWNLOAD_DIR / f"{symbol}_balance_sheet_{timestamp}{primary_file.suffix}"
        
        # Rename primary file
        try:
            primary_file.rename(new_name)
            print(f" ✓")
        except Exception as e:
            print(f" ✗ Rename failed: {e}")
            return False
        
        # Delete all duplicate files
        if len(downloaded_files) > 1:
            for dup_file in downloaded_files[1:]:
                try:
                    if dup_file.exists():
                        dup_file.unlink()
                except:
                    pass
        
        # Cleanup Chrome-numbered duplicates: "filename (1).xlsx", "filename (2).xlsx"
        import re
        for pattern in ["*.xlsx", "*.xls"]:
            for file_path in self.DOWNLOAD_DIR.glob(pattern):
                # Skip our renamed files
                if file_path.name.startswith(f"{symbol}_balance_sheet"):
                    continue
                # Check for Chrome duplicate pattern
                if re.search(r'\s+\(\d+\)\.(xlsx|xls)$', file_path.name):
                    if file_path.stat().st_mtime > start_time:
                        try:
                            file_path.unlink()
                        except:
                            pass
        
        return True
    
    def login(self, driver, wait):
        """Login to screener.in"""
        print("Logging in...")
        driver.get(self.LOGIN_URL)
        
        # Wait for login form and fill credentials
        email_input = wait.until(EC.presence_of_element_located((By.NAME, "username")))
        password_input = driver.find_element(By.NAME, "password")
        
        email_input.send_keys(self.EMAIL)
        password_input.send_keys(self.PASSWORD)
        
        # Submit form using JavaScript to avoid click interception
        login_button = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'].button-primary"))
        )
        driver.execute_script("arguments[0].click();", login_button)
        
        # Wait for login to complete (wait for URL change)
        wait.until(lambda d: d.current_url != self.LOGIN_URL)
        print("Login successful!")
        
        # Navigate to a base company page to ensure search box is available
        print("Navigating to base page...")
        driver.get("https://www.screener.in/company/ASHOKLEY/consolidated/")
        wait.until(lambda d: d.execute_script('return document.readyState') == 'complete')
        time.sleep(1)  # Small delay to ensure page is fully rendered
    
    def search_and_export(self, driver, wait, symbol):
        """Search for symbol and export balance sheet"""
        try:
            # Use JavaScript to interact with the search box (since it may be hidden visually)
            search_script = f"""
            var searchBox = document.querySelector('input[aria-label="Search for a company"]');
            if (searchBox) {{
                searchBox.value = '{symbol}';
                searchBox.dispatchEvent(new Event('input', {{ bubbles: true }}));
                searchBox.dispatchEvent(new Event('change', {{ bubbles: true }}));
                
                // Simulate Enter keypress
                var event = new KeyboardEvent('keydown', {{
                    key: 'Enter',
                    code: 'Enter',
                    keyCode: 13,
                    which: 13,
                    bubbles: true
                }});
                searchBox.dispatchEvent(event);
                return true;
            }}
            return false;
            """
            
            result = driver.execute_script(search_script)
            if not result:
                raise Exception("Search box not found via JavaScript")
            
            time.sleep(0.5)  # Small delay after triggering search
            
            # Wait for company page to load
            wait.until(lambda d: '/company/' in d.current_url)
            wait.until(lambda d: d.execute_script('return document.readyState') == 'complete')
            
            # Wait for search dropdown to close and page to fully render
            time.sleep(3)  # Increased from 2 to 3 seconds
            
            # Click Export to Excel button
            export_button = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[aria-label="Export to Excel"]'))
            )
            driver.execute_script("arguments[0].click();", export_button)
            
            # Wait for download to complete
            if self.wait_for_download(symbol, timeout=45):
                self.stats['successful'] += 1
                return True
            else:
                raise Exception("Download timeout")
                
        except Exception as e:
            raise Exception(f"Search/Export failed: {str(e)}")
    
    def run(self):
        """Main execution"""
        symbols = self.read_symbols()
        processed_symbols = self.get_processed_symbols()
        
        print(f"\n{'='*60}")
        print(f"Balance Sheet Exporter for Screener.in")
        print(f"{'='*60}")
        print(f"Total symbols: {self.stats['total']}")
        print(f"Already processed: {len(processed_symbols)}")
        print(f"Download directory: {self.DOWNLOAD_DIR}")
        if self.TEST_MODE:
            print(f"TEST MODE: Processing only first 5 symbols")
        print(f"{'='*60}\n")
        
        # Setup Chrome
        chrome_options = Options()
        chrome_options.add_experimental_option("prefs", {
            "download.default_directory": str(self.DOWNLOAD_DIR),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True
        })
        
        print("Starting browser...")
        driver = webdriver.Chrome(options=chrome_options)
        driver.maximize_window()
        wait = WebDriverWait(driver, 15)
        
        try:
            # Login
            self.login(driver, wait)
            
            # Process each symbol
            start_time = time.time()
            
            for idx, symbol in enumerate(symbols, 1):
                # Skip if already processed
                if symbol in processed_symbols:
                    print(f"[{idx}/{self.stats['total']}] {symbol} - Skipped (already processed)")
                    self.stats['skipped'] += 1
                    continue
                
                try:
                    # Process symbol
                    print(f"[{idx}/{self.stats['total']}] {symbol} - Processing...", end=' ')
                    
                    if self.search_and_export(driver, wait, symbol):
                        print("✓ Downloaded")
                        self.mark_as_processed(symbol)
                    
                    # Calculate progress
                    progress = ((idx) / self.stats['total']) * 100
                    elapsed = time.time() - start_time
                    avg_time = elapsed / idx
                    remaining = (self.stats['total'] - idx) * avg_time
                    
                    print(f"    Progress: {progress:.1f}% | "
                          f"Success: {self.stats['successful']} | "
                          f"Failed: {self.stats['failed']} | "
                          f"ETA: {remaining/60:.1f} min")
                    
                except Exception as e:
                    print(f"✗ Failed - {str(e)}")
                    self.log_error(symbol, str(e))
                    # Continue with next symbol
                    continue
            
            # Final statistics
            elapsed = time.time() - start_time
            print(f"\n{'='*60}")
            print(f"COMPLETED!")
            print(f"{'='*60}")
            print(f"Total symbols: {self.stats['total']}")
            print(f"Successful: {self.stats['successful']}")
            print(f"Failed: {self.stats['failed']}")
            print(f"Skipped: {self.stats['skipped']}")
            print(f"Time taken: {elapsed/60:.1f} minutes")
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"\nFatal error: {e}")
        
        finally:
            driver.quit()
            print("Browser closed.")

def main():
    parser = argparse.ArgumentParser(description='Download balance sheets from Screener.in')
    parser.add_argument(
        '--symbols',
        default='/Users/paresh/Desktop/Mark Python/all nse symbol.txt',
        help='Path to NSE symbols file'
    )
    parser.add_argument(
        '--output',
        default='/Users/paresh/Downloads/balance_sheets',
        help='Output directory for downloads'
    )
    parser.add_argument(
        '--test',
        action='store_true',
        help='Test mode: process only first 5 symbols'
    )
    parser.add_argument(
        '--resume',
        action='store_true',
        help='Resume from last position (skip already processed symbols)'
    )
    
    args = parser.parse_args()
    
    exporter = BalanceSheetExporter(
        symbols_file=args.symbols,
        download_dir=args.output,
        test_mode=args.test
    )
    
    exporter.run()

if __name__ == "__main__":
    main()
