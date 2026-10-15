"""Fetch NSE's and BSE's corporate-action lists and keep them in the price store (`exchange_actions`).

Every run asks again for the window around today (60 days back, 45 ahead): an
exchange sometimes lists an action after its ex-date has begun, and version 1
of this screener fetched an 8-day window once and never again, so it missed
KOTYARK's 10:1 bonus (2026-06-24) and TAALTECH's split (2026-09-22) on NSE.
Rows are kept as the exchange printed them, with the factor read from the
purpose text (action_factors.py); a row already stored is not stored twice.

A fetch that SUCCEEDS replaces what is stored for what it covered: per
exchange, segment and requested year chunk that returned at least one row, the
stored rows whose ex-date falls in that chunk of the securities the answer
lists there (BSE by scrip code, NSE by ISIN, else by symbol) are deleted and
the fetched ones written, in one transaction. An exchange revises an ex-date or
a ratio now and then; when the old row stayed beside the new one, a near-1
bonus was applied twice or a corrected ratio was multiplied with the stale one.
A security absent from the answer keeps its rows: BSE's list leaves out scrips
delisted or amalgamated since (GSPL, JBCHEPHARM, ...), so an answer is not
complete. A fetch that fails (an exception, an HTTP error, NSE's "no data
found") or comes back empty deletes nothing.

The whole retained history is asked again per exchange when it was last fetched
in full more than 30 days ago (or never: `action_list_history`), so a listing
published long after its ex-date, or a history BSE refused during a rebuild, is
still seen. A rebuild asks for it at once.

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
HISTORY_REFRESH_DAYS = 30               # the whole retained range is asked again this often
HISTORY_LEAD_DAYS = 10                  # an ex-date this many days before the first stored day still matches it
EXCHANGES = ("NSE", "BSE")
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


def _records(answer) -> list[dict]:
    """NSE's action records: a list, or none for its {"msg": "no data found"} (or any other non-list) answer."""
    if isinstance(answer, dict):
        answer = answer.get("data")
    return [r for r in answer if isinstance(r, dict)] if isinstance(answer, list) else []


def nse_rows(records: list[dict], segment: str) -> pd.DataFrame:
    rows = []
    for r in _records(records):
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


def fetch_nse_parts(nse, start: date, end: date) -> list[tuple[str, str, date, date, pd.DataFrame]]:
    """(exchange, segment, chunk start, chunk end, rows) for every request NSE answered; raises on a failure."""
    parts = []
    for segment in NSE_SEGMENTS:
        for a, b in year_chunks(start, end):
            records = nse.actions(segment=segment, from_date=datetime.combine(a, datetime.min.time()),
                                  to_date=datetime.combine(b, datetime.min.time()))
            parts.append(("NSE", segment, a, b, nse_rows(records, segment)))
    return parts


def fetch_bse_parts(start: date, end: date, get=requests.get) -> list[tuple[str, str, date, date, pd.DataFrame]]:
    """(exchange, segment, chunk start, chunk end, rows) for every request BSE answered; raises on a failure."""
    parts = []
    for a, b in year_chunks(start, end):
        params = {"scripcode": "", "Fdate": a.strftime("%Y%m%d"), "TDate": b.strftime("%Y%m%d"), "Purposecode": "",
                  "strSearch": "S", "ddlindustrys": "", "ddlcategorys": "E", "segment": "0"}
        response = get(BSE_URL, headers=BSE_HEADERS, params=params, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"BSE action list answered HTTP {response.status_code}")
        parts.append(("BSE", "E", a, b, bse_rows(response.text)))
    return parts


def _joined(parts) -> pd.DataFrame:
    frames = [rows for *_, rows in parts if not rows.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)


def fetch_nse(nse, start: date, end: date) -> pd.DataFrame:
    return _joined(fetch_nse_parts(nse, start, end))


def fetch_bse(start: date, end: date, get=requests.get) -> pd.DataFrame:
    return _joined(fetch_bse_parts(start, end, get))


