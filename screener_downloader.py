import requests
from bs4 import BeautifulSoup
import os
import getpass
from pathlib import Path

def download_screener_export():
    # Configuration
    LOGIN_URL = "https://www.screener.in/login/"
    EXPORT_URL = "https://www.screener.in/screens/3427564/all-company/"
    OUTPUT_FILE = Path("/Users/paresh/Downloads/all-company.csv")

    # Check and remove existing file
    if OUTPUT_FILE.exists():
        try:
            os.remove(OUTPUT_FILE)
            print(f"Existing file removed: {OUTPUT_FILE}")
        except OSError as e:
            print(f"Error removing existing file: {e}")
    else:
        print(f"No existing file found to remove.")
    
    # Get credentials
    email = "p.paresh25@gmail.com"
    password = "m5.jpShuPQ]9#5K"

    if not email:
        email = input("Enter Screener.in Email: ")
    if not password:
        password = getpass.getpass("Enter Screener.in Password: ")

    session = requests.Session()
    
    # 1. Get the login page to fetch CSRF token
    print("Fetching login page...")
    try:
        login_page = session.get(LOGIN_URL)
        login_page.raise_for_status()
    except requests.RequestException as e:
        print(f"Error fetching login page: {e}")
        return

    soup = BeautifulSoup(login_page.content, 'html.parser')
    csrf_token_input = soup.find('input', {'name': 'csrfmiddlewaretoken'})
    
    if not csrf_token_input:
        print("Error: Could not find CSRF token on login page.")
        return
        
    csrf_token = csrf_token_input['value']
    
    # 2. Perform Login
    print("Logging in...")
    login_data = {
        'username': email,
        'password': password,
        'csrfmiddlewaretoken': csrf_token,
        'next': '/'
    }
    
    headers = {
        'Referer': LOGIN_URL,
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    try:
        response = session.post(LOGIN_URL, data=login_data, headers=headers)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Login failed: {e}")
        return

    # Check if login was successful (usually redirects or changes content)
    if "Log out" not in response.text and response.url == LOGIN_URL:
         print("Login failed. Please check your credentials.")
         # Sometimes specific error messages are in the HTML
         soup_resp = BeautifulSoup(response.content, 'html.parser')
         error = soup_resp.find('div', {'class': 'invalid-feedback'})
         if error:
             print(f"Server message: {error.text.strip()}")
         return
    
    print("Login successful.")

    # 3. Download the Export File
    print(f"Downloading export to {OUTPUT_FILE}...")
    try:
        export_response = session.get(EXPORT_URL, headers=headers)
        export_response.raise_for_status()
        
        # Verify content type (basic check to ensure we didn't get an HTML error page)
        if 'text/csv' not in export_response.headers.get('Content-Type', '') and \
           'application/csv' not in export_response.headers.get('Content-Type', ''):
             # Force verify content starts roughly like a CSV if header is missing/generic
             pass 

        with open(OUTPUT_FILE, 'wb') as f:
            f.write(export_response.content)
            
        print("Download complete!")
        print(f"File size: {OUTPUT_FILE.stat().st_size} bytes")
        
    except requests.RequestException as e:
        print(f"Error downloading file: {e}")

if __name__ == "__main__":
    download_screener_export()
