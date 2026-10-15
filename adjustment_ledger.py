"""Stored adjustments with a ledger (price store version 3): decide only what can have changed, keep it, check it.

Version 2 recomputed every adjusted price on every run (`price_adjustments.rebuild`,
12-24 s of a run). Version 3 stores the decisions and the adjusted prices and,
after each sync, decides again only the symbols whose decisions can have
changed. The principle stays: an adjusted price is raw x the product of the
symbol's ledger factors after that day, rewritten from `raw_prices` whenever
the symbol's factors change -- never derived from an adjusted price -- so
replaying any step gives the same result and a factor cannot be applied twice.

Correctness is defined by the full rebuild: after any sequence of daily runs
the ledger, price_adjustments, adjustment_periods, gap_history, symbol_state
and every eod_prices row equal what `rebuild()` (the oracle) writes from the
same raw prices, lists, overrides and gap history. tests/test_adjustment_ledger.py
drives both day by day and compares them after every day.

Tables (eod_store.py): adjustment_ledger (applied factors, one a symbol-day),
gap_history, eod_prices (a table), symbol_state, maintenance_state.

Daily flow (`maintain`, from price_adjustments.refresh after the lists were fetched):

  1. symbol_state holds, per symbol, the raw rows the last decision saw (count,
     last day, the sum of a hash of each row's day, open and close) and a digest
     of its other inputs. One aggregate over raw_prices classifies each symbol:
     same, append (only rows after its last day), rewritten (a backfilled, replayed
     with other prices, or removed row -- any row on or before its last day
     changed), new, or vanished.
  2. The affected symbols are collected by the dependency rules below. A backfill
     is never an append: a new day changes its neighbours' gaps.
  3. They are decided again on their full history with the same function the
     rebuild uses (price_adjustments.decide_exchange), NSE first, then BSE with the
     NSE twins' new factors.
  4. In ONE transaction: their price_adjustments, ledger (Decided_At kept for an
     unchanged factor), gap_history and periods rows are replaced; eod_prices is
     rewritten from raw for every symbol whose periods changed or whose raw rows
     were rewritten, and the appended rows of all others are written with their
     ledger's factors (normally none after a new day, never assumed); then the
     checks run, and any failure rolls everything back.

Dependency rules (the reasons logged; when unsure a rule takes more symbols, never fewer):

  rewritten        raw rows on or before the symbol's last decided day changed or went (fill-missing,
                   a replayed or re-sent day with other prices, a ticker reused by another issuer)
  new              a series without state: a new listing, both names of a rename (the upsert moves
                   the derived rows with the raw rows, gap history included, and drops both states,
                   since NSE lists match by name and overrides are written per name)
  vanished         no raw rows left (renamed away, all aged out): every derived row is deleted
  gap              an appended day whose open gaps below 0.80 or at 1.8 and up (priced from Rs 5):
                   the gap rules, the noise count and gap_history
  halt             an appended day more than 10 days after the previous one: a listing no day matched
                   is flagged once the symbol trades again, and a return after 366 days is a new listing
  override_day     an override for an appended day (an override for a future day starts to match)
  listing_day      a listed action with an ex-date an appended day can be the first day within 10 of
  listing_due      a listed action whose "no trade within 10 days" check comes due: ex-date + 10 days
                   reached by the exchange's last stored day or by today; or the exchange's first day moved
  inputs           the digest of the symbol's resolved list rows (a list replacement can add or remove
                   rows; a full history refresh too; NSE resolves by ISIN, prefix or today's name, so
                   securities changes count), its overrides (in file order), and for BSE its NSE twin
                   (the ISIN mapping) and the twin's applied factors (the twin factor, the nse_covers
                   rule and the same-event match with BSE listings in _twin_meets_listing)
  twin_pending     BSE: the twin has a factor dated on or after the BSE series' last day; it lands on
                   the first BSE day on or after NSE's day, however late
  twin_covered     BSE: a new NSE day of the twin can make BSE days within 10 days after it "covered"
                   (no BSE gap rule while the twin trades); only BSE rows more than 10 days after the
                   twin's previous day can change, so a normal day takes none
  twin_rewritten   BSE: the twin's raw rows were rewritten, are new or vanished (covered days, twin days)
  retention        rows aged out (apply_retention marks Pruned_Before). The new first day loses its gap:
                   a symbol with a decision or flag stored on or before it is taken. A listed action
                   matched to an aged-out day can move to a later one: ex-dates from 10 days before the
                   exchange's old first day to the new first day. On BSE, days the NSE twin's aged-out
                   rows covered lose the cover: a gap candidate up to 10 days after the later of NSE's
                   first day and the twin's. (A twin factor lost with NSE's rows is `inputs`.) All
                   others only lose their ledger and price_adjustments rows before the exchange's first
                   day; their periods are recomputed (the remaining factors multiply in the same order,
                   so their adjusted prices stay bit for bit). gap_history keeps the aged-out gaps, so
                   the noise count does not change.
  range_back       the exchange's last stored day moved back: all its symbols

A DuckDB or rule version change, or a store never maintained, takes the full rebuild.

Checks (all tested):

  - adjustment_ledger PRIMARY KEY (Exchange, Symbol, Date) and CHECK (Factor > 0 AND isfinite):
    a second factor on a symbol-day, a factor of 0, below 0 or not finite cannot be stored.
  - price_adjustments.refuse_repeats: one Event_Key (a listed action without its day) twice
    within 30 days, or a BSE listing beside an NSE twin factor of the same ratio, is refused
    and flagged; a factor whose adjusted open still gaps 25% or more is flagged for review.
  - check_prices: every stored adjusted row of the touched symbols is raw x the product of the
    symbol's later ledger factors (to rounding); none missing, none extra.
  - check_continuity: on each ledger day of a touched symbol the stored adjusted open over the
    stored previous close is the raw gap over the factor -- a factor applied twice or not at all
    shows there. A failed check rolls the maintenance back, names the symbols in the review
    file and runs the full rebuild instead.
  - verify (weekly, `run_screener.py --verify`, after every migration): the oracle is computed
    and every derived table compared with it, eod_prices row for row; a difference is logged
    with its symbols and the derived tables are replaced by the oracle's in one transaction.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import eod_store
import price_adjustments as pa

module_logger = logging.getLogger(__name__)

RULE_VERSION = 1                       # bump when a decision rule changes: the next run rebuilds in full
VERIFY_EVERY_DAYS = 7
EXCHANGES = ("NSE", "BSE")
ONE_DAY = pd.Timedelta(days=1)
LEDGER_COLUMNS = ["Exchange", "Symbol", "Date", "Factor", "Event", "Event_Key", "Gap", "Listed_Kind",
                  "Listed_Factor"]
GAP_COLUMNS = ["Exchange", "Symbol", "Date", "Gap"]
PERIOD_COLUMNS = ["Exchange", "Symbol", "From_Date", "Adj"]
CONTINUITY_TOLERANCE = 0.02            # |ln| between a stored adjusted gap and the raw gap over the factor
CHECK_MIN_PRICE = 0.01                 # below this the 4-place rounding is too coarse to check a gap
MIGRATION_HEADROOM = 256 * 1024 * 1024
REASONS = ("rewritten", "new", "gap", "halt", "override_day", "listing_day", "listing_due", "inputs",
           "twin_pending", "twin_covered", "twin_rewritten", "retention", "range_back")


class LedgerCheckError(RuntimeError):
    """A check of the stored prices failed; `problems` holds (Exchange, Symbol, what) rows."""

    def __init__(self, problems: list[tuple]):
        self.problems = problems
        shown = "; ".join(f"{e} {s}: {w}" for e, s, w in problems[:5])
        super().__init__(f"{len(problems)} stored-price check(s) failed: {shown}")


# --- reading ---------------------------------------------------------------------------------------

def _frame(conn, sql: str, params=None) -> pd.DataFrame:
    """A query's rows; datetime columns in nanoseconds, so every frame merges with every other."""
    frame = conn.execute(sql, params or []).df()
    for column in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[column]):
            frame[column] = frame[column].astype("datetime64[ns]")
    return frame


def _days(values) -> pd.Series:
    return pd.to_datetime(values).astype("datetime64[ns]")


def _day_text(values) -> pd.Series:
    return pd.Series(pd.to_datetime(values)).dt.strftime("%Y-%m-%d").fillna("NaT")


@dataclass
class Inputs:
    """Everything a decision reads besides the raw prices, read once a run."""
    today: date
    actions: pd.DataFrame
    securities: pd.DataFrame
    overrides: pd.DataFrame
    ranges: dict                       # exchange -> (first, last) stored day, or (None, None)
    moving: dict                       # exchange -> price-moving listed actions resolved to stored symbols
    twins: dict                        # BSE symbol -> NSE twin symbol


def read_inputs(conn, overrides_path: Path | None, today: date, logger: logging.Logger) -> Inputs:
    securities = _frame(conn, "SELECT Exchange, Symbol, ISIN, Series, Code FROM securities")
    actions = _frame(conn, "SELECT * FROM exchange_actions")
    overrides = pa.read_overrides(overrides_path, logger)
    overrides = overrides.assign(Date=_days(overrides["Date"])) if not overrides.empty else overrides
    ranges = {exchange: (None, None) for exchange in EXCHANGES}
    for exchange, first, last in conn.execute(
            "SELECT Exchange, min(Date), max(Date) FROM raw_prices GROUP BY Exchange").fetchall():
        ranges[exchange] = (first, last)
    moving = {exchange: pa._moving_actions(actions, securities, exchange) for exchange in EXCHANGES}
    return Inputs(today, actions, securities, overrides, ranges, moving, pa.twin_map(securities))