def _scopes_with_rows(rows: pd.DataFrame, replace) -> pd.DataFrame:
    """The (Exchange, Segment, From_Date, To_Date) scopes of `replace` that at least one fetched row falls in."""
    kept = []
    for exchange, segment, start, end in replace:
        inside = ((rows["Exchange"] == exchange) & (rows["Segment"] == segment)
                  & rows["Ex_Date"].between(pd.Timestamp(start), pd.Timestamp(end)))
        if inside.any():
            kept.append((exchange, segment, pd.Timestamp(start), pd.Timestamp(end)))
    return pd.DataFrame(kept, columns=["Exchange", "Segment", "From_Date", "To_Date"])


SAME_ROW = """
    a.Exchange = f.Exchange
    AND a.Segment IS NOT DISTINCT FROM f.Segment
    AND a.Symbol IS NOT DISTINCT FROM f.Symbol
    AND a.Code IS NOT DISTINCT FROM f.Code
    AND a.ISIN IS NOT DISTINCT FROM f.ISIN
    AND a.Ex_Date IS NOT DISTINCT FROM f.Ex_Date
    AND a.Purpose IS NOT DISTINCT FROM f.Purpose
"""
# A security is its BSE scrip code, else its ISIN, else (NSE SME rows carry none) its symbol.
SECURITY = "COALESCE({t}.Code, {t}.ISIN, 'symbol:' || {t}.Symbol)"
# A stored row is replaced only inside a fetched chunk AND for a security the answer lists in
# that chunk: BSE's list leaves out scrips that were delisted or amalgamated since, so rows of
# a security absent from the answer are kept.
IN_SCOPE = f"""
    a.Exchange = s.Exchange AND a.Segment IS NOT DISTINCT FROM s.Segment
    AND a.Ex_Date BETWEEN CAST(s.From_Date AS DATE) AND CAST(s.To_Date AS DATE)
    AND {SECURITY.format(t='a')} IN (
        SELECT {SECURITY.format(t='f')} FROM fetched_actions f
        WHERE f.Exchange = s.Exchange AND f.Segment IS NOT DISTINCT FROM s.Segment
          AND CAST(f.Ex_Date AS DATE) BETWEEN CAST(s.From_Date AS DATE) AND CAST(s.To_Date AS DATE))
"""


def store(conn, rows: pd.DataFrame, replace=()) -> tuple[int, int]:
    """Store fetched rows; returns (rows new to the store, stored rows the fetch no longer lists).

    `replace` names what the fetch covered, as (exchange, segment, start, end): the
    stored rows of such a scope with an ex-date in [start, end], of a security the
    fetch lists in that scope, are replaced by the fetched ones (delete, then
    insert, in one transaction) -- only for a scope the fetch returned at least one
    row for, so an empty answer deletes nothing, and never for a security the
    answer left out. Without `replace` rows are only added.
    """
    if rows.empty:
        return 0, 0
    rows = rows.drop_duplicates(subset=["Exchange", "Segment", "Symbol", "Code", "ISIN", "Ex_Date", "Purpose"]).copy()
    rows["Ex_Date"] = pd.to_datetime(rows["Ex_Date"])
    rows["Factor"] = pd.to_numeric(rows["Factor"], errors="coerce")
    for column in ("Exchange", "Segment", "Symbol", "Code", "ISIN", "Company", "Series", "Purpose", "Kind"):
        rows[column] = rows[column].astype("string")
    scopes = _scopes_with_rows(rows, replace)
    conn.register("fetched_actions", rows)
    conn.register("replaced_scopes", scopes)
    conn.execute("BEGIN TRANSACTION")
    try:
        new = conn.execute(f"SELECT count(*) FROM fetched_actions f WHERE NOT EXISTS "
                           f"(SELECT 1 FROM exchange_actions a WHERE {SAME_ROW})").fetchone()[0]
        removed = 0
        if not scopes.empty:
            removed = conn.execute(
                f"SELECT count(*) FROM exchange_actions a WHERE EXISTS (SELECT 1 FROM replaced_scopes s WHERE {IN_SCOPE}) "
                f"AND NOT EXISTS (SELECT 1 FROM fetched_actions f WHERE {SAME_ROW})").fetchone()[0]
            conn.execute(f"DELETE FROM exchange_actions a WHERE EXISTS (SELECT 1 FROM replaced_scopes s WHERE {IN_SCOPE})")
        conn.execute(f"INSERT INTO exchange_actions SELECT f.*, CURRENT_TIMESTAMP FROM fetched_actions f "
                     f"WHERE NOT EXISTS (SELECT 1 FROM exchange_actions a WHERE {SAME_ROW})")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.unregister("fetched_actions")
        conn.unregister("replaced_scopes")
    return int(new), int(removed)


