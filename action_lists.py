"""Fetch NSE's and BSE's corporate-action lists and keep them in the price store (`exchange_actions`).

Every run asks again for the window around today (60 days back, 45 ahead): an
exchange sometimes lists an action after its ex-date has begun, and version 1
of this screener fetched an 8-day window once and never again, so it missed
KOTYARK's 10:1 bonus (2026-06-24) and TAALTECH's split (2026-09-22) on NSE.
A rebuild asks for the whole retained history once. Rows are kept as the
exchange printed them, with the factor read from the purpose text
(action_factors.py); a row already stored is not stored twice.

    NSE  nse.actions(segment, from_date, to_date) for equities, sme and mf (ETF unit splits are
         only in mf). `isin` is a real ISIN for equities, a number for SME (kept as null).
    BSE  api.bseindia.com/BseIndiaAPI/api/CorpactCSVDownload/w, equity (category E): Security
         Code (the scrip code, which survives renames), Security Name (TODAY's short name),
         Ex Date, Purpose. BSE's server answers 403 to an address for a while after many
         requests; a failed fetch is logged and the stored list is used until the next run.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from action_factors import parse_purpose

LOOK_BACK_DAYS, LOOK_AHEAD_DAYS = 60, 45
NSE_SEGMENTS = ("equities", "sme", "mf")
BSE_URL = "https://api.bseindia.com/BseIndiaAPI/api/CorpactCSVDownload/w"
BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}
ISIN = re.compile(r"^IN[A-Z0-9]{10}$")
COLUMNS = ["Exchange", "Segment", "Symbol", "Code", "ISIN", "Company", "Series", "Ex_Date", "Purpose", "Kind", "Factor"]


def window(today: date) -> tuple[date, date]:
    return today - timedelta(LOOK_BACK_DAYS), today + timedelta(LOOK_AHEAD_DAYS)


def year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """[start, end] cut at year ends: one request a year is what NSE answers in full."""
    chunks, current = [], start
    while current <= end:
        last = min(date(current.year, 12, 31), end)
        chunks.append((current, last))
        current = last + timedelta(1)
    return chunks


def _date(text, fmt: str):
    try:
        return datetime.strptime(str(text).strip(), fmt).date()
    except ValueError:
        return None


def nse_rows(records: list[dict], segment: str) -> pd.DataFrame:
    rows = []
    for r in records or []:
        isin = str(r.get("isin") or "").strip().upper()
        rows.append({"Exchange": "NSE", "Segment": segment, "Symbol": str(r.get("symbol") or "").strip().upper(),
                     "Code": None, "ISIN": isin if ISIN.match(isin) else None,
                     "Company": str(r.get("comp") or "").strip(), "Series": str(r.get("series") or "").strip() or None,
                     "Ex_Date": _date(r.get("exDate"), "%d-%b-%Y"), "Purpose": str(r.get("subject") or "").strip()})
    return with_factors(pd.DataFrame(rows, columns=COLUMNS[:-2]))


def bse_rows(text: str) -> pd.DataFrame:
    text = text.lstrip("﻿")
    if not text.startswith("Security Code"):
        raise ValueError(f"not BSE's action list: {text[:80]!r}")
    frame = pd.read_csv(io.StringIO(text), dtype=str).rename(columns=lambda c: str(c).strip())
    rows = pd.DataFrame({
        "Exchange": "BSE", "Segment": "E",
        "Symbol": frame["Security Name"].str.strip().str.upper(), "Code": frame["Security Code"].str.strip(),
        "ISIN": None, "Company": frame.get("Company Name", pd.Series(dtype=str)).str.strip(), "Series": None,
        "Ex_Date": [_date(v, "%d %b %Y") for v in frame["Ex Date"]], "Purpose": frame["Purpose"].str.strip()})
    return with_factors(rows)


def with_factors(rows: pd.DataFrame) -> pd.DataFrame:
    parsed = [parse_purpose(p) for p in rows["Purpose"].fillna("")]
    rows = rows.copy()
    rows["Kind"] = [kind for kind, _, _ in parsed]
    rows["Factor"] = [factor for _, factor, _ in parsed]
    return rows.loc[:, COLUMNS]


def fetch_nse(nse, start: date, end: date) -> pd.DataFrame:
    frames = []
    for segment in NSE_SEGMENTS:
        for a, b in year_chunks(start, end):
            records = nse.actions(segment=segment, from_date=datetime.combine(a, datetime.min.time()),
                                  to_date=datetime.combine(b, datetime.min.time()))
            frames.append(nse_rows(records, segment))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)


def fetch_bse(start: date, end: date, get=requests.get) -> pd.DataFrame:
    frames = []
    for a, b in year_chunks(start, end):
        params = {"scripcode": "", "Fdate": a.strftime("%Y%m%d"), "TDate": b.strftime("%Y%m%d"), "Purposecode": "",
                  "strSearch": "S", "ddlindustrys": "", "ddlcategorys": "E", "segment": "0"}
        response = get(BSE_URL, headers=BSE_HEADERS, params=params, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"BSE action list answered HTTP {response.status_code}")
        frames.append(bse_rows(response.text))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)


def store(conn, rows: pd.DataFrame) -> int:
    """Add the rows not stored yet; returns how many were new."""
    if rows.empty:
        return 0
    rows = rows.drop_duplicates(subset=["Exchange", "Segment", "Symbol", "Code", "ISIN", "Ex_Date", "Purpose"]).copy()
    rows["Ex_Date"] = pd.to_datetime(rows["Ex_Date"])
    rows["Factor"] = pd.to_numeric(rows["Factor"], errors="coerce")
    for column in ("Exchange", "Segment", "Symbol", "Code", "ISIN", "Company", "Series", "Purpose", "Kind"):
        rows[column] = rows[column].astype("string")
    conn.register("fetched_actions", rows)
    try:
        before = conn.execute("SELECT count(*) FROM exchange_actions").fetchone()[0]
        conn.execute(
            """
            INSERT INTO exchange_actions
            SELECT f.*, CURRENT_TIMESTAMP FROM fetched_actions f
            WHERE NOT EXISTS (
                SELECT 1 FROM exchange_actions a
                WHERE a.Exchange = f.Exchange
                  AND a.Segment IS NOT DISTINCT FROM f.Segment
                  AND a.Symbol IS NOT DISTINCT FROM f.Symbol
                  AND a.Code IS NOT DISTINCT FROM f.Code
                  AND a.ISIN IS NOT DISTINCT FROM f.ISIN
                  AND a.Ex_Date IS NOT DISTINCT FROM f.Ex_Date
                  AND a.Purpose IS NOT DISTINCT FROM f.Purpose
            )
            """
        )
        return conn.execute("SELECT count(*) FROM exchange_actions").fetchone()[0] - before
    finally:
        conn.unregister("fetched_actions")


def refresh(conn, nse, start: date, end: date, logger, get=requests.get) -> dict:
    """Fetch both exchanges' lists for [start, end] and store them; a failure is logged, not raised."""
    result = {}
    for exchange, fetch in (("NSE", lambda: fetch_nse(nse, start, end)), ("BSE", lambda: fetch_bse(start, end, get))):
        if exchange == "NSE" and nse is None:
            result[exchange] = "skipped: no NSE session"
            continue
        try:
            rows = fetch()
            result[exchange] = f"{len(rows)} listed, {store(conn, rows)} new"
        except Exception as error:                  # an exchange that refuses today is asked again tomorrow
            result[exchange] = f"failed: {error}"
            logger.warning(f"{exchange} corporate-action list unavailable ({error}); using the stored list.")
    logger.info(f"Corporate-action lists {start} -> {end}: {result}")
    return result