def _load_raw(conn, exchange: str, symbols=None) -> pd.DataFrame:
    """Symbol, Date, Open, Close of `exchange` (all a decision reads), for `symbols` or all."""
    if symbols is None:
        return _frame(conn, "SELECT Symbol, Date, Open, Close FROM raw_prices WHERE Exchange = ?", [exchange])
    conn.register("wanted_symbols", pd.DataFrame({"Symbol": sorted(symbols)}, dtype=object))
    try:
        return _frame(conn, "SELECT r.Symbol, r.Date, r.Open, r.Close FROM raw_prices r "
                            "JOIN wanted_symbols w ON w.Symbol = r.Symbol WHERE r.Exchange = ?", [exchange])
    finally:
        conn.unregister("wanted_symbols")


def _frozen_counts(conn, exchange: str) -> pd.Series:
    """Per symbol, the gap_history rows on or before its first raw day: sure gaps no longer stored."""
    frame = conn.execute(
        """
        SELECT g.Symbol, count(*) AS n
        FROM gap_history g
        JOIN (SELECT Symbol, min(Date) AS First FROM raw_prices WHERE Exchange = $ex GROUP BY Symbol) f
          ON f.Symbol = g.Symbol
        WHERE g.Exchange = $ex AND g.Date <= f.First
        GROUP BY g.Symbol
        """, {"ex": exchange}).df()
    return frame.set_index("Symbol")["n"] if not frame.empty else pd.Series(dtype=float)


def _nse_days(conn, twin_symbols: set, nse_ledger: pd.DataFrame) -> pd.DataFrame:
    """Every NSE day of the twins with its applied Factor and Event (what twin_inputs reads)."""
    if not twin_symbols:
        return pd.DataFrame({"Symbol": pd.Series(dtype=object), "Date": pd.Series(dtype="datetime64[ns]"),
                             "Event": pd.Series(dtype=object), "Factor": pd.Series(dtype=float)})
    conn.register("twin_symbols", pd.DataFrame({"Symbol": sorted(twin_symbols)}, dtype=object))
    try:
        days = _frame(conn, "SELECT r.Symbol, r.Date FROM raw_prices r JOIN twin_symbols t ON t.Symbol = r.Symbol "
                            "WHERE r.Exchange = 'NSE'")
    finally:
        conn.unregister("twin_symbols")
    applied = nse_ledger[["Symbol", "Date", "Event", "Factor"]].assign(Date=_days(nse_ledger["Date"]))
    days = days.merge(applied, on=["Symbol", "Date"], how="left")
    days["Factor"] = pd.to_numeric(days["Factor"], errors="coerce").fillna(1.0)
    return days


def _empty(columns) -> pd.DataFrame:
    return pd.DataFrame({column: pd.Series(dtype=object) for column in columns})


def _concat(frames, columns) -> pd.DataFrame:
    """The non-empty frames, concatenated (an empty frame with `columns` when there are none)."""
    frames = [frame[columns] for frame in frames if frame is not None and not frame.empty]
    return pd.concat(frames, ignore_index=True) if frames else _empty(columns)


@dataclass
class Part:
    """One exchange's decisions for some symbols (or all)."""
    exchange: str
    symbols: set
    events: pd.DataFrame               # price_adjustments rows
    ledger: pd.DataFrame               # applied factors (LEDGER_COLUMNS)
    gaps: pd.DataFrame                 # the sure gaps now stored (GAP_COLUMNS)


def decide_part(conn, inputs: Inputs, exchange: str, symbols=None, nse_ledger: pd.DataFrame | None = None,
                history: bool = True) -> Part:
    """Decide `symbols` of `exchange` (None: all) on their full history; for BSE with `nse_ledger`'s factors."""
    raw = _load_raw(conn, exchange, symbols)
    present = set(raw["Symbol"].unique())
    if raw.empty:
        return Part(exchange, set(), _empty(pa.EVENT_COLUMNS), _empty(LEDGER_COLUMNS), _empty(GAP_COLUMNS))
    frozen = _frozen_counts(conn, exchange) if history else None
    nse_days = None
    if exchange == "BSE":
        nse_ledger = nse_ledger if nse_ledger is not None else _empty(LEDGER_COLUMNS)
        nse_days = _nse_days(conn, {inputs.twins[s] for s in present if s in inputs.twins}, nse_ledger)
    first, last = inputs.ranges[exchange]
    decided, missed = pa.decide_exchange(exchange, raw, inputs.actions, inputs.securities, inputs.overrides,
                                         inputs.today, first, last, nse_days, frozen, inputs.moving[exchange])
    events = decided[decided["Event"].notna() | decided["Flag"].notna() | decided["Listed_Kind"].notna()]
    events = events[pa.EVENT_COLUMNS]
    events = _concat([events, missed], pa.EVENT_COLUMNS)
    ledger = decided[decided["Event"].notna() & (decided["Factor"] != 1.0)][LEDGER_COLUMNS]
    gaps = decided[decided["Sure"].astype(bool)][GAP_COLUMNS]
    parts = []
    for frame in (events, ledger, gaps):
        parts.append(frame.assign(Date=_days(frame["Date"])).reset_index(drop=True))
    return Part(exchange, present, *parts)


def periods_of(ledger: pd.DataFrame) -> pd.DataFrame:
    """adjustment_periods rows of the ledger rows (price_adjustments.adjustment_periods, dates as dates)."""
    if ledger.empty:
        return _empty(PERIOD_COLUMNS)
    rows = ledger[["Exchange", "Symbol", "Date", "Factor", "Event"]]
    return pa.adjustment_periods(rows.assign(Date=pd.to_datetime(rows["Date"]).dt.date))


def input_digests(inputs: Inputs, exchange: str, symbols, nse_ledger: pd.DataFrame | None = None) -> pd.DataFrame:
    """(Exchange, Symbol, Inputs_Digest): a hash of everything besides its raw prices a symbol is decided from.

    Its resolved list rows, its overrides in file order, and on BSE its NSE twin and the twin's applied
    factors. A symbol whose digest differs from its symbol_state row is decided again.
    """
    symbols = sorted(symbols)
    lines = []
    moving = inputs.moving[exchange]
    if not moving.empty:
        lines.append(pd.DataFrame({"Symbol": moving["Symbol"].to_numpy(), "Line": (
            "list|" + _day_text(moving["Ex_Date"]).to_numpy() + "|" + moving["Kind"].fillna("").astype(str).to_numpy()
            + "|" + moving["Factor"].map(repr).to_numpy())}))
    overrides = inputs.overrides[inputs.overrides["Exchange"] == exchange] if not inputs.overrides.empty \
        else inputs.overrides
    if not overrides.empty:
        position = pd.Series(range(len(overrides))).map(lambda n: f"{n:06d}").to_numpy()
        lines.append(pd.DataFrame({"Symbol": overrides["Symbol"].to_numpy(), "Line": (
            "override|" + position + "|" + _day_text(overrides["Date"]).to_numpy() + "|"
            + overrides["Factor"].map(repr).to_numpy())}))
    if exchange == "BSE" and inputs.twins:
        pairs = pd.DataFrame({"Symbol": list(inputs.twins), "Twin": list(inputs.twins.values())})
        lines.append(pd.DataFrame({"Symbol": pairs["Symbol"], "Line": "twin|" + pairs["Twin"]}))
        if nse_ledger is not None and not nse_ledger.empty:
            events = pairs.merge(nse_ledger[["Symbol", "Date", "Factor"]].rename(columns={"Symbol": "Twin"}), on="Twin")
            if not events.empty:
                lines.append(pd.DataFrame({"Symbol": events["Symbol"].to_numpy(), "Line": (
                    "twin_event|" + _day_text(events["Date"]).to_numpy() + "|"
                    + events["Factor"].map(repr).to_numpy())}))
    text: dict = {}
    if lines:
        joined = pd.concat(lines, ignore_index=True).sort_values(["Symbol", "Line"])
        text = joined.groupby("Symbol")["Line"].agg("\n".join).to_dict()
    digests = [hashlib.sha1(text.get(symbol, "").encode("utf-8")).hexdigest()[:20] for symbol in symbols]
    return pd.DataFrame({"Exchange": exchange, "Symbol": symbols, "Inputs_Digest": digests})


@dataclass
class Decisions:
    """The oracle: every derived table as the full rebuild writes it."""
    inputs: Inputs
    events: pd.DataFrame
    ledger: pd.DataFrame
    periods: pd.DataFrame
    gap_history: pd.DataFrame
    digests: pd.DataFrame