def _fetchers(nse, start: date, end: date, get):
    return {"NSE": lambda: fetch_nse_parts(nse, start, end), "BSE": lambda: fetch_bse_parts(start, end, get)}


def refresh(conn, nse, start: date, end: date, logger, get=requests.get, exchanges=EXCHANGES) -> dict:
    """Fetch the exchanges' lists for [start, end] and store them; a failure is logged, not raised.

    Returns {exchange: text}; the text starts with "failed" or "skipped" when nothing was stored.
    """
    result = {}
    fetchers = _fetchers(nse, start, end, get)
    for exchange in exchanges:
        if exchange == "NSE" and nse is None:
            result[exchange] = "skipped: no NSE session"
            continue
        try:
            parts = fetchers[exchange]()
            rows = _joined(parts)
            if rows.empty:
                result[exchange] = "0 listed: nothing stored or replaced"
                continue
            new, removed = store(conn, rows, [(e, s, a, b) for e, s, a, b, _ in parts])
            result[exchange] = f"{len(rows)} listed, {new} new, {removed} no longer listed (replaced)"
        except Exception as error:                  # an exchange that refuses today is asked again tomorrow
            result[exchange] = f"failed: {error}"
            logger.warning(f"{exchange} corporate-action list unavailable ({error}); using the stored list.")
    logger.info(f"Corporate-action lists {start} -> {end}: {result}")
    return result


def _ensure_history_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS action_list_history "
                 "(Exchange VARCHAR NOT NULL, From_Date DATE, To_Date DATE, Fetched_On DATE)")


def history_due(conn, exchange: str, today: date) -> bool:
    """True when `exchange`'s whole retained list was never fetched, or last fetched HISTORY_REFRESH_DAYS ago or more."""
    _ensure_history_table(conn)
    last = conn.execute("SELECT max(Fetched_On) FROM action_list_history WHERE Exchange = ?", [exchange]).fetchone()[0]
    return last is None or (today - last).days >= HISTORY_REFRESH_DAYS


def record_history(conn, exchange: str, start: date, end: date, today: date) -> None:
    _ensure_history_table(conn)
    conn.execute("DELETE FROM action_list_history WHERE Exchange = ?", [exchange])
    conn.execute("INSERT INTO action_list_history VALUES (?, ?, ?, ?)", [exchange, start, end, today])


def refresh_history(conn, nse, start: date, end: date, today: date, logger, get=requests.get,
                    exchanges=EXCHANGES) -> dict:
    """Fetch the whole range [start, end] and record, per exchange, a fetch that stored rows."""
    result = refresh(conn, nse, start, end, logger, get, exchanges)
    for exchange, text in result.items():
        if not text.startswith(("failed", "skipped", "0 listed")):
            record_history(conn, exchange, start, end, today)
    return result


def retained_start(conn) -> date | None:
    """The first ex-date that can still match a stored day: HISTORY_LEAD_DAYS before the first raw row."""
    first = conn.execute("SELECT min(Date) FROM raw_prices").fetchone()[0]
    return None if first is None else first - timedelta(HISTORY_LEAD_DAYS)


def refresh_history_if_due(conn, nse, today: date, end: date, logger, get=requests.get) -> dict:
    """Post-sync: ask again for the whole retained range of each exchange whose last full fetch is due.

    Never raises: the outcome is logged and returned ({} when nothing was due).
    """
    try:
        start = retained_start(conn)
        due = [exchange for exchange in EXCHANGES if history_due(conn, exchange, today)]
        if start is None or not due:
            return {}
        logger.info(f"Corporate-action history {start} -> {end} due for {', '.join(due)} "
                    f"(last full fetch missing or {HISTORY_REFRESH_DAYS}+ days old)")
        return refresh_history(conn, nse, start, end, today, logger, get, due)
    except Exception as error:
        logger.warning(f"Corporate-action history refresh failed ({error}); the stored lists are used.")
        return {"failed": str(error)}
