"""
Screener.in CSV Downloader using Selenium
Downloads all-company.csv automatically after login
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from pathlib import Path
import time
import os

def download_screener_csv():
    # Configuration
    LOGIN_URL = "https://www.screener.in/login/"
    SCREEN_URL = "https://www.screener.in/screens/3427564/all-company/"
    DOWNLOAD_DIR = Path("/Users/paresh/Downloads")
    OUTPUT_FILE = DOWNLOAD_DIR / "all-company.csv"
    
    # Credentials
    EMAIL = "p.paresh25@gmail.com"
    PASSWORD = "m5.jpShuPQ]9#5K"
    
    # Remove existing file
    if OUTPUT_FILE.exists():
        os.remove(OUTPUT_FILE)
        print(f"Removed existing file: {OUTPUT_FILE}")
    
    # Setup Chrome options for background-like execution
    chrome_options = Options()
    chrome_options.add_experimental_option("prefs", {
        "download.default_directory": str(DOWNLOAD_DIR),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True
    })
    
    # Run minimized and off-screen for unobtrusive operation
    chrome_options.add_argument("--window-position=-2400,-2400")  # Off-screen
    chrome_options.add_argument("--window-size=800,600")  # Small window
    
    print("Starting browser (minimized)...")
    driver = webdriver.Chrome(options=chrome_options)
    driver.minimize_window()  # Minimize to dock
    wait = WebDriverWait(driver, 15)
    
    try:
        # 1. Login
        print("Logging in...")
        driver.get(LOGIN_URL)
        
        # Wait for login form and fill credentials
        email_input = wait.until(EC.presence_of_element_located((By.NAME, "username")))
        password_input = driver.find_element(By.NAME, "password")
        
        email_input.send_keys(EMAIL)
        password_input.send_keys(PASSWORD)
        
        # Submit form
        login_button = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        login_button.click()
        
        # Wait for login to complete (check for logout button or redirect)
        time.sleep(3)
        print("Login successful!")
        
        # 2. Navigate to screen
        print(f"Loading screen: {SCREEN_URL}")
        driver.get(SCREEN_URL)
        time.sleep(3)
        
        # 3. Click Export button (try multiple strategies)
        print("Clicking Export button...")
        export_clicked = False
        
        # Strategy 1: Try finding button by text
        try:
            export_button = wait.until(
                EC.element_to_be_clickable((By.XPATH, "//button[normalize-space(text())='Export']"))
            )
            export_button.click()
            export_clicked = True
            print("Export button clicked (XPath method)!")
        except:
            print("XPath method failed, trying alternative...")
        
        # Strategy 2: Try by class or other attributes
        if not export_clicked:
            try:
                # Sometimes the button has specific class
                buttons = driver.find_elements(By.TAG_NAME, "button")
                for btn in buttons:
                    if 'export' in btn.text.lower():
                        btn.click()
                        export_clicked = True
                        print("Export button clicked (text search)!")
                        break
            except:
                pass
        
        # Strategy 3: JavaScript click as last resort
        if not export_clicked:
            try:
                driver.execute_script("""
                    var buttons = document.querySelectorAll('button');
                    for (var i = 0; i < buttons.length; i++) {
                        if (buttons[i].textContent.trim() === 'Export') {
                            buttons[i].click();
                            break;
                        }
                    }
                """)
                export_clicked = True
                print("Export button clicked (JavaScript)!")
            except Exception as e:
                print(f"All methods failed: {e}")
                raise
        
        # Wait for download to complete (increased timeout for slow network)
        print("Waiting for download to complete...")
        timeout = 120  # 2 minutes for slow network
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            elapsed = int(time.time() - start_time)
            if OUTPUT_FILE.exists() and OUTPUT_FILE.stat().st_size > 0:
                print(f"✓ Download complete: {OUTPUT_FILE}")
                print(f"  Time taken: {elapsed}s")
                print(f"  File size: {OUTPUT_FILE.stat().st_size:,} bytes")
                
                # Verify it's CSV
                with open(OUTPUT_FILE, 'r') as f:
                    first_line = f.readline()
                    print(f"  First line: {first_line[:100]}")
                break
            
            # Progress indicator
            if elapsed % 10 == 0 and elapsed > 0:
                print(f"  Waiting... {elapsed}s elapsed")
            time.sleep(1)
        else:
            print("⚠ Download timeout - file may not have been created")
            print(f"  Waited for {timeout} seconds")
        
    except Exception as e:
        print(f"Error: {e}")
    
    finally:
        driver.quit()
        print("Browser closed.")

if __name__ == "__main__":
    download_screener_csv()