def decide_all(conn, inputs: Inputs, history: bool = True) -> Decisions:
    """Decide every symbol (the full rebuild); `history` reads and writes gap_history (version 3)."""
    nse = decide_part(conn, inputs, "NSE", None, None, history)
    bse = decide_part(conn, inputs, "BSE", None, nse.ledger, history)
    events = _concat([nse.events, bse.events], pa.EVENT_COLUMNS)
    ledger = _concat([nse.ledger, bse.ledger], LEDGER_COLUMNS)
    gaps = _concat([nse.gaps, bse.gaps], GAP_COLUMNS)
    if history:
        frozen = _frame(conn, """
            SELECT g.Exchange, g.Symbol, g.Date, g.Gap FROM gap_history g
            JOIN (SELECT Exchange, Symbol, min(Date) AS First FROM raw_prices GROUP BY Exchange, Symbol) f
              ON f.Exchange = g.Exchange AND f.Symbol = g.Symbol
            WHERE g.Date <= f.First""")
        gaps = _concat([frozen, gaps], GAP_COLUMNS)
    digests = _concat([input_digests(inputs, "NSE", nse.symbols),
                       input_digests(inputs, "BSE", bse.symbols, nse.ledger)], ["Exchange", "Symbol", "Inputs_Digest"])
    return Decisions(inputs, events, ledger, periods_of(ledger), gaps, digests)


# --- state ---------------------------------------------------------------------------------------------

def read_state(conn) -> dict:
    if "maintenance_state" not in eod_store.relations(conn):
        return {}
    return dict(conn.execute("SELECT Key, Value FROM maintenance_state").fetchall())


def _state_date(state: dict, key: str):
    value = state.get(key)
    return date.fromisoformat(value) if value else None


def _write_state(conn, inputs: Inputs, verified: bool, overrides_hash: str) -> None:
    values = {"schema_version": "3", "rule_version": str(RULE_VERSION), "duckdb_version": duckdb.__version__,
              "today": inputs.today.isoformat(), "overrides_hash": overrides_hash,
              "maintained_at": datetime.now(pa.MARKET_TZ).strftime("%Y-%m-%d %H:%M:%S")}
    for exchange, (first, last) in inputs.ranges.items():
        values[f"first_{exchange}"] = first.isoformat() if first else ""
        values[f"last_{exchange}"] = last.isoformat() if last else ""
    if verified:
        values["last_full_verify"] = inputs.today.isoformat()
    conn.register("new_state", pd.DataFrame(list(values.items()), columns=["Key", "Value"]))
    try:
        conn.execute("INSERT OR REPLACE INTO maintenance_state SELECT Key, Value FROM new_state")
    finally:
        conn.unregister("new_state")


def _overrides_hash(overrides: pd.DataFrame) -> str:
    text = overrides[["Exchange", "Symbol", "Date", "Factor"]].astype(str).to_csv(index=False) \
        if not overrides.empty else ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def _write_symbol_state(conn, digests: pd.DataFrame, source: str = "raw") -> None:
    """symbol_state from the raw aggregates (`source` "raw": computed now; else a temp table of them)."""
    aggregates = (f"(SELECT Exchange, Symbol, count(*) AS Rows, min(Date) AS First_Date, max(Date) AS Last_Date, "
                  f"sum({eod_store.row_hash()}) AS Digest FROM raw_prices GROUP BY Exchange, Symbol)"
                  if source == "raw" else source)
    conn.register("symbol_digests", digests[["Exchange", "Symbol", "Inputs_Digest"]])
    try:
        conn.execute("DELETE FROM symbol_state")
        conn.execute(f"""
            INSERT INTO symbol_state
            SELECT a.Exchange, a.Symbol, a.Rows, a.First_Date, a.Last_Date, a.Digest, d.Inputs_Digest, NULL
            FROM {aggregates} a
            LEFT JOIN symbol_digests d ON d.Exchange = a.Exchange AND d.Symbol = a.Symbol""")
    finally:
        conn.unregister("symbol_digests")


# --- writing ---------------------------------------------------------------------------------------

