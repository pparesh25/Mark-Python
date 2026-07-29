"""
Screener.in CSV Downloader

The export button on a screen page is a plain Django form POST, so the CSV can
be fetched directly over HTTP without driving a browser.

Usage:
    python3 fundamental_downloader.py
    python3 fundamental_downloader.py --output /path/to/dir
"""

from pathlib import Path
import argparse
import secrets
import time
import logging
import config

BASE_URL = "https://www.screener.in"
# Screener.in serves a different (JS-only) page to unrecognised clients.
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)
# A valid export always starts with these columns; anything else means we were
# handed a login or error page and must not overwrite a good file.
EXPECTED_HEADER_FIELD = "ISIN Code"
LOGIN_TIMEOUT_SECONDS = 30
EXPORT_TIMEOUT_SECONDS = 120


def _read_credentials():
    """Return (email, password) from the credentials file outside the repo."""
    lines = config.SCREENER_CREDS.read_text(encoding="utf-8").strip().splitlines()
    if len(lines) < 2:
        raise RuntimeError(
            f"Credentials file incomplete: {config.SCREENER_CREDS} "
            "(email on line 1, password on line 2)"
        )
    return lines[0].strip(), lines[1].strip()


def _download_via_http(
    output_file: Path,
    *,
    session=None,
) -> bool:
    """Log in and export the screen with two direct HTTP requests."""
    import requests

    email, password = _read_credentials()
    started = time.perf_counter()

    session = session or requests.Session()
    session.headers.update({"User-Agent": HTTP_USER_AGENT})

    # Django accepts an unmasked 32-character CSRF secret when the form value
    # and cookie match. Creating that pair locally avoids a separate login-page
    # GET, leaving only the login and export requests.
    login_url = f"{BASE_URL}/login/"
    csrf_token = secrets.token_hex(16)
    session.cookies.set(
        "csrftoken",
        csrf_token,
        domain="www.screener.in",
        path="/",
    )

    logging.info("🔑 Logging in...")
    login = session.post(
        login_url,
        data={
            "csrfmiddlewaretoken": csrf_token,
            "next": "",
            "username": email,
            "password": password,
        },
        headers={"Referer": login_url},
        timeout=LOGIN_TIMEOUT_SECONDS,
    )
    login.raise_for_status()
    if login.url.rstrip("/").endswith("/login"):
        raise RuntimeError("Login rejected — check the credentials file")

    # Django rotates the CSRF token on login, so the refreshed cookie is a valid
    # form token for the export POST and saves fetching the screen page.
    screen_url = f"{BASE_URL}/screens/{config.SCREENER_SCREEN_ID}/all-company/"
    export_url = (
        f"{BASE_URL}/api/export/screen/?url_name=screen"
        f"&screen_id={config.SCREENER_SCREEN_ID}&slug_name=all-company"
    )
    logging.info("📥 Requesting export...")
    export = session.post(
        export_url,
        data={"csrfmiddlewaretoken": session.cookies.get("csrftoken", "")},
        headers={"Referer": screen_url},
        timeout=EXPORT_TIMEOUT_SECONDS,
    )
    export.raise_for_status()

    content_type = export.headers.get("Content-Type", "")
    if "csv" not in content_type.lower():
        raise RuntimeError(f"Export returned {content_type!r}, not a CSV")
    header = export.content.split(b"\n", 1)[0].decode("utf-8", "replace")
    if EXPECTED_HEADER_FIELD not in header:
        raise RuntimeError(f"Export header missing {EXPECTED_HEADER_FIELD!r}: {header[:120]}")

    # Write through a temp file so a failure never truncates the previous CSV.
    output_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = output_file.with_suffix(".csv.part")
    try:
        temp_file.write_bytes(export.content)
        temp_file.replace(output_file)
    except Exception:
        temp_file.unlink(missing_ok=True)
        raise

    elapsed = time.perf_counter() - started
    logging.info(f"✅ Download complete: {output_file}")
    logging.info(f"   ⏱️ Time taken: {elapsed:.2f}s")
    logging.info(f"   📦 File size: {output_file.stat().st_size:,} bytes")
    logging.info(f"   📋 First line: {header[:80]}...")
    return True


def download_screener_csv(output_dir):
    """Download all-company.csv directly over HTTP."""
    output_file = Path(output_dir) / "all-company.csv"
    return _download_via_http(output_file)


def main():
    parser = argparse.ArgumentParser(
        description='Download all-company.csv from Screener.in'
    )
    parser.add_argument(
        '--output',
        default=str(config.FUNDAMENTAL_DIR),
        help=f'Output directory for download (default: {config.FUNDAMENTAL_DIR})'
    )
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-7s  %(message)s', datefmt='%H:%M:%S')
    
    logging.info("=" * 60)
    logging.info("📊 Screener.in All-Company CSV Downloader")
    logging.info("=" * 60)
    logging.info(f"📂 Output directory: {args.output}")
    logging.info("⚡ Mode: optimized direct HTTP")
    logging.info("=" * 60 + "\n")

    try:
        download_screener_csv(output_dir=args.output)
        logging.info("\n🎉 Download completed successfully!")
    except Exception as error:
        logging.error(f"\n❌ Download failed: {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