def _with_decided_at(ledger: pd.DataFrame, stored: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Ledger rows with Decided_At: kept from the stored row when the factor is unchanged, else now."""
    keys = ["Exchange", "Symbol", "Date", "Factor", "Event", "Event_Key"]
    ledger = ledger[LEDGER_COLUMNS].assign(Date=_days(ledger["Date"]))
    if not stored.empty:
        known = stored[keys + ["Decided_At"]].assign(Date=_days(stored["Date"])).drop_duplicates(keys)
        ledger = ledger.merge(known, on=keys, how="left")
    else:
        ledger = ledger.assign(Decided_At=pd.NaT)
    ledger["Decided_At"] = pd.to_datetime(ledger["Decided_At"]).fillna(pd.Timestamp(now))
    return ledger.assign(Rule_Version=RULE_VERSION)


def _insert(conn, table: str, frame: pd.DataFrame, columns: list[str]) -> None:
    if frame.empty:
        return
    name = f"new_{table}"
    conn.register(name, frame[columns])
    try:
        listed = ", ".join(columns)
        conn.execute(f"INSERT INTO {table} ({listed}) SELECT {listed} FROM {name}")
    finally:
        conn.unregister(name)


def _stored_ledger(conn) -> pd.DataFrame:
    return _frame(conn, "SELECT * FROM adjustment_ledger")


def write_all(conn, decisions: Decisions, now: datetime) -> None:
    """Replace every derived table with the oracle's (inside the caller's transaction; version 3)."""
    stored = _stored_ledger(conn)
    conn.execute("DELETE FROM price_adjustments")
    _insert(conn, "price_adjustments", decisions.events, pa.EVENT_COLUMNS)
    conn.execute("DELETE FROM adjustment_ledger")
    _insert(conn, "adjustment_ledger", _with_decided_at(decisions.ledger, stored, now),
            LEDGER_COLUMNS + ["Decided_At", "Rule_Version"])
    conn.execute("DELETE FROM adjustment_periods")
    _insert(conn, "adjustment_periods", decisions.periods, PERIOD_COLUMNS)
    conn.execute("DELETE FROM gap_history")
    _insert(conn, "gap_history", decisions.gap_history, GAP_COLUMNS)
    # eod_prices: all rows into an empty table; else only the symbols whose rows differ from raw x the new
    # periods -- rewriting 3.8 million equal rows would only grow the file by the size of the table.
    if conn.execute("SELECT count(*) FROM (SELECT 1 FROM eod_prices LIMIT 1)").fetchone()[0] == 0:
        eod_store.rematerialize(conn)
    else:
        pairs = _eod_differences(conn, decisions.periods)
        if pairs:
            conn.register("differing_symbols", pd.DataFrame(pairs, columns=["Exchange", "Symbol"]))
            try:
                eod_store.rematerialize(conn, "differing_symbols")
            finally:
                conn.unregister("differing_symbols")
    _write_symbol_state(conn, decisions.digests)
    _write_state(conn, decisions.inputs, verified=True, overrides_hash=_overrides_hash(decisions.inputs.overrides))


# --- checks ------------------------------------------------------------------------------------------

def _near(stored: str, expected: str, tolerance: str) -> str:
    return (f"COALESCE(({stored} IS NULL AND {expected} IS NULL) OR {stored} = {expected} "
            f"OR abs({stored} - {expected}) <= {tolerance} + 1e-9 * abs({expected}), false)")


def _scope_sql(table: str, scope: str | None) -> str:
    if scope is None:
        return table
    return (f"(SELECT t.* FROM {table} t JOIN {scope} k ON k.Exchange = t.Exchange AND k.Symbol = t.Symbol "
            "AND (k.After IS NULL OR t.Date > k.After))")


def check_prices(conn, scope: pd.DataFrame | None = None) -> list[tuple]:
    """(Exchange, Symbol, what) for stored adjusted rows that are not raw x the product of the symbol's later
    ledger factors (to the 4-place rounding), raw rows without one, and adjusted rows without a raw row.

    `scope` (Exchange, Symbol, After): the rows after After (all when None) of those symbols; None: all.
    The ledger is read directly, not adjustment_periods, so a period table out of step is caught too.
    """
    if scope is not None and scope.empty:
        return []
    if scope is not None:
        conn.register("check_scope", scope[["Exchange", "Symbol", "After"]].assign(After=_days(scope["After"])))
    try:
        raw_rows = _scope_sql("raw_prices", "check_scope" if scope is not None else None)
        eod_rows = _scope_sql("eod_prices", "check_scope" if scope is not None else None)
        prices = " AND ".join(_near(f"e.{c}", f"r.{c} * r.Adj", "1e-4") for c in ("Open", "High", "Low", "Close"))
        quantities = " AND ".join(_near(f"e.{c}", f"r.{c} / r.Adj", "1.0") for c in ("Volume", "Dlv_Qty"))
        rows = conn.execute(f"""
            WITH r0 AS (SELECT * FROM {raw_rows}),
            adj AS (SELECT r0.Exchange, r0.Symbol, r0.Date, exp(sum(ln(l.Factor))) AS Adj
                    FROM r0 JOIN adjustment_ledger l
                      ON l.Exchange = r0.Exchange AND l.Symbol = r0.Symbol AND l.Date > r0.Date
                    GROUP BY r0.Exchange, r0.Symbol, r0.Date),
            r AS (SELECT r0.*, COALESCE(adj.Adj, 1.0) AS Adj FROM r0 LEFT JOIN adj
                    ON adj.Exchange = r0.Exchange AND adj.Symbol = r0.Symbol AND adj.Date = r0.Date),
            e AS (SELECT * FROM {eod_rows}),
            raw_n AS (SELECT Exchange, Symbol, Date, count(*) AS n FROM r0 GROUP BY ALL),
            eod_n AS (SELECT Exchange, Symbol, Date, count(*) AS n FROM e GROUP BY ALL)
            SELECT DISTINCT r.Exchange, r.Symbol, 'adjusted row is not raw x ledger factors' AS what
              FROM r LEFT JOIN e ON e.Exchange = r.Exchange AND e.Symbol = r.Symbol AND e.Date = r.Date
             WHERE e.Symbol IS NULL OR NOT ({prices} AND {quantities}
                   AND e.Total_Trades IS NOT DISTINCT FROM r.Total_Trades)
            UNION
            SELECT DISTINCT e.Exchange, e.Symbol, 'adjusted row without its raw row'
              FROM eod_n e LEFT JOIN raw_n r ON r.Exchange = e.Exchange AND r.Symbol = e.Symbol AND r.Date = e.Date
             WHERE e.n > COALESCE(r.n, 0)
            ORDER BY 1, 2""").fetchall()
    finally:
        if scope is not None:
            conn.unregister("check_scope")
    return [tuple(row) for row in rows]


def check_continuity(conn, symbols: pd.DataFrame | None = None) -> list[tuple]:
    """(Exchange, Symbol, what) where the stored adjusted gap on a ledger day is not the raw gap over its factor.

    A factor stored twice divides the history again and leaves the gap at gap / factor^2; a factor
    missing from the stored rows leaves the raw gap. `symbols` (Exchange, Symbol): those only; None: all.
    """
    if symbols is not None and symbols.empty:
        return []
    if symbols is not None:
        conn.register("continuity_scope", symbols[["Exchange", "Symbol"]].assign(After=pd.NaT))
    try:
        eod_rows = _scope_sql("eod_prices", "continuity_scope" if symbols is not None else None)
        rows = conn.execute(f"""
            WITH e AS (SELECT Exchange, Symbol, Date, Open,
                              lag(Close) OVER (PARTITION BY Exchange, Symbol ORDER BY Date) AS Prev_Close
                       FROM {eod_rows})
            SELECT l.Exchange, l.Symbol, l.Date, l.Factor, l.Gap, e.Open / e.Prev_Close AS Stored_Gap
            FROM adjustment_ledger l
            JOIN e ON e.Exchange = l.Exchange AND e.Symbol = l.Symbol AND e.Date = l.Date
            WHERE l.Gap > 0 AND e.Prev_Close >= {CHECK_MIN_PRICE} AND e.Open >= {CHECK_MIN_PRICE}
              AND abs(ln((e.Open / e.Prev_Close) / (l.Gap / l.Factor))) > {CONTINUITY_TOLERANCE}
            ORDER BY 1, 2, 3""").fetchall()
    finally:
        if symbols is not None:
            conn.unregister("continuity_scope")
    problems = []
    for exchange, symbol, day, factor, gap, stored_gap in rows:
        twice = abs(np.log(stored_gap) - np.log(gap / factor ** 2)) < CONTINUITY_TOLERANCE
        problems.append((exchange, symbol, f"{day}: stored adjusted gap {stored_gap:.4f}, expected "
                         f"{gap / factor:.4f} (factor {factor:g}){' -- applied twice' if twice else ''}"))
    return problems


def _eod_differences(conn, periods: pd.DataFrame) -> list[tuple]:
    """(Exchange, Symbol) whose eod_prices rows are not, row for row, raw x `periods` (the store's own SQL)."""
    conn.register("oracle_periods", periods.assign(From_Date=_days(periods["From_Date"]))
                  if not periods.empty else _empty(PERIOD_COLUMNS))
    try:
        expected = eod_store.eod_select(periods="oracle_periods")
        pairs = conn.execute(f"""
            WITH x AS ({expected})
            SELECT DISTINCT Exchange, Symbol FROM (
                (SELECT * FROM eod_prices EXCEPT ALL SELECT * FROM x)
                UNION ALL
                (SELECT * FROM x EXCEPT ALL SELECT * FROM eod_prices))
            ORDER BY 1, 2""").fetchall()
    finally:
        conn.unregister("oracle_periods")
    return [tuple(pair) for pair in pairs]


def differences(conn, decisions: Decisions) -> dict:
    """{what: sorted (Exchange, Symbol) pairs} where the stored derived tables differ from the oracle's."""
    stored = _stored_ledger(conn)
    oracle = {
        "price_adjustments": (decisions.events, pa.EVENT_COLUMNS),
        "adjustment_ledger": (_with_decided_at(decisions.ledger, stored, datetime.now()), LEDGER_COLUMNS
                              + ["Rule_Version"]),
        "adjustment_periods": (decisions.periods, PERIOD_COLUMNS),
        "gap_history": (decisions.gap_history, GAP_COLUMNS),
    }
    casts = {"Date": "CAST(Date AS DATE) AS Date", "From_Date": "CAST(From_Date AS DATE) AS From_Date"}
    found: dict = {}
    for table, (frame, columns) in oracle.items():
        listed = ", ".join(casts.get(column, column) for column in columns)
        name = f"oracle_{table}"
        conn.register(name, frame[columns] if not frame.empty else _empty(columns))
        try:
            pairs = conn.execute(f"""
                SELECT DISTINCT Exchange, Symbol FROM (
                    (SELECT {listed} FROM {table} EXCEPT ALL SELECT {listed} FROM {name})
                    UNION ALL
                    (SELECT {listed} FROM {name} EXCEPT ALL SELECT {listed} FROM {table}))
                ORDER BY 1, 2""").fetchall()
        finally:
            conn.unregister(name)
        if pairs:
            found[table] = [tuple(pair) for pair in pairs]
    pairs = _eod_differences(conn, decisions.periods)
    if pairs:
        found["eod_prices"] = pairs
    conn.register("oracle_digests", decisions.digests)
    try:
        pairs = conn.execute(f"""
            WITH x AS (SELECT a.Exchange, a.Symbol, a.Rows, a.First_Date, a.Last_Date, a.Digest, d.Inputs_Digest
                       FROM (SELECT Exchange, Symbol, count(*) AS Rows, min(Date) AS First_Date, max(Date) AS Last_Date,
                                    sum({eod_store.row_hash()}) AS Digest FROM raw_prices GROUP BY Exchange, Symbol) a
                       LEFT JOIN oracle_digests d ON d.Exchange = a.Exchange AND d.Symbol = a.Symbol),
                 s AS (SELECT Exchange, Symbol, Rows, First_Date, Last_Date, Digest, Inputs_Digest FROM symbol_state)
            SELECT DISTINCT Exchange, Symbol FROM ((SELECT * FROM s EXCEPT ALL SELECT * FROM x)
                                                    UNION ALL (SELECT * FROM x EXCEPT ALL SELECT * FROM s))
            ORDER BY 1, 2""").fetchall()
    finally:
        conn.unregister("oracle_digests")
    if pairs:
        found["symbol_state"] = [tuple(pair) for pair in pairs]
    return found


# --- the full rebuild (the oracle) ----------------------------------------------------------------------

def _unmatched_overrides(conn, overrides: pd.DataFrame, logger: logging.Logger) -> int:
    if overrides.empty:
        return 0
    conn.register("override_symbols", overrides[["Exchange", "Symbol"]].drop_duplicates())
    try:
        stored = _frame(conn, "SELECT r.Exchange, r.Symbol, r.Date FROM raw_prices r JOIN override_symbols o "
                              "ON o.Exchange = r.Exchange AND o.Symbol = r.Symbol")
    finally:
        conn.unregister("override_symbols")
    return pa.warn_unmatched_overrides(overrides, stored, logger)


def write_review(conn, review_path: Path | None, today: date, extra: pd.DataFrame | None = None) -> int:
    """The review file from price_adjustments (the last 30 days' flags) plus `extra` rows; returns its rows."""
    since = pd.Timestamp(today) - pd.Timedelta(days=pa.REVIEW_DAYS)
    events = _frame(conn, "SELECT * FROM price_adjustments WHERE Date >= ?", [since.date()])
    securities = _frame(conn, "SELECT Exchange, Symbol, ISIN, Series, Code FROM securities")
    review = pa.review_list(events, today, securities)
    if extra is not None and not extra.empty:
        review = pd.concat([review, extra], ignore_index=True)
    if review_path is not None:
        review.to_csv(review_path, index=False)
    return len(review)


def _summary(conn, mode: str, review_rows: int, unmatched: int, started: float, ledger: pd.DataFrame | None = None,
             **extra) -> dict:
    """The run's summary; `ledger` (the applied factors) defaults to the stored adjustment_ledger."""
    if ledger is None:
        ledger = _frame(conn, "SELECT Exchange, Event FROM adjustment_ledger")
    factors = {f"{exchange} {event}": int(n)
               for (exchange, event), n in ledger.groupby(["Exchange", "Event"]).size().items()}
    rows = conn.execute("SELECT count(*) FROM raw_prices").fetchone()[0]
    flagged = conn.execute("SELECT count(*) FROM price_adjustments WHERE Flag IS NOT NULL").fetchone()[0]
    return {"mode": mode, "rows": int(rows), "factors": factors, "flagged": int(flagged), "review_rows": review_rows,
            "overrides_unmatched": unmatched, "seconds": round(time.perf_counter() - started, 2), **extra}


def _full_checks(conn, decisions: Decisions | None = None) -> list[tuple]:
    problems = check_prices(conn) + check_continuity(conn)
    if decisions is not None:
        for table, pairs in differences(conn, decisions).items():
            problems += [(exchange, symbol, f"{table} differs from the full rebuild") for exchange, symbol in pairs]
    return problems


def rebuild(conn, overrides_path: Path | None = None, review_path: Path | None = None, today: date | None = None,
            logger: logging.Logger | None = None) -> dict:
    """Decide every factor from what is stored and write it in one transaction (the oracle).

    Version 3: every derived table, eod_prices row for row, checked before the commit. Version 2:
    adjustment_periods and price_adjustments, as before (its eod_prices is a view).
    """
    logger = logger or module_logger
    today = today or pa.market_today()
    started = time.perf_counter()
    version = eod_store.schema_version(conn)
    inputs = read_inputs(conn, overrides_path, today, logger)
    unmatched = _unmatched_overrides(conn, inputs.overrides, logger)
    decisions = decide_all(conn, inputs, history=version == 3)
    pa.check_factors(decisions.events, decisions.periods)                # before anything is written
    conn.execute("BEGIN TRANSACTION")
    try:
        if version == 3:
            write_all(conn, decisions, datetime.now(pa.MARKET_TZ).replace(tzinfo=None))
            problems = check_prices(conn) + check_continuity(conn)
            if problems:
                raise LedgerCheckError(problems)
        else:
            conn.execute("DELETE FROM adjustment_periods")
            _insert(conn, "adjustment_periods", decisions.periods, PERIOD_COLUMNS)
            conn.execute("DELETE FROM price_adjustments")
            _insert(conn, "price_adjustments", decisions.events, pa.EVENT_COLUMNS)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    review_rows = write_review(conn, review_path, today)
    return _summary(conn, "full", review_rows, unmatched, started, ledger=decisions.ledger)


# --- the daily, incremental maintenance -------------------------------------------------------------------

class _Affected:
    """Symbols to decide again, per exchange, with the reasons."""

    def __init__(self):
        self.by = {exchange: {} for exchange in EXCHANGES}

    def add(self, exchange: str, symbols, reason: str, present: set | None = None) -> None:
        bucket = self.by[exchange]
        for symbol in symbols:
            if present is None or symbol in present:
                bucket.setdefault(symbol, set()).add(reason)

    def symbols(self, exchange: str) -> set:
        return set(self.by[exchange])

    def counts(self, exchange: str) -> dict:
        return dict(Counter(reason for reasons in self.by[exchange].values() for reason in reasons))


def _classify(conn) -> pd.DataFrame:
    """Per symbol: Kind (same, append, rewritten, new, vanished) against symbol_state; temp table symbol_changes."""
    conn.execute(f"""
        CREATE OR REPLACE TEMP TABLE symbol_changes AS
        WITH agg AS (
            SELECT r.Exchange, r.Symbol, count(*) AS Rows, min(r.Date) AS First_Date, max(r.Date) AS Last_Date,
                   sum({eod_store.row_hash('r.')}) AS Digest,
                   count(*) FILTER (WHERE r.Date <= s.Last_Date) AS Old_Rows,
                   sum({eod_store.row_hash('r.')}) FILTER (WHERE r.Date <= s.Last_Date) AS Old_Digest
            FROM raw_prices r LEFT JOIN symbol_state s ON s.Exchange = r.Exchange AND s.Symbol = r.Symbol
            GROUP BY r.Exchange, r.Symbol)
        SELECT COALESCE(a.Exchange, s.Exchange) AS Exchange, COALESCE(a.Symbol, s.Symbol) AS Symbol,
               a.Rows, a.First_Date, a.Last_Date, a.Digest,
               s.Last_Date AS Prev_Last, s.Inputs_Digest AS Prev_Inputs, s.Pruned_Before,
               CASE WHEN a.Symbol IS NULL THEN 'vanished'
                    WHEN s.Symbol IS NULL THEN 'new'
                    WHEN a.Old_Rows = s.Rows AND a.Old_Digest IS NOT DISTINCT FROM s.Digest
                         THEN CASE WHEN a.Rows = s.Rows THEN 'same' ELSE 'append' END
                    ELSE 'rewritten' END AS Kind
        FROM agg a FULL OUTER JOIN symbol_state s ON s.Exchange = a.Exchange AND s.Symbol = a.Symbol""")
    return _frame(conn, "SELECT Exchange, Symbol, Rows, First_Date, Last_Date, Prev_Last, Prev_Inputs, "
                        "Pruned_Before, Kind FROM symbol_changes")


def _appended(conn) -> pd.DataFrame:
    """Per appended symbol: its new days' range, whether one gaps the way a rule decides, whether one
    comes after a halt (computed against the last stored day)."""
    return _frame(conn, f"""
        WITH s AS (SELECT Exchange, Symbol, Prev_Last FROM symbol_changes WHERE Kind = 'append'),
        g AS (SELECT r.Exchange, r.Symbol, r.Date, r.Open, s.Prev_Last,
                     lag(r.Close) OVER w AS Prev_Close, lag(r.Date) OVER w AS Prev_Date
              FROM raw_prices r JOIN s ON s.Exchange = r.Exchange AND s.Symbol = r.Symbol
              WHERE r.Date >= s.Prev_Last
              WINDOW w AS (PARTITION BY r.Exchange, r.Symbol ORDER BY r.Date))
        SELECT Exchange, Symbol, any_value(Prev_Last) AS Prev_Last, min(Date) AS First_New, max(Date) AS Last_New,
               COALESCE(bool_or(Prev_Close >= {pa.MIN_PRICE} AND Open > 0
                        AND (Open / Prev_Close < {pa.UNSURE_BELOW} OR Open / Prev_Close >= {pa.UNSURE_ABOVE})),
                        false) AS Candidate,
               COALESCE(bool_or(datediff('day', Prev_Date, Date) > {pa.LAG.days}), false) AS Idle
        FROM g WHERE Date > Prev_Last GROUP BY Exchange, Symbol""")


def _gap_candidates(conn, exchange: str, windows: pd.DataFrame) -> set:
    """Symbols of `windows` (Symbol, Since, Until) with a gap a rule decides, or a year-long halt, in the window."""
    if windows.empty:
        return set()
    conn.register("gap_windows", windows[["Symbol", "Since", "Until"]].assign(Since=_days(windows["Since"]),
                                                                          Until=_days(windows["Until"])))
    try:
        rows = conn.execute(f"""
            WITH g AS (SELECT r.Symbol, r.Date, r.Open, lag(r.Close) OVER w AS Prev_Close,
                              lag(r.Date) OVER w AS Prev_Date, u.Since, u.Until
                       FROM raw_prices r JOIN gap_windows u ON u.Symbol = r.Symbol
                       WHERE r.Exchange = ? AND r.Date <= u.Until
                       WINDOW w AS (PARTITION BY r.Symbol ORDER BY r.Date))
            SELECT DISTINCT Symbol FROM g
            WHERE Date >= Since AND ((Prev_Close >= {pa.MIN_PRICE} AND Open > 0
                   AND (Open / Prev_Close < {pa.UNSURE_BELOW} OR Open / Prev_Close >= {pa.UNSURE_ABOVE}))
                   OR datediff('day', Prev_Date, Date) > {pa.NEW_LISTING_AFTER_DAYS})""", [exchange]).fetchall()
    finally:
        conn.unregister("gap_windows")
    return {row[0] for row in rows}


def _retention_hits(conn, inputs: Inputs, state: dict, changes: pd.DataFrame, exchange: str,
                    decided_days: pd.DataFrame) -> set:
    """Symbols whose decisions change because rows aged out (apply_retention marked them Pruned_Before).

    On a shortened history only three things can decide differently; everything else is unchanged or
    reaches the symbol through another rule (a twin's lost factors through `inputs`):
      * the new first day, which has no previous close any more: a decision or flag stored on or before it;
      * a listed action whose day an aged-out row was: an ex-date from LAG before the exchange's old first
        day up to the new first day;
      * BSE: days the NSE twin's aged-out rows covered (no BSE gap rule while the twin trades): a gap
        candidate up to LAG after the later of NSE's first day and the twin's own first day.
    The gap counts do not change: gap_history keeps the aged-out gaps.
    """
    first_x = inputs.ranges[exchange][0]
    if first_x is None:
        return set()
    mine = changes[(changes["Exchange"] == exchange) & (changes["Kind"] != "vanished")]
    first = {s: pd.Timestamp(d) for s, d in zip(mine["Symbol"], mine["First_Date"])}
    pruned = set(mine.loc[mine["Pruned_Before"].notna(), "Symbol"])
    hits = set()
    if pruned:
        stored = decided_days[(decided_days["Exchange"] == exchange) & decided_days["Symbol"].isin(pruned)]
        hits |= {s for s, day in zip(stored["Symbol"], stored["Date"]) if day <= first[s]}
        moving = inputs.moving[exchange]
        if not moving.empty:
            low = pd.Timestamp(_state_date(state, f"first_{exchange}") or first_x) - pa.LAG - ONE_DAY
            listed = moving[moving["Symbol"].isin(pruned)]
            ex_date = _days(listed["Ex_Date"]).to_numpy()
            high = listed["Symbol"].map(first).to_numpy() + ONE_DAY
            hits |= set(listed.loc[(ex_date >= low) & (ex_date <= high), "Symbol"])
    if exchange == "BSE" and inputs.ranges["NSE"][0] is not None:
        nse = changes[(changes["Exchange"] == "NSE") & (changes["Kind"] != "vanished")]
        nse_first = dict(zip(nse["Symbol"], nse["First_Date"]))
        nse_pruned = set(nse.loc[nse["Pruned_Before"].notna(), "Symbol"])
        first_nse = pd.Timestamp(inputs.ranges["NSE"][0])
        low = pd.Timestamp(_state_date(state, "first_NSE") or first_nse) - ONE_DAY
        windows = [(bse, low, max(first_nse, pd.Timestamp(nse_first[twin])) + pa.LAG + ONE_DAY)
                   for bse, twin in inputs.twins.items() if bse in first and twin in nse_pruned]
        hits |= _gap_candidates(conn, "BSE", pd.DataFrame(windows, columns=["Symbol", "Since", "Until"]))
    return hits


def incremental(conn, overrides_path: Path | None, review_path: Path | None, today: date,
                logger: logging.Logger) -> dict:
    """Decide again only the symbols whose decisions can have changed; see the module docstring."""
    started = time.perf_counter()
    state = read_state(conn)
    inputs = read_inputs(conn, overrides_path, today, logger)
    unmatched = _unmatched_overrides(conn, inputs.overrides, logger)
    changes = _classify(conn)
    appended = _appended(conn)
    stored_ledger = _stored_ledger(conn)
    decided_days = _frame(conn, "SELECT Exchange, Symbol, Date FROM price_adjustments")
    affected = _Affected()
    present, vanished = {}, {}
    for exchange in EXCHANGES:
        mine = changes[changes["Exchange"] == exchange]
        present[exchange] = set(mine.loc[mine["Kind"] != "vanished", "Symbol"])
        vanished[exchange] = set(mine.loc[mine["Kind"] == "vanished", "Symbol"])
        affected.add(exchange, mine.loc[mine["Kind"] == "rewritten", "Symbol"], "rewritten")
        affected.add(exchange, mine.loc[mine["Kind"] == "new", "Symbol"], "new")
        _appended_rules(inputs, exchange, appended[appended["Exchange"] == exchange], affected)
        _range_rules(inputs, state, exchange, present[exchange], affected)

    # NSE: inputs digests, retention, then the decisions.
    digests = {"NSE": input_digests(inputs, "NSE", present["NSE"])}
    _digest_rule(changes, digests["NSE"], affected)
    affected.add("NSE", _retention_hits(conn, inputs, state, changes, "NSE", decided_days), "retention")
    nse_set = affected.symbols("NSE") & present["NSE"]
    nse = decide_part(conn, inputs, "NSE", nse_set, None) if nse_set else None
    first_nse = inputs.ranges["NSE"][0]
    kept = stored_ledger[(stored_ledger["Exchange"] == "NSE")
                         & ~stored_ledger["Symbol"].isin(nse_set | vanished["NSE"])]
    if first_nse is not None:
        kept = kept[kept["Date"] >= pd.Timestamp(first_nse)]
    nse_ledger = _concat([kept, nse.ledger if nse else None], LEDGER_COLUMNS)

    # BSE: with the NSE twins' new factors.
    digests["BSE"] = input_digests(inputs, "BSE", present["BSE"], nse_ledger)
    _digest_rule(changes, digests["BSE"], affected)
    _twin_rules(conn, inputs, changes, appended, nse_ledger, present["BSE"], affected)
    affected.add("BSE", _retention_hits(conn, inputs, state, changes, "BSE", decided_days), "retention")
    bse_set = affected.symbols("BSE") & present["BSE"]
    bse = decide_part(conn, inputs, "BSE", bse_set, nse_ledger) if bse_set else None

    parts = [p for p in (nse, bse) if p is not None]
    pair = ["Exchange", "Symbol"]
    decided = pd.DataFrame([(p.exchange, s) for p in parts for s in sorted(p.symbols)], columns=pair)
    new_ledger = _concat([p.ledger for p in parts], LEDGER_COLUMNS)
    retention_touched = _retention_touched(stored_ledger, inputs, vanished)
    only_retention = _without(retention_touched, decided)
    periods_scope = _concat([decided, retention_touched], pair).drop_duplicates()
    periods_ledger = _concat([new_ledger, _ledger_after_retention(stored_ledger, inputs, only_retention)],
                             LEDGER_COLUMNS)
    new_periods = periods_of(periods_ledger)
    old_periods = _frame(conn, "SELECT * FROM adjustment_periods")
    changed_periods = _changed_periods(old_periods, new_periods, periods_scope)
    rewritten = changes[changes["Kind"].isin(["rewritten", "new"])][pair]
    full_scope = _concat([changed_periods, rewritten], pair).drop_duplicates()
    after_scope = _without(appended[pair + ["Prev_Last"]].rename(columns={"Prev_Last": "After"}), full_scope)
    now = datetime.now(pa.MARKET_TZ).replace(tzinfo=None)
    all_digests = _concat(list(digests.values()), ["Exchange", "Symbol", "Inputs_Digest"])

    conn.execute("BEGIN TRANSACTION")
    try:
        _apply(conn, inputs, parts, decided, vanished, stored_ledger, periods_scope, new_periods, full_scope,
               after_scope, changes, now)
        _write_symbol_state(conn, all_digests, source="(SELECT * FROM symbol_changes WHERE Kind <> 'vanished')")
        _write_state(conn, inputs, verified=False, overrides_hash=_overrides_hash(inputs.overrides))
        checked = _concat([full_scope.assign(After=pd.NaT), after_scope], pair + ["After"])
        problems = check_prices(conn, checked)
        continuity = _concat([decided, full_scope], pair).drop_duplicates()
        problems += check_continuity(conn, continuity)
        if problems:
            raise LedgerCheckError(problems)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("DROP TABLE IF EXISTS symbol_changes")
    review_rows = write_review(conn, review_path, today)
    reasons = {exchange: affected.counts(exchange) for exchange in EXCHANGES}
    shown = {exchange: sorted(affected.symbols(exchange))[:12] for exchange in EXCHANGES}
    logger.info(f"Incremental maintenance: decided again NSE {len(nse_set)}, BSE {len(bse_set)} symbols "
                f"{reasons}; adjusted rows rewritten for {len(full_scope)} symbols, appended rows of "
                f"{len(after_scope)}; e.g. {shown}")
    return _summary(conn, "incremental", review_rows, unmatched, started,
                    decided={"NSE": len(nse_set), "BSE": len(bse_set)}, reasons=reasons,
                    rewritten_symbols=len(full_scope), vanished=sum(len(v) for v in vanished.values()))


def _appended_rules(inputs: Inputs, exchange: str, appended: pd.DataFrame, affected: _Affected) -> None:
    if appended.empty:
        return
    affected.add(exchange, appended.loc[appended["Candidate"].astype(bool), "Symbol"], "gap")
    affected.add(exchange, appended.loc[appended["Idle"].astype(bool), "Symbol"], "halt")
    overrides = inputs.overrides
    if not overrides.empty:
        hit = overrides[overrides["Exchange"] == exchange].merge(appended, on="Symbol")
        hit = hit[(hit["Date"] > hit["Prev_Last"]) & (hit["Date"] <= hit["Last_New"])]
        affected.add(exchange, hit["Symbol"], "override_day")
    moving = inputs.moving[exchange]
    if not moving.empty:
        hit = moving.merge(appended, on="Symbol")
        ex_date = _days(hit["Ex_Date"])
        hit = hit[(ex_date >= hit["First_New"] - pa.LAG - ONE_DAY) & (ex_date <= hit["Last_New"])]
        affected.add(exchange, hit["Symbol"], "listing_day")


def _range_rules(inputs: Inputs, state: dict, exchange: str, present: set, affected: _Affected) -> None:
    """listing_due and range_back: what moves with the exchange's stored range and with today."""
    first, last = inputs.ranges[exchange]
    prev_first, prev_last = _state_date(state, f"first_{exchange}"), _state_date(state, f"last_{exchange}")
    prev_today = _state_date(state, "today")
    if first is None:
        return
    if prev_last is not None and last < prev_last:
        affected.add(exchange, present, "range_back")
    moving = inputs.moving[exchange]
    if moving.empty:
        return
    ex_date = _days(moving["Ex_Date"])
    due = ex_date + pa.LAG
    hit = pd.Series(False, index=moving.index)
    if prev_last is None:
        hit |= True
    else:
        hit |= (due >= pd.Timestamp(prev_last) - ONE_DAY) & (due <= pd.Timestamp(last))
    if prev_today is not None:
        hit |= (due >= pd.Timestamp(prev_today) - ONE_DAY) & (due <= pd.Timestamp(inputs.today))
    if prev_first != first:
        low = pd.Timestamp(min(first, prev_first or first)) - pa.LAG - ONE_DAY
        high = pd.Timestamp(max(first, prev_first or first)) + pa.LAG + ONE_DAY
        hit |= (ex_date >= low) & (ex_date <= high)
    affected.add(exchange, moving.loc[hit, "Symbol"], "listing_due", present)


def _digest_rule(changes: pd.DataFrame, digests: pd.DataFrame, affected: _Affected) -> None:
    if digests.empty:
        return
    merged = digests.merge(changes[["Exchange", "Symbol", "Prev_Inputs"]], on=["Exchange", "Symbol"], how="left")
    differ = merged[merged["Inputs_Digest"] != merged["Prev_Inputs"]]
    for exchange, part in differ.groupby("Exchange"):
        affected.add(exchange, part["Symbol"], "inputs")


def _twin_rules(conn, inputs: Inputs, changes: pd.DataFrame, appended: pd.DataFrame, nse_ledger: pd.DataFrame,
                present: set, affected: _Affected) -> None:
    """twin_pending, twin_covered and twin_rewritten for BSE."""
    twin_of = {bse: nse for bse, nse in inputs.twins.items() if bse in present}
    if not twin_of:
        return
    bse_of: dict = {}
    for bse, nse in twin_of.items():
        bse_of.setdefault(nse, []).append(bse)
    # twin_pending: an NSE factor dated on or after the BSE series' last day has not landed on a BSE day yet.
    bse_appended = appended[appended["Exchange"] == "BSE"]
    if not nse_ledger.empty and not bse_appended.empty:
        latest = _days(nse_ledger["Date"]).groupby(nse_ledger["Symbol"]).max()
        twin_latest = bse_appended["Symbol"].map(twin_of).map(latest)
        pending = twin_latest.notna() & (twin_latest >= bse_appended["Prev_Last"])
        affected.add("BSE", bse_appended.loc[pending, "Symbol"], "twin_pending")
    # twin_rewritten: the twin's raw rows changed other than by appending.
    nse_changes = changes[changes["Exchange"] == "NSE"]
    moved = nse_changes[nse_changes["Kind"].isin(["rewritten", "new", "vanished"])]["Symbol"]
    for twin in moved:
        affected.add("BSE", bse_of.get(twin, []), "twin_rewritten")
    # twin_covered: BSE days more than LAG after the twin's previous day and within LAG after a new one.
    nse_appended = appended[appended["Exchange"] == "NSE"]
    windows = []
    for twin, prev_last, last_new in zip(nse_appended["Symbol"], nse_appended["Prev_Last"], nse_appended["Last_New"]):
        for bse in bse_of.get(twin, []):
            windows.append((bse, prev_last + pa.LAG, last_new + pa.LAG))
    if windows:
        frame = pd.DataFrame(windows, columns=["Symbol", "Low", "High"])
        conn.register("covered_windows", frame)
        try:
            rows = conn.execute("SELECT DISTINCT w.Symbol FROM covered_windows w JOIN raw_prices r "
                                "ON r.Exchange = 'BSE' AND r.Symbol = w.Symbol AND r.Date >= w.Low "
                                "AND r.Date <= w.High").fetchall()
        finally:
            conn.unregister("covered_windows")
        affected.add("BSE", [row[0] for row in rows], "twin_covered")


def _without(frame: pd.DataFrame, other: pd.DataFrame) -> pd.DataFrame:
    """The rows of `frame` whose (Exchange, Symbol) is not in `other`."""
    if frame.empty or other.empty:
        return frame.reset_index(drop=True)
    keys = set(zip(other["Exchange"], other["Symbol"]))
    keep = [(e, s) not in keys for e, s in zip(frame["Exchange"], frame["Symbol"])]
    return frame[keep].reset_index(drop=True)


def _retention_touched(stored_ledger: pd.DataFrame, inputs: Inputs, vanished: dict) -> pd.DataFrame:
    """(Exchange, Symbol) whose ledger loses rows before the exchange's first stored day."""
    rows = []
    for exchange in EXCHANGES:
        first = inputs.ranges[exchange][0]
        if first is None:
            continue
        mine = stored_ledger[(stored_ledger["Exchange"] == exchange) & (stored_ledger["Date"] < pd.Timestamp(first))]
        rows += [(exchange, s) for s in sorted(set(mine["Symbol"]) - vanished[exchange])]
    return pd.DataFrame(rows, columns=["Exchange", "Symbol"])


def _ledger_after_retention(stored_ledger: pd.DataFrame, inputs: Inputs, symbols: pd.DataFrame) -> pd.DataFrame:
    """The stored ledger rows of `symbols` on or after their exchange's first stored day."""
    if symbols.empty:
        return _empty(LEDGER_COLUMNS)
    rows = stored_ledger.merge(symbols, on=["Exchange", "Symbol"])
    first = rows["Exchange"].map(lambda e: pd.Timestamp(inputs.ranges[e][0]))
    return rows[rows["Date"] >= first][LEDGER_COLUMNS]


def _changed_periods(old: pd.DataFrame, new: pd.DataFrame, scope: pd.DataFrame) -> pd.DataFrame:
    """(Exchange, Symbol) of `scope` whose periods differ (an eod_prices rewrite is needed)."""
    if scope.empty:
        return pd.DataFrame(columns=["Exchange", "Symbol"])

    def rows_of(frame):
        frame = frame.merge(scope, on=["Exchange", "Symbol"])
        keyed = {}
        for row in frame.assign(From_Date=_days(frame["From_Date"])).sort_values("From_Date").itertuples():
            keyed.setdefault((row.Exchange, row.Symbol), []).append((row.From_Date, row.Adj))
        return keyed

    before, after = rows_of(old), rows_of(new)
    changed = [key for key in set(before) | set(after) if before.get(key) != after.get(key)]
    return pd.DataFrame(sorted(changed), columns=["Exchange", "Symbol"])


def _apply(conn, inputs, parts, decided, vanished, stored_ledger, periods_scope, new_periods, full_scope,
           after_scope, changes, now) -> None:
    """The writes of one incremental run (inside its transaction)."""
    gone = pd.DataFrame([(e, s) for e in EXCHANGES for s in sorted(vanished[e])], columns=["Exchange", "Symbol"])
    if not gone.empty:
        conn.register("vanished_symbols", gone)
        try:
            eod_store.delete_derived(conn, "vanished_symbols")
        finally:
            conn.unregister("vanished_symbols")
    for exchange in EXCHANGES:
        first = inputs.ranges[exchange][0]
        if first is not None:
            for table in ("price_adjustments", "adjustment_ledger", "eod_prices"):
                conn.execute(f"DELETE FROM {table} WHERE Exchange = ? AND Date < ?", [exchange, first])
    if not decided.empty:
        conn.register("decided_symbols", decided.merge(
            changes[["Exchange", "Symbol", "First_Date"]], on=["Exchange", "Symbol"], how="left"))
        try:
            for table in ("price_adjustments", "adjustment_ledger"):
                conn.execute(f"DELETE FROM {table} t USING decided_symbols k "
                             "WHERE t.Exchange = k.Exchange AND t.Symbol = k.Symbol")
            # Gaps of days still stored are decided again; older ones (aged out) stay.
            conn.execute("DELETE FROM gap_history t USING decided_symbols k WHERE t.Exchange = k.Exchange "
                         "AND t.Symbol = k.Symbol AND t.Date > k.First_Date")
        finally:
            conn.unregister("decided_symbols")
        _insert(conn, "price_adjustments", _concat([p.events for p in parts], pa.EVENT_COLUMNS), pa.EVENT_COLUMNS)
        ledger = _concat([p.ledger for p in parts], LEDGER_COLUMNS)
        mine = stored_ledger.merge(decided, on=["Exchange", "Symbol"])
        _insert(conn, "adjustment_ledger", _with_decided_at(ledger, mine, now),
                LEDGER_COLUMNS + ["Decided_At", "Rule_Version"])
        _insert(conn, "gap_history", _concat([p.gaps for p in parts], GAP_COLUMNS), GAP_COLUMNS)
    if not periods_scope.empty:
        conn.register("period_symbols", periods_scope)
        try:
            conn.execute("DELETE FROM adjustment_periods t USING period_symbols k "
                         "WHERE t.Exchange = k.Exchange AND t.Symbol = k.Symbol")
        finally:
            conn.unregister("period_symbols")
        _insert(conn, "adjustment_periods", new_periods, PERIOD_COLUMNS)
    if not full_scope.empty:
        conn.register("rewrite_symbols", full_scope)
        try:
            eod_store.rematerialize(conn, "rewrite_symbols")
        finally:
            conn.unregister("rewrite_symbols")
    if not after_scope.empty:
        conn.register("append_rows", after_scope.assign(After=_days(after_scope["After"])))
        try:
            eod_store.rematerialize(conn, "append_rows", ("Exchange", "Symbol", "After"))
        finally:
            conn.unregister("append_rows")


# --- the entry points --------------------------------------------------------------------------------

def _full_reason(state: dict) -> str | None:
    if not state.get("rule_version"):
        return "the store was never maintained as version 3"
    if state.get("rule_version") != str(RULE_VERSION):
        return f"decision rules changed (version {state.get('rule_version')} -> {RULE_VERSION})"
    if state.get("duckdb_version") != duckdb.__version__:
        return f"DuckDB changed ({state.get('duckdb_version')} -> {duckdb.__version__}): its row hashes may differ"
    return None


def verify_due(state: dict, today: date) -> bool:
    last = _state_date(state, "last_full_verify")
    return last is None or (today - last).days >= VERIFY_EVERY_DAYS


def maintain(conn, overrides_path: Path | None, review_path: Path | None, today: date | None = None,
             logger: logging.Logger | None = None) -> dict:
    """The daily decision step: incremental on a version 3 store (full when needed), the full verify when it is
    due; the full rebuild on a version 2 store."""
    logger = logger or module_logger
    today = today or pa.market_today()
    if eod_store.schema_version(conn) != 3:
        return rebuild(conn, overrides_path, review_path, today, logger)
    reason = _full_reason(read_state(conn))
    if reason:
        logger.info(f"Full rebuild of the adjusted prices: {reason}.")
        summary = rebuild(conn, overrides_path, review_path, today, logger)
        summary["mode"] = f"full ({reason})"
    else:
        try:
            summary = incremental(conn, overrides_path, review_path, today, logger)
        except LedgerCheckError as error:
            logger.error(f"STORED-PRICE CHECK FAILED; the maintenance was rolled back and the full rebuild runs "
                         f"instead: {error}")
            summary = rebuild(conn, overrides_path, review_path, today, logger)
            summary["mode"] = "full (an incremental check failed)"
            summary["check_failed"] = [list(problem) for problem in error.problems[:50]]
            extra = pd.DataFrame([{"Exchange": e, "Symbol": s, "Date": today, "Flag": f"stored-price check: {w}"}
                                  for e, s, w in error.problems])
            summary["review_rows"] = write_review(conn, review_path, today, extra)
        except Exception as error:                   # an incremental run that cannot finish: decide everything
            logger.error(f"INCREMENTAL MAINTENANCE FAILED ({type(error).__name__}: {error}); it was rolled back "
                         "and the full rebuild runs instead.")
            summary = rebuild(conn, overrides_path, review_path, today, logger)
            summary["mode"] = f"full (the incremental run failed: {type(error).__name__})"
    if verify_due(read_state(conn), today):
        summary["verify"] = verify(conn, overrides_path, review_path, today, logger)
    return summary


def verify(conn, overrides_path: Path | None, review_path: Path | None, today: date | None = None,
           logger: logging.Logger | None = None, repair: bool = True) -> dict:
    """Compare every derived table with the full rebuild; on a difference log it and (repair) replace them.

    Returns {"status": "ok" | "repaired" | "differs", "differences": {table: [[exchange, symbol], ...]}}.
    """
    logger = logger or module_logger
    today = today or pa.market_today()
    started = time.perf_counter()
    version = eod_store.schema_version(conn)
    if version not in (2, 3):
        raise RuntimeError(f"the price store is version {version}; nothing to verify")
    inputs = read_inputs(conn, overrides_path, today, logger)
    decisions = decide_all(conn, inputs, history=version == 3)
    if version == 3:
        found = differences(conn, decisions)
        independent = check_prices(conn) + check_continuity(conn)
        if independent:
            found["checks"] = sorted({(e, s) for e, s, _ in independent})
    else:
        found = {table: pairs for table, pairs in differences_v2(conn, decisions).items()}
    if not found:
        if version == 3:
            conn.execute("BEGIN TRANSACTION")
            conn.execute("INSERT OR REPLACE INTO maintenance_state VALUES ('last_full_verify', ?)",
                         [today.isoformat()])
            conn.execute("COMMIT")
        logger.info(f"Full verify: the stored prices and decisions equal the full rebuild "
                    f"({time.perf_counter() - started:.1f}s).")
        return {"status": "ok", "differences": {}, "seconds": round(time.perf_counter() - started, 2)}
    for table, pairs in found.items():
        logger.error(f"FULL VERIFY: {table} differs from the full rebuild for {len(pairs)} symbol(s): "
                     f"{[f'{e} {s}' for e, s in pairs[:20]]}")
    result = {"status": "differs", "differences": {t: [list(p) for p in pairs] for t, pairs in found.items()}}
    if repair:
        pa.check_factors(decisions.events, decisions.periods)
        conn.execute("BEGIN TRANSACTION")
        try:
            if version == 3:
                write_all(conn, decisions, datetime.now(pa.MARKET_TZ).replace(tzinfo=None))
                problems = _full_checks(conn, decisions)
                if problems:
                    raise LedgerCheckError(problems)
            else:
                conn.execute("DELETE FROM adjustment_periods")
                _insert(conn, "adjustment_periods", decisions.periods, PERIOD_COLUMNS)
                conn.execute("DELETE FROM price_adjustments")
                _insert(conn, "price_adjustments", decisions.events, pa.EVENT_COLUMNS)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        write_review(conn, review_path, today)
        logger.warning("Full verify: the derived tables were replaced by the full rebuild's.")
        result["status"] = "repaired"
    result["seconds"] = round(time.perf_counter() - started, 2)
    return result


def differences_v2(conn, decisions: Decisions) -> dict:
    """The version 2 tables (price_adjustments, adjustment_periods) against the oracle's."""
    found = {}
    casts = {"Date": "CAST(Date AS DATE) AS Date", "From_Date": "CAST(From_Date AS DATE) AS From_Date"}
    for table, frame, columns in (("price_adjustments", decisions.events, pa.EVENT_COLUMNS),
                                  ("adjustment_periods", decisions.periods, PERIOD_COLUMNS)):
        listed = ", ".join(casts.get(column, column) for column in columns)
        conn.register("oracle_rows", frame[columns] if not frame.empty else _empty(columns))
        try:
            pairs = conn.execute(f"""
                SELECT DISTINCT Exchange, Symbol FROM (
                    (SELECT {listed} FROM {table} EXCEPT ALL SELECT {listed} FROM oracle_rows)
                    UNION ALL (SELECT {listed} FROM oracle_rows EXCEPT ALL SELECT {listed} FROM {table}))
                ORDER BY 1, 2""").fetchall()
        finally:
            conn.unregister("oracle_rows")
        if pairs:
            found[table] = [tuple(pair) for pair in pairs]
    return found


def migrate(path: Path, overrides_path: Path | None, review_path: Path | None, today: date | None = None,
            logger: logging.Logger | None = None, backup: bool = True) -> bool:
    """Turn a version 2 store into version 3 in one transaction; False (and the store still version 2) on failure.

    First the file is copied to <stem>.v2-backup.duckdb beside it (unless `backup` is False), when the disk
    has room for the copy and the new tables. raw_prices is never written. Inside the transaction the
    tables are created, everything is decided (the full rebuild), eod_prices is filled, and the full
    verify runs; any failure rolls it all back.
    """
    logger = logger or module_logger
    today = today or pa.market_today()
    path = Path(path)
    conn = duckdb.connect(str(path))
    try:
        version = eod_store.schema_version(conn)
    finally:
        conn.close()                                      # a closed file has no pending WAL to miss in the copy
    if version != 2:
        return version == 3
    size = path.stat().st_size
    need = (size if backup else 0) + int(size * 1.5) + MIGRATION_HEADROOM
    free = shutil.disk_usage(path.parent).free
    if free < need:
        logger.error(f"PRICE STORE NOT MIGRATED TO VERSION 3: {free / 1e6:.0f} MB free on the disk, about "
                     f"{need / 1e6:.0f} MB needed (backup and new tables). It keeps working as version 2; "
                     "free some space and the next run migrates it.")
        return False
    started = time.perf_counter()
    if backup:
        target = path.with_name(f"{path.stem}.v2-backup{path.suffix}")
        shutil.copy2(path, target)
        logger.info(f"Version 2 price store kept as {target.name} before the migration "
                    f"({time.perf_counter() - started:.1f}s).")
    conn = duckdb.connect(str(path))
    raw_sql = ("SELECT count(*), sum(hash(Date, Symbol, Exchange, Open, High, Low, Close, Volume, Total_Trades, "
               "Dlv_Qty)) FROM raw_prices")
    try:
        raw_before = conn.execute(raw_sql).fetchone()
        old_periods = _frame(conn, "SELECT * FROM adjustment_periods")
        conn.execute("BEGIN TRANSACTION")
        try:
            eod_store.create_schema(conn, 3)
            inputs = read_inputs(conn, overrides_path, today, logger)
            decisions = decide_all(conn, inputs, history=True)
            pa.check_factors(decisions.events, decisions.periods)
            write_all(conn, decisions, datetime.now(pa.MARKET_TZ).replace(tzinfo=None))
            problems = _full_checks(conn, decisions)
            if conn.execute(raw_sql).fetchone() != raw_before:
                problems.append(("-", "-", "raw_prices changed during the migration"))
            if problems:
                raise LedgerCheckError(problems)
            conn.execute("COMMIT")
        except Exception as error:
            conn.execute("ROLLBACK")
            logger.error(f"PRICE STORE MIGRATION TO VERSION 3 FAILED AND WAS ROLLED BACK ({error}); the store "
                         "keeps working as version 2 and the next run tries again.")
            return False
        changed = _changed_periods(old_periods, decisions.periods,
                                   _concat([old_periods, decisions.periods], ["Exchange", "Symbol"]).drop_duplicates())
        if not changed.empty:
            logger.info(f"Migration: {len(changed)} symbol(s) decided differently from the version 2 store's last "
                        f"run (lists or rules changed since): {changed.head(20).values.tolist()}")
        write_review(conn, review_path, today)
        logger.info(f"Price store migrated to version 3 in {time.perf_counter() - started:.1f}s and verified.")
        return True
    finally:
        conn.close()
