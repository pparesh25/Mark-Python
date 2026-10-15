"""Stored adjustments with a ledger (store version 3): the incremental maintenance against the full rebuild.

The simulation drives two stores with the same day-by-day feed -- prices, list answers, overrides,
retention -- and decides one incrementally (adjustment_ledger.maintain) and the other with the full
rebuild (price_adjustments.rebuild, the oracle) after every simulated day, then compares every
derived table row for row.
"""

from __future__ import annotations

import logging
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd
import pytest

import action_lists
import adjustment_ledger as al
import eod_store
import price_adjustments as pa
from eod_store import apply_retention, create_schema, schema_version, upsert_daily_prices

LOG = logging.getLogger("ledger-test")
START = date(2024, 1, 1)
STEP_DAYS = 5                         # one session every 5 calendar days: 110 sessions span 545 days
YEARS = 1.1                           # retention: 402 days, so rows age out from session 81 on


def day_of(session: int) -> date:
    return START + timedelta(days=STEP_DAYS * session)


# --- the simulated market -----------------------------------------------------------------------------

@dataclass
class Series:
    """One exchange's series of one company: which sessions it trades, its name over time, its jumps."""
    exchange: str
    symbol: str
    isin: str
    company: str
    code: str | None = None
    sessions: set = field(default_factory=set)
    open_shock: dict = field(default_factory=dict)      # session -> multiplier on that day's open only
    renames: dict = field(default_factory=dict)          # session -> the symbol from that session on
    isins: dict = field(default_factory=dict)            # session -> the ISIN from that session on

    def name(self, session: int) -> str:
        return next((n for s, n in sorted(self.renames.items(), reverse=True) if session >= s), self.symbol)

    def isin_at(self, session: int) -> str:
        return next((i for s, i in sorted(self.isins.items(), reverse=True) if session >= s), self.isin)


class Market:
    """Companies with a value path and corporate actions; series quoted on NSE and BSE."""

    def __init__(self, sessions: int, seed: int):
        self.n = sessions
        self.rng = np.random.default_rng(seed)
        self.values: dict = {}
        self.jumps: dict = {}            # company -> {session: factor}
        self.series: list[Series] = []
        self.lists: list[dict] = []      # published list rows
        self.withheld: dict = {}         # (exchange, symbol at session, session) -> delivery session
        self.replays: list = []          # (delivery session, Series, session, open multiplier)
        self.overrides: dict = {}        # session -> overrides file text from that session on
        self.years = YEARS               # retention

    def company(self, name: str, base: float, jumps: dict | None = None) -> str:
        steps = self.rng.normal(0, 0.01, self.n)
        self.values[name] = base * np.exp(np.cumsum(steps))
        self.jumps[name] = jumps or {}
        return name

    def quote(self, exchange, symbol, isin, company, sessions=None, code=None, **extra) -> Series:
        days = set(range(self.n)) if sessions is None else set(sessions)
        series = Series(exchange, symbol, isin, company, code, days, **extra)
        self.series.append(series)
        return series

    def factor(self, company: str, session: int) -> float:
        return float(np.prod([f for s, f in self.jumps[company].items() if s <= session]))

    def row(self, series: Series, session: int, open_multiplier: float = 1.0) -> dict:
        value = self.values[series.company]
        bse = 1.001 if series.exchange == "BSE" else 1.0
        close = value[session] * self.factor(series.company, session) * bse
        previous = value[session - 1] if session else value[0]
        opening = previous * self.factor(series.company, session) * bse * (1 + 0.002 * ((session * 7) % 5 - 2))
        opening *= series.open_shock.get(session, 1.0) * open_multiplier
        high, low = max(opening, close) * 1.01, min(opening, close) * 0.99
        return {"Date": day_of(session), "Symbol": series.name(session), "Exchange": series.exchange,
                "ISIN": series.isin_at(session), "Series": "EQ" if series.exchange == "NSE" else "A",
                "Code": series.code, "Open": round(opening, 2), "High": round(high, 2), "Low": round(low, 2),
                "Close": round(close, 2), "Volume": 1000 + session, "Total_Trades": 10, "Dlv_Qty": 400}

    def deliveries(self, session: int) -> list[pd.DataFrame]:
        """The upsert batches arriving at `session`: one per exchange and day, in day order."""
        if not hasattr(self, "_arrivals"):
            self._arrivals: dict = {}
            for series in self.series:
                for traded in sorted(s for s in series.sessions if s < self.n):
                    arrives = self.withheld.get((series.exchange, series.symbol, traded), traded)
                    self._arrivals.setdefault(arrives, []).append(self.row(series, traded))
            for arrives, series, traded, multiplier in self.replays:
                self._arrivals.setdefault(arrives, []).append(self.row(series, traded, multiplier))
        rows = self._arrivals.get(session, [])
        if not rows:
            return []
        frame = pd.DataFrame(rows)
        return [part for _, part in frame.groupby(["Date", "Exchange"], sort=True)]

    def list_rows(self, exchange: str, session: int, start: date, end: date) -> pd.DataFrame:
        published = [r for r in self.lists if r["exchange"] == exchange and r["published"] <= session
                     < r.get("withdrawn", 10 ** 6) and start <= day_of(r["ex"]) <= end]
        if exchange == "NSE":
            return action_lists.nse_rows([{"symbol": r["symbol"], "series": "EQ", "isin": r["isin"], "comp": "Co",
                                           "exDate": day_of(r["ex"]).strftime("%d-%b-%Y"), "subject": r["purpose"]}
                                          for r in published], "equities")
        text = "Security Code,Security Name,Company Name,Ex Date,Purpose,Record Date\n" + "".join(
            f"{r['code']},{r['symbol']},Co,{day_of(r['ex']):%d %b %Y},{r['purpose']},{day_of(r['ex']):%d %b %Y}\n"
            for r in published)
        return action_lists.bse_rows(text)

    def overrides_text(self, session: int) -> str:
        current = [text for s, text in sorted(self.overrides.items()) if s <= session]
        return current[-1] if current else "Exchange,Symbol,Date,Factor,Note\n"


def scripted_market() -> Market:
    """Every case the dependency rules exist for, on a handful of companies (see each comment)."""
    m = Market(110, seed=7)
    q = m.quote
    plain = [m.company(f"PLAIN{i}", 100 + 10 * i) for i in range(3)]
    for i, name in enumerate(plain):                    # ordinary twins, nothing happens
        q("NSE", name, f"INE10{i}A01011", name)
        q("BSE", name, f"INE10{i}A01011", name, code=f"50010{i}")
    # A listed split NSE lists ahead of its ex-date; the BSE twin takes NSE's factor.
    m.company("SPLIT", 800, {30: 0.5})
    q("NSE", "SPLIT", "INE200A01011", "SPLIT")
    q("BSE", "SPLIT", "INE200A01011", "SPLIT", code="500200")
    m.lists.append(dict(exchange="NSE", symbol="SPLIT", isin="INE200A01011", ex=30, published=25,
                        purpose="Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share"))
    # A 1:10 bonus too small for the open to show, with its ex-date revised (the old row replaced).
    m.company("SMALLBON", 300, {34: 10 / 11})
    q("NSE", "SMALLBON", "INE201A01011", "SMALLBON")
    m.lists.append(dict(exchange="NSE", symbol="SMALLBON", isin="INE201A01011", ex=33, published=28, withdrawn=31,
                        purpose="Bonus 1:10"))
    m.lists.append(dict(exchange="NSE", symbol="SMALLBON", isin="INE201A01011", ex=34, published=31,
                        purpose="Bonus 1:10"))
    # A consolidation listed ten sessions after its ex-date (the list arrives late).
    m.company("CONSOL", 20, {50: 10.0})
    q("NSE", "CONSOL", "INE202A01011", "CONSOL")
    m.lists.append(dict(exchange="NSE", symbol="CONSOL", isin="INE202A01011", ex=50, published=60,
                        purpose="Consolidation of shares From Rs 1/- Per Share To Rs 10/- Per Share"))
    # A demerger the price decides.
    m.company("DEMERGE", 500, {55: 0.7})
    q("NSE", "DEMERGE", "INE203A01011", "DEMERGE")
    m.lists.append(dict(exchange="NSE", symbol="DEMERGE", isin="INE203A01011", ex=55, published=50,
                        purpose="Demerger"))
    # An unlisted halving: the gap rule.
    m.company("GAPONLY", 400, {35: 0.5})
    q("NSE", "GAPONLY", "INE204A01011", "GAPONLY")
    # Noisy: more than eight sure gaps (halvings and doublings), which age out of the store later.
    m.company("NOISY", 200, {s: (0.5 if (s // 2) % 2 == 0 else 2.0) for s in range(6, 44, 2)})
    q("NSE", "NOISY", "INE205A01011", "NOISY")
    # Halted across a listed split (no trade within 10 days of the ex-date), back at session 67.
    m.company("HALTCO", 600, {62: 0.5})
    q("NSE", "HALTCO", "INE206A01011", "HALTCO", sessions=set(range(140)) - set(range(60, 67)))
    m.lists.append(dict(exchange="NSE", symbol="HALTCO", isin="INE206A01011", ex=62, published=55,
                        purpose="Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share"))
    # Renamed at session 70, with a split before: its ledger row follows the new name.
    m.company("OLDNAME", 900, {20: 0.5})
    q("NSE", "OLDNAME", "INE207A01011", "OLDNAME", renames={70: "NEWNAME"})
    m.lists.append(dict(exchange="NSE", symbol="OLDNAME", isin="INE207A01011", ex=20, published=15,
                        purpose="Face Value Split (Sub-Division) - From Rs 2/- Per Share To Rs 1/- Per Share"))
    # An unresolved 0.7 gap an override decides from session 45 until it is removed at 80, and an
    # override written at 85 for a day that has not come yet (session 90, whose open shows nothing).
    m.company("OVERRIDE", 300, {42: 0.7})
    q("NSE", "OVERRIDE", "INE208A01011", "OVERRIDE")
    m.overrides[45] = f"Exchange,Symbol,Date,Factor,Note\nNSE,OVERRIDE,{day_of(42)},0.7,hand\n"
    m.overrides[80] = "Exchange,Symbol,Date,Factor,Note\n"
    m.overrides[85] = f"Exchange,Symbol,Date,Factor,Note\nNSE,OVERRIDE,{day_of(90)},0.8,hand\n"
    # A replayed day with other prices: session 50 sent again at 52 with half the open.
    replay = q("NSE", "REPLAY", "INE209A01011", m.company("REPLAY", 250))
    m.replays.append((52, replay, 50, 0.5))
    # A backfilled day: session 75 (the split's day) arrives at 80; until then the gap is on 76.
    m.company("BACKFILL", 700, {75: 0.5})
    q("NSE", "BACKFILL", "INE210A01011", "BACKFILL")
    m.withheld[("NSE", "BACKFILL", 75)] = 80
    # NSE twin halted across a 1:1 bonus; BSE traded the ex-date, where its list and open agree.
    m.company("TWINHALT", 120, {60: 0.5})
    q("NSE", "TWINHALT", "INE211A01011", "TWINHALT", sessions=set(range(140)) - set(range(58, 63)))
    q("BSE", "TWINHALT", "INE211A01011", "TWINHALT", code="500211")
    m.lists.append(dict(exchange="BSE", symbol="TWINHALT", code="500211", ex=60, published=55,
                        purpose="Bonus issue 1:1"))
    # BSE halted 70-80 across NSE's split at 72: the twin factor waits for BSE's next day.
    m.company("BSELATE", 640, {72: 0.5})
    q("NSE", "BSELATE", "INE212A01011", "BSELATE")
    q("BSE", "BSELATE", "INE212A01011", "BSELATE", sessions=set(range(140)) - set(range(70, 81)), code="500212")
    # A BSE series that starts after NSE's split has nothing to scale.
    m.company("NEWLIST", 480, {30: 0.5})
    q("NSE", "NEWLIST", "INE213A01011", "NEWLIST")
    q("BSE", "NEWLIST", "INE213A01011", "NEWLIST", sessions=range(35, 140), code="500213")
    # BSE only: its own 2:1 bonus by scrip code.
    m.company("ONLYBSE", 90, {65: 1 / 3})
    q("BSE", "ONLYBSE", "INE900A01011", "ONLYBSE", code="590001")
    m.lists.append(dict(exchange="BSE", symbol="ONLYBSE", code="590001", ex=65, published=60,
                        purpose="Bonus issue 2:1"))
    # A BSE ticker handed to another issuer at session 90: the old history is discarded.
    m.company("REUSED", 150, {40: 0.5})
    m.company("REUSED2", 33)
    q("BSE", "REUSED", "INE901A01011", "REUSED", sessions=range(0, 85), code="590002")
    q("BSE", "REUSED", "INE777Z01011", "REUSED2", sessions=range(90, 140), code="590003")
    # A BSE-only company that lists on NSE at session 95: its BSE series gains a twin.
    m.company("LATENSE", 260, {104: 0.5})
    q("BSE", "LATENSE", "INE902A01011", "LATENSE", code="590004")
    q("NSE", "LATENSE", "INE902A01011", "LATENSE", sessions=range(95, 110))
    # The NSE twin's days 100-104 arrive late (at 106): while they are missing a thin BSE 0.45 open
    # is a gap the BSE rule takes; once they are in, NSE covers those days and it is not.
    m.company("PAUSE", 180)
    q("NSE", "PAUSE", "INE214A01011", "PAUSE")
    q("BSE", "PAUSE", "INE214A01011", "PAUSE", code="500214", open_shock={103: 0.45})
    for s in range(100, 106):
        m.withheld[("NSE", "PAUSE", s)] = 106
    # Back after more than 366 days without a trade: a new listing; its old days age out later.
    m.company("RELIST", 96)
    q("BSE", "RELIST", "INE903A01011", "RELIST", sessions=list(range(0, 9)) + list(range(83, 110)), code="590005",
      open_shock={83: 0.03})
    # The same without a big gap on either return (only the halt rule sees them): a 1:10 bonus listed
    # while halted (flagged "no trade within 10 days" when it trades again), and a relisting at the old price.
    m.company("HALT2", 300, {42: 10 / 11})
    q("NSE", "HALT2", "INE217A01011", "HALT2", sessions=set(range(140)) - set(range(40, 48)))
    m.lists.append(dict(exchange="NSE", symbol="HALT2", isin="INE217A01011", ex=42, published=35,
                        purpose="Bonus 1:10"))
    m.company("RELIST2", 140)
    q("BSE", "RELIST2", "INE904A01011", "RELIST2", sessions=list(range(0, 6)) + list(range(80, 110)), code="590006")
    # NSE halted on 24-26 while BSE trades: BSE's 0.45 open on 25 is covered by NSE's day 23 -- until that
    # day ages out (session 104), when the BSE gap rule takes it.
    m.company("COVCUT", 220)
    q("NSE", "COVCUT", "INE218A01011", "COVCUT", sessions=set(range(140)) - {24, 25, 26})
    q("BSE", "COVCUT", "INE218A01011", "COVCUT", code="500218", open_shock={25: 0.45})
    # NSE's 1:10 bonus on 50, a day BSE did not trade: the twin factor waits for BSE's next day (no gap there).
    m.company("TWINGAP", 410, {50: 10 / 11})
    q("NSE", "TWINGAP", "INE219A01011", "TWINGAP")
    q("BSE", "TWINGAP", "INE219A01011", "TWINGAP", sessions=set(range(140)) - {50}, code="500219")
    m.lists.append(dict(exchange="NSE", symbol="TWINGAP", isin="INE219A01011", ex=50, published=45,
                        purpose="Bonus 1:10"))
    # NSE's days 59-62 arrive late (at 65, after 63 and 64): until then BSE's 0.45 open on 61 is uncovered.
    m.company("COVBACK", 160)
    q("NSE", "COVBACK", "INE220A01011", "COVBACK")
    q("BSE", "COVBACK", "INE220A01011", "COVBACK", code="500220", open_shock={61: 0.45})
    for s in (59, 60, 61, 62):
        m.withheld[("NSE", "COVBACK", s)] = 65
    # Never trades again after a listed split: nothing to flag.
    m.company("GONE", 80, {25: 0.5})
    q("NSE", "GONE", "INE215A01011", "GONE", sessions=range(0, 20))
    m.lists.append(dict(exchange="NSE", symbol="GONE", isin="INE215A01011", ex=25, published=18,
                        purpose="Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share"))
    # A split that ages out of the store (from session 83): the first remaining day loses its gap.
    m.company("EARLY", 1000, {3: 0.5})
    q("NSE", "EARLY", "INE216A01011", "EARLY")
    q("BSE", "EARLY", "INE216A01011", "EARLY", code="500216")
    m.lists.append(dict(exchange="NSE", symbol="EARLY", isin="INE216A01011", ex=3, published=0,
                        purpose="Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share"))
    return m


def random_market(seed: int, sessions: int = 45) -> Market:
    """Random companies with random actions, lists (some late, some revised), halts, late days, replays,
    overrides and retention."""
    m = Market(sessions, seed=seed)
    m.years = 0.35                       # 128 days: rows age out from session 26 on
    rng = np.random.default_rng(seed + 1000)
    ratios = {"Bonus 1:1": 0.5, "Bonus 1:10": 10 / 11, "Bonus issue 2:1": 1 / 3,
              "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share": 0.2}
    for i in range(10):
        name = f"R{seed}X{i}"
        jumps = {}
        lists = []
        for _ in range(rng.integers(0, 3)):
            session = int(rng.integers(3, sessions - 3))
            purpose = list(ratios)[int(rng.integers(0, len(ratios)))]
            jumps[session] = ratios[purpose] if rng.random() < 0.8 else float(rng.choice([0.5, 0.7, 2.0]))
            if rng.random() < 0.7:
                lists.append((session, purpose))
        m.company(name, float(rng.uniform(8, 900)), jumps)
        isin = f"INE{seed % 10}{i:02d}B01011"
        halt = set()
        if rng.random() < 0.3:
            start = int(rng.integers(5, sessions - 10))
            halt = set(range(start, start + int(rng.integers(2, 8))))
        on_nse, on_bse = rng.random() < 0.8, rng.random() < 0.7 or True
        if on_nse:
            q = m.quote("NSE", name, isin, name, sessions=set(range(sessions)) - halt)
            if rng.random() < 0.2:                                  # a late day
                late = int(rng.integers(2, sessions - 4))
                m.withheld[("NSE", name, late)] = late + int(rng.integers(1, 4))
            if rng.random() < 0.15:
                m.replays.append((int(rng.integers(5, sessions)), q, int(rng.integers(1, 5)), 0.98))
        if on_bse:
            bse_halt = set(range(int(rng.integers(5, sessions - 10)), sessions)) if rng.random() < 0.1 else set()
            m.quote("BSE", name, isin, name, sessions=set(range(sessions)) - bse_halt - (halt if not on_nse else set()),
                    code=f"5{seed % 10}{i:04d}")
        for session, purpose in lists:
            exchange = "NSE" if on_nse and rng.random() < 0.7 else "BSE"
            published = max(0, session - int(rng.integers(-8, 10)))
            row = dict(exchange=exchange, symbol=name, isin=isin, code=f"5{seed % 10}{i:04d}", ex=session,
                       published=published, purpose=purpose)
            if rng.random() < 0.2:                                  # revised a day later
                m.lists.append({**row, "withdrawn": published + 2})
                row = {**row, "ex": min(session + 1, sessions - 1), "published": published + 2}
            m.lists.append(row)
        if rng.random() < 0.15:
            m.overrides[int(rng.integers(5, sessions))] = (
                f"Exchange,Symbol,Date,Factor,Note\nNSE,{name},{day_of(int(rng.integers(2, sessions)))},0.5,r\n")
    return m


# --- the two stores --------------------------------------------------------------------------------

COMPARED = {
    "raw_prices": "SELECT * FROM raw_prices",
    "securities": "SELECT * FROM securities",
    "price_adjustments": "SELECT * FROM price_adjustments",
    "adjustment_ledger": "SELECT * EXCLUDE (Decided_At) FROM adjustment_ledger",
    "adjustment_periods": "SELECT * FROM adjustment_periods",
    "gap_history": "SELECT * FROM gap_history",
    "symbol_state": "SELECT * REPLACE (CAST(Digest AS VARCHAR) AS Digest) FROM symbol_state",
    "eod_prices": "SELECT * FROM eod_prices",
    "eod_data": "SELECT * FROM eod_data",
}


def snapshot(conn) -> dict:
    return {name: conn.execute(f"SELECT * FROM ({sql}) ORDER BY ALL").fetchall() for name, sql in COMPARED.items()}


def assert_same(incremental: dict, oracle: dict, session: int) -> None:
    for name in COMPARED:
        left, right = incremental[name], oracle[name]
        if left != right:
            only_i = [row for row in left if row not in set(right)][:5]
            only_o = [row for row in right if row not in set(left)][:5]
            raise AssertionError(f"session {session}: {name} differs from the full rebuild\n"
                                 f"  incremental only: {only_i}\n  full rebuild only: {only_o}")


def feed(conn, market: Market, session: int, overrides_path) -> None:
    """One day's input, identical for both stores: upserts, retention, the list answers (window, and the
    whole history every 30 sessions), the overrides file."""
    for batch in market.deliveries(session):
        conn.execute("BEGIN TRANSACTION")
        upsert_daily_prices(conn, batch)
        conn.execute("COMMIT")
    apply_retention(conn, market.years)
    today = day_of(session)
    start, end = action_lists.window(today)
    if session % 30 == 29:
        start = START - timedelta(days=10)
    for exchange, segment in (("NSE", "equities"), ("BSE", "E")):
        rows = market.list_rows(exchange, session, start, end)
        if not rows.empty:
            action_lists.store(conn, rows, [(exchange, segment, a, b) for a, b in action_lists.year_chunks(start, end)])
    overrides_path.write_text(market.overrides_text(session), encoding="utf-8")


def simulate(tmp_path, market: Market, monkeypatch, verify_every: int = 20, probe=None):
    """Run both stores day by day; returns (the incremental runs' reasons counted, both connections)."""
    monkeypatch.setattr(al, "verify_due", lambda state, today: False)    # nothing may repair the store
    incremental = duckdb.connect(str(tmp_path / "incremental.duckdb"))
    oracle = duckdb.connect(str(tmp_path / "oracle.duckdb"))
    overrides = tmp_path / "overrides.csv"
    for conn in (incremental, oracle):
        create_schema(conn)
    reasons = Counter()
    try:
        for session in range(market.n):
            today = day_of(session)
            feed(incremental, market, session, overrides)
            summary = al.maintain(incremental, overrides, None, today, LOG)
            feed(oracle, market, session, overrides)
            pa.rebuild(oracle, overrides, None, today, LOG)
            if session:
                assert summary["mode"] == "incremental", (session, summary["mode"])
                for exchange_reasons in summary["reasons"].values():
                    reasons.update(exchange_reasons.keys())
            assert_same(snapshot(incremental), snapshot(oracle), session)
            if probe is not None:
                probe(session, oracle)
            if session % verify_every == verify_every - 1:
                checked = al.verify(incremental, overrides, None, today, LOG, repair=False)
                assert checked["status"] == "ok", (session, checked["differences"])
        return reasons, incremental, oracle
    except Exception:
        incremental.close()
        oracle.close()
        raise


SEEN_SQL = """
    SELECT Exchange || ' ' || Symbol || ' ' || COALESCE(Event, '-') || ' ' || COALESCE(Flag, '-')
    FROM price_adjustments WHERE Symbol IN ('COVCUT', 'RELIST', 'RELIST2', 'HALT2', 'PAUSE', 'EARLY', 'NOISY',
                                            'COVBACK', 'TWINGAP', 'OVERRIDE')
"""


def test_daily_incremental_maintenance_equals_the_full_rebuild_after_every_day(tmp_path, monkeypatch):
    market = scripted_market()
    seen: dict = {}

    def probe(session, conn):                      # which decisions existed on which simulated days
        for (text,) in conn.execute(SEEN_SQL).fetchall():
            seen.setdefault(text, set()).add(session)

    reasons, incremental, oracle = simulate(tmp_path, market, monkeypatch, probe=probe)
    try:
        # The cover is lost when NSE's day 23 ages out (session 104); a session later BSE's own day 24 ages out
        # too and day 25, now BSE's first, has no gap at all.
        assert seen.get("BSE COVCUT gap -") == {104}
        assert max(seen["BSE PAUSE gap -"]) == 105                         # until NSE's late days came
        assert seen.get(f"NSE HALT2 - {pa.FLAG_NO_TRADE}")                  # the halt rule alone sees these two
        assert seen.get(f"BSE RELIST2 - {pa.FLAG_NEW_LISTING}")
        assert max(seen[f"BSE RELIST - {pa.FLAG_NEW_LISTING}"]) < market.n - 1   # gone once its old days aged out
        assert any(text.startswith("NSE NOISY -") and pa.FLAG_GAP in text for text in seen)
        assert max(seen["NSE EARLY list -"]) < market.n - 1
        assert seen.get("BSE COVBACK gap -") == {61, 62, 63, 64}            # covered once NSE's days came
        assert min(seen["BSE TWINGAP nse -"]) == 51
        assert min(seen["NSE OVERRIDE override -"]) == 45 and 90 in seen["NSE OVERRIDE override -"]
        # Every rule was exercised (range_back needs a store whose last day moves back).
        assert set(al.REASONS) - {"range_back"} <= set(reasons), set(al.REASONS) - set(reasons)
        ledger = {(e, s, ev) for e, s, ev in oracle.execute("SELECT Exchange, Symbol, Event FROM adjustment_ledger")
                  .fetchall()}
        assert ("BSE", "SPLIT", "nse") in ledger and ("NSE", "NEWNAME", "list") not in ledger    # aged out
        assert ("BSE", "TWINHALT", "nse") in ledger and ("BSE", "ONLYBSE", "list") in ledger
        assert ("NSE", "CONSOL", "list") in ledger and ("NSE", "DEMERGE", "list_price") in ledger
        assert ("NSE", "OVERRIDE", "override") in ledger and ("BSE", "BSELATE", "nse") in ledger
        assert ("BSE", "LATENSE", "nse") in ledger and ("NSE", "SMALLBON", "list") in ledger
        assert ("BSE", "PAUSE", "gap") not in ledger                       # covered once NSE's days came
        assert ("BSE", "NEWLIST", "nse") not in ledger and ("NSE", "GONE", "list") not in ledger
        twin_day = oracle.execute("SELECT Date FROM adjustment_ledger WHERE Exchange = 'BSE' AND Symbol = "
                                  "'TWINHALT'").fetchone()[0]
        assert twin_day == day_of(60)                                      # BSE's own ex-date, not NSE's 63
        late_day = oracle.execute("SELECT Date FROM adjustment_ledger WHERE Exchange = 'BSE' AND Symbol = "
                                  "'BSELATE'").fetchone()[0]
        assert late_day == day_of(81)
        first = oracle.execute("SELECT min(Date) FROM raw_prices").fetchone()[0]
        assert first > day_of(25)                                          # retention pruned the early splits
        assert oracle.execute("SELECT count(*) FROM gap_history WHERE Symbol = 'NOISY' AND Date < ?",
                              [first]).fetchone()[0] > 0                  # aged-out gaps still counted
        assert oracle.execute("SELECT count(*) FROM raw_prices WHERE Symbol = 'REUSED' AND Date < ?",
                              [day_of(90)]).fetchone()[0] == 0
        assert incremental.execute("SELECT count(*) FROM adjustment_ledger").fetchone()[0] > 10
    finally:
        incremental.close()
        oracle.close()


@pytest.mark.parametrize("seed", [11, 23, 37])
def test_random_markets_stay_equal_to_the_full_rebuild(tmp_path, monkeypatch, seed):
    reasons, incremental, oracle = simulate(tmp_path, random_market(seed), monkeypatch, verify_every=35)
    incremental.close()
    oracle.close()
    assert reasons                                                          # something was decided again


# --- replays, double application and the checks ---------------------------------------------------------

def pocl_market(sessions=30) -> Market:
    m = Market(sessions, seed=3)
    m.company("POCL", 1400, {20: 0.4})
    m.quote("NSE", "POCL", "INE063E01061", "POCL")
    m.quote("BSE", "POCL", "INE063E01061", "POCL", code="532626")
    m.lists.append(dict(exchange="NSE", symbol="POCL", isin="INE063E01061", ex=20, published=0,
                        purpose="Face Value Split (Sub-Division) - From Rs 5/- Per Share To Rs 2/- Per Share"))
    return m


@pytest.fixture(scope="module")
def pocl_template(tmp_path_factory):
    """A version 3 store with a listed split (POCL) and its BSE twin, fed 30 days and maintained (built once)."""
    folder = tmp_path_factory.mktemp("pocl")
    market = pocl_market()
    conn = duckdb.connect(str(folder / "store.duckdb"))
    create_schema(conn)
    for session in range(market.n):
        feed(conn, market, session, folder / "overrides.csv")
    al.maintain(conn, folder / "overrides.csv", None, day_of(market.n - 1), LOG)
    conn.close()
    return folder


def small_store(tmp_path, template):
    """A copy of the POCL store: (connection, its market, its overrides file)."""
    for name in ("store.duckdb", "overrides.csv"):
        shutil.copy2(template / name, tmp_path / name)
    return duckdb.connect(str(tmp_path / "store.duckdb")), pocl_market(), tmp_path / "overrides.csv"


def test_replaying_a_day_or_running_twice_changes_nothing(tmp_path, pocl_template):
    conn, market, overrides = small_store(tmp_path, pocl_template)
    before = snapshot(conn)
    decided_at = conn.execute("SELECT * FROM adjustment_ledger ORDER BY ALL").fetchall()
    today = day_of(market.n - 1)
    summary = al.maintain(conn, overrides, None, today, LOG)
    assert summary["mode"] == "incremental" and summary["decided"] == {"NSE": 0, "BSE": 0}
    for batch in market.deliveries(21) + market.deliveries(20):         # the ex-date and the day after, again
        upsert_daily_prices(conn, batch)
    al.maintain(conn, overrides, None, today, LOG)
    assert snapshot(conn) == before
    assert conn.execute("SELECT * FROM adjustment_ledger ORDER BY ALL").fetchall() == decided_at   # Decided_At kept
    close = conn.execute("SELECT Close FROM eod_prices WHERE Exchange = 'NSE' AND Date = ?", [day_of(0)]).fetchone()[0]
    raw = conn.execute("SELECT Close FROM raw_prices WHERE Exchange = 'NSE' AND Date = ?", [day_of(0)]).fetchone()[0]
    assert close == pytest.approx(raw * 0.4, abs=1e-4)                 # once, not 0.16 or 0.064
    conn.close()


def test_the_ledger_cannot_hold_two_factors_on_a_day_or_a_bad_factor(tmp_path, pocl_template):
    conn, _, _ = small_store(tmp_path, pocl_template)
    row = conn.execute("SELECT * FROM adjustment_ledger WHERE Exchange = 'NSE'").fetchone()
    with pytest.raises(duckdb.ConstraintException):
        conn.execute("INSERT INTO adjustment_ledger VALUES (?, ?, ?, 0.4, 'gap', 'gap|x', 0.4, NULL, NULL, NULL, 1)",
                     list(row[:3]))
    for bad in (0.0, -0.5, float("nan"), float("inf")):
        with pytest.raises(duckdb.ConstraintException):
            conn.execute("INSERT INTO adjustment_ledger VALUES ('NSE', 'X', DATE '2024-01-01', ?, 'gap', 'k', "
                         "NULL, NULL, NULL, NULL, 1)", [bad])
    conn.close()


def test_one_listed_event_on_two_days_is_applied_once_and_the_second_flagged():
    # Two list rows of one 1:1 bonus (a revision kept beside the old row), both days halving: the
    # second application is refused -- POCL was divided three times by the version 1 store.
    days = pd.bdate_range("2025-04-01", periods=40)
    closes = [100.0] * 20 + [50.0] * 2 + [25.0] * 18
    raw = pd.DataFrame({"Symbol": "TWICE", "Date": days, "Open": closes, "Close": closes})
    actions = pd.DataFrame({"Exchange": "NSE", "Segment": "equities", "Symbol": "TWICE", "Code": None,
                            "ISIN": "INE555A01011", "Company": "", "Series": "EQ", "Ex_Date": [days[20], days[22]],
                            "Purpose": ["Bonus 1:1", "Bonus 1:1 (revised)"], "Kind": "bonus", "Factor": 0.5})
    securities = pd.DataFrame({"Exchange": ["NSE"], "Symbol": ["TWICE"], "ISIN": ["INE555A01011"],
                               "Series": ["EQ"], "Code": [None]})
    decided, _ = pa.decide_exchange("NSE", raw, actions, securities, pd.DataFrame(columns=pa.OVERRIDE_COLUMNS),
                                    days[-1].date())
    applied = decided[decided["Event"].notna()]
    assert applied[["Event", "Factor"]].values.tolist() == [["list", 0.5], [pa.EVENT_REFUSED, 1.0]]
    assert applied["Flag"].iloc[1].startswith(pa.FLAG_REFUSED) and applied["Event_Key"].nunique() == 1


def test_a_bse_listing_beside_its_twins_factor_of_the_same_ratio_is_refused():
    days = pd.to_datetime(["2025-05-01", "2025-05-05", "2025-05-06"])
    decided = pd.DataFrame({"Symbol": "B", "Date": days, "Event": ["nse", "list", None], "Factor": [0.5, 0.5, 1.0],
                            "Gap": [0.5, 0.5, 1.0], "Flag": None, "Listed_Kind": [None, "bonus", None],
                            "Twin_Date": [days[0], pd.NaT, pd.NaT], "Exchange": "BSE"})
    out = pa.refuse_repeats(decided, "BSE")
    assert out["Event"].tolist() == ["nse", pa.EVENT_REFUSED, None] and out["Factor"].tolist() == [0.5, 1.0, 1.0]


def test_a_factor_stored_twice_fails_the_checks_rolls_back_and_the_full_rebuild_repairs(tmp_path, monkeypatch,
                                                                                         pocl_template):
    conn, market, overrides = small_store(tmp_path, pocl_template)
    before = snapshot(conn)
    real = eod_store.rematerialize
    bugs = {"left": 1}

    def twice(conn, scope=None, columns=("Exchange", "Symbol")):          # a bug: the factor applied twice
        real(conn, scope, columns)
        if bugs["left"] and scope is not None:
            bugs["left"] -= 1
            conn.execute("UPDATE eod_prices SET Open = Open * 0.4, High = High * 0.4, Low = Low * 0.4, "
                         "Close = Close * 0.4 WHERE Exchange = 'NSE' AND Date < ?", [day_of(20)])

    monkeypatch.setattr(eod_store, "rematerialize", twice)
    # NSE POCL is decided again and its adjusted rows rewritten: its periods are gone, its inputs "changed".
    conn.execute("DELETE FROM adjustment_periods WHERE Exchange = 'NSE'")
    conn.execute("UPDATE symbol_state SET Inputs_Digest = 'stale' WHERE Exchange = 'NSE'")
    tampered = snapshot(conn)
    review = tmp_path / "review.csv"
    with pytest.raises(al.LedgerCheckError) as caught:
        al.incremental(conn, overrides, review, day_of(market.n - 1), LOG)
    assert any(s == "POCL" and "applied twice" in what for _, s, what in caught.value.problems)
    assert snapshot(conn) == tampered                                    # rolled back, nothing written
    bugs["left"] = 1
    summary = al.maintain(conn, overrides, review, day_of(market.n - 1), LOG)
    assert summary["mode"] == "full (an incremental check failed)"
    assert "POCL" in review.read_text() and "applied twice" in review.read_text()
    assert snapshot(conn) == before                                      # the full rebuild repaired it
    conn.close()


def test_the_checks_catch_a_stored_row_out_of_step_with_raw_and_the_ledger(tmp_path, pocl_template):
    conn, _, _ = small_store(tmp_path, pocl_template)
    assert al.check_prices(conn) == [] and al.check_continuity(conn) == []
    conn.execute("UPDATE eod_prices SET Close = Close + 1 WHERE Exchange = 'BSE' AND Date = ?", [day_of(3)])
    conn.execute("DELETE FROM eod_prices WHERE Exchange = 'NSE' AND Date = ?", [day_of(4)])
    problems = al.check_prices(conn)
    assert {(e, s) for e, s, _ in problems} == {("BSE", "POCL"), ("NSE", "POCL")}
    conn.execute("DELETE FROM adjustment_ledger WHERE Exchange = 'BSE'")   # the factor gone from the ledger only
    assert ("BSE", "POCL") in {(e, s) for e, s, _ in al.check_prices(conn)}
    conn.close()


# --- the full verify and its repair ---------------------------------------------------------------------

def test_verify_finds_and_repairs_any_difference_from_the_full_rebuild(tmp_path, caplog, pocl_template):
    conn, market, overrides = small_store(tmp_path, pocl_template)
    today = day_of(market.n - 1)
    before = snapshot(conn)
    assert al.verify(conn, overrides, None, today, LOG)["status"] == "ok"
    conn.execute("UPDATE eod_prices SET Close = 1 WHERE Exchange = 'NSE' AND Date = ?", [day_of(2)])
    conn.execute("DELETE FROM adjustment_ledger WHERE Exchange = 'BSE'")
    conn.execute("DELETE FROM gap_history")
    with caplog.at_level(logging.ERROR):
        result = al.verify(conn, overrides, None, today, LOG)
    assert result["status"] == "repaired"
    assert {"eod_prices", "adjustment_ledger"} <= set(result["differences"])
    assert "FULL VERIFY" in caplog.text and "POCL" in caplog.text
    assert snapshot(conn) == before
    assert al.verify(conn, overrides, None, today, LOG)["status"] == "ok"
    conn.close()


def test_the_full_verify_runs_by_itself_once_a_week(tmp_path, pocl_template):
    conn, market, overrides = small_store(tmp_path, pocl_template)
    today = day_of(market.n - 1)
    assert "verify" not in al.maintain(conn, overrides, None, today, LOG)
    conn.execute("UPDATE maintenance_state SET Value = ? WHERE Key = 'last_full_verify'",
                 [(today - timedelta(days=7)).isoformat()])
    summary = al.maintain(conn, overrides, None, today, LOG)
    assert summary["verify"]["status"] == "ok"
    assert al.read_state(conn)["last_full_verify"] == today.isoformat()
    conn.close()


def test_a_rule_or_duckdb_change_takes_the_full_rebuild(tmp_path, pocl_template):
    conn, market, overrides = small_store(tmp_path, pocl_template)
    conn.execute("UPDATE maintenance_state SET Value = '0' WHERE Key = 'rule_version'")
    assert al.maintain(conn, overrides, None, day_of(market.n - 1), LOG)["mode"].startswith("full (decision rules")
    conn.execute("UPDATE maintenance_state SET Value = '0.0.1' WHERE Key = 'duckdb_version'")
    assert al.maintain(conn, overrides, None, day_of(market.n - 1), LOG)["mode"].startswith("full (DuckDB")
    conn.close()


# --- migration from version 2 ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def v2_template(tmp_path_factory):
    """A version 2 store (views over raw prices) of the scripted market's first 60 days, decided by the
    version 2 path (built once)."""
    folder = tmp_path_factory.mktemp("v2")
    market = scripted_market()
    market.n = 60
    conn = duckdb.connect(str(folder / "v2.duckdb"))
    create_schema(conn, 2)
    for session in range(60):
        feed(conn, market, session, folder / "overrides.csv")
    pa.rebuild(conn, folder / "overrides.csv", None, day_of(59), LOG)
    assert schema_version(conn) == 2
    conn.close()
    return folder


def v2_store(tmp_path, template, name="v2.duckdb"):
    """A copy of the version 2 store: (its path, its overrides file)."""
    shutil.copy2(template / "v2.duckdb", tmp_path / name)
    shutil.copy2(template / "overrides.csv", tmp_path / "overrides.csv")
    return tmp_path / name, tmp_path / "overrides.csv"


RAW_HASH = ("SELECT count(*), sum(hash(Date, Symbol, Exchange, Open, High, Low, Close, Volume, Total_Trades, "
            "Dlv_Qty)) FROM raw_prices")


def test_a_version_2_store_migrates_with_a_backup_and_reads_the_same(tmp_path, caplog, v2_template):
    path, overrides = v2_store(tmp_path, v2_template)
    conn = duckdb.connect(str(path))
    view = conn.execute("SELECT * FROM eod_data ORDER BY ALL").fetchall()
    raw = conn.execute(RAW_HASH).fetchone()
    conn.close()
    with caplog.at_level(logging.INFO):
        assert al.migrate(path, overrides, None, day_of(59), LOG)
    backup = path.with_name("v2.v2-backup.duckdb")
    old = duckdb.connect(str(backup), read_only=True)
    assert schema_version(old) == 2 and old.execute("SELECT * FROM eod_data ORDER BY ALL").fetchall() == view
    old.close()
    conn = duckdb.connect(str(path))
    assert schema_version(conn) == 3
    assert conn.execute("SELECT * FROM eod_data ORDER BY ALL").fetchall() == view          # what the engine reads
    assert conn.execute(RAW_HASH).fetchone() == raw                                         # raw untouched
    assert conn.execute("SELECT count(*) FROM adjustment_ledger").fetchone()[0] > 5
    assert al.verify(conn, overrides, None, day_of(59), LOG, repair=False)["status"] == "ok"
    assert al.maintain(conn, overrides, None, day_of(59), LOG)["mode"] == "incremental"
    assert "migrated to version 3" in caplog.text
    conn.close()


def test_a_failed_migration_rolls_back_and_the_store_stays_version_2(tmp_path, monkeypatch, caplog, v2_template):
    path, overrides = v2_store(tmp_path, v2_template)
    conn = duckdb.connect(str(path))
    view = conn.execute("SELECT * FROM eod_data ORDER BY ALL").fetchall()
    conn.close()

    def broken(*args, **kwargs):
        raise RuntimeError("disk gave up")

    monkeypatch.setattr(al, "write_all", broken)
    with caplog.at_level(logging.ERROR):
        assert al.migrate(path, overrides, None, day_of(59), LOG) is False
    assert "MIGRATION TO VERSION 3 FAILED" in caplog.text
    conn = duckdb.connect(str(path))
    assert schema_version(conn) == 2
    assert conn.execute("SELECT * FROM eod_data ORDER BY ALL").fetchall() == view
    assert al.maintain(conn, overrides, None, day_of(59), LOG)["mode"] == "full"          # decided as version 2
    conn.close()


def test_the_migration_waits_when_the_disk_has_no_room(tmp_path, monkeypatch, caplog, v2_template):
    path, overrides = v2_store(tmp_path, v2_template)
    monkeypatch.setattr(al.shutil, "disk_usage", lambda folder: shutil._ntuple_diskusage(10, 9, 1))
    with caplog.at_level(logging.ERROR):
        assert al.migrate(path, overrides, None, day_of(59), LOG) is False
    assert "NOT MIGRATED" in caplog.text and not path.with_name("v2.v2-backup.duckdb").exists()
    conn = duckdb.connect(str(path), read_only=True)
    assert schema_version(conn) == 2
    conn.close()


def test_the_maintenance_migrates_a_version_2_store_on_its_first_run(tmp_path, monkeypatch, v2_template):
    import config
    import eod_maintenance

    path, _ = v2_store(tmp_path, v2_template, "NSE_BSE_EOD.duckdb")
    monkeypatch.setattr(config, "DUCKDB_FILE", path)
    monkeypatch.setattr(config, "EOD_METADATA_DIR", tmp_path / "metadata")
    monkeypatch.setattr(config, "DATABASE_DIR", tmp_path)
    monkeypatch.setattr(config, "EOD_RETAIN_YEARS", 5)
    monkeypatch.setattr(action_lists, "refresh", lambda *a, **k: {})
    monkeypatch.setattr(action_lists, "refresh_history_if_due", lambda *a, **k: {})
    summary = eod_maintenance.maintain(LOG, nse_factory=lambda logger: None, today=day_of(59))
    assert summary["schema_version"] == 3 and summary["mode"] == "incremental"
    assert (tmp_path / "NSE_BSE_EOD.v2-backup.duckdb").exists()
    assert eod_maintenance.verify_store(LOG, today=day_of(59))["status"] == "ok"


# --- the other writers keep a version 3 store in step -----------------------------------------------------

def test_a_compacted_version_3_store_keeps_its_tables_and_stays_equal_to_the_full_rebuild(tmp_path, pocl_template):
    from compact_price_database import build_compact_database

    conn, market, overrides = small_store(tmp_path, pocl_template)
    conn.close()
    target = tmp_path / "compact.duckdb"
    build_compact_database(tmp_path / "store.duckdb", target, years=60 / 365.25)     # cuts the first rows
    conn = duckdb.connect(str(target))
    assert schema_version(conn) == 3
    assert conn.execute("SELECT count(*) FROM symbol_state WHERE Pruned_Before IS NOT NULL").fetchone()[0] == 2
    summary = al.maintain(conn, overrides, None, day_of(29), LOG)
    assert summary["mode"] == "incremental"
    assert al.verify(conn, overrides, None, day_of(29), LOG, repair=False)["status"] == "ok"
    conn.close()


def test_a_late_delivery_report_updates_the_stored_adjusted_rows(tmp_path, pocl_template):
    from nse_eod_downloader.delivery import store_delivery

    conn, market, overrides = small_store(tmp_path, pocl_template)
    conn.close()
    report = pd.DataFrame({"Symbol": ["POCL"], "Series": ["EQ"], "Total_Trades": [77], "Dlv_Qty": [5000]})
    assert store_delivery(report, day_of(5), tmp_path / "store.duckdb") == 1
    conn = duckdb.connect(str(tmp_path / "store.duckdb"))
    stored = conn.execute("SELECT Total_Trades, Dlv_Qty FROM eod_prices WHERE Exchange = 'NSE' AND Date = ?",
                          [day_of(5)]).fetchone()
    assert stored == (77, round(5000 / 0.4))                               # before the split: divided by 0.4
    assert al.check_prices(conn) == []
    conn.close()


def test_a_rename_moves_the_ledger_and_the_gap_history_with_the_raw_rows(tmp_path, pocl_template):
    conn, market, overrides = small_store(tmp_path, pocl_template)
    conn.execute("INSERT INTO gap_history VALUES ('NSE', 'POCL', DATE '2020-01-01', 0.4)")    # aged out long ago
    gaps = conn.execute("SELECT count(*) FROM gap_history WHERE Exchange = 'NSE' AND Symbol = 'POCL'").fetchone()[0]
    rows = market.row(market.series[0], market.n - 1)
    rows.update(Symbol="POCLNEW", Date=day_of(market.n))
    upsert_daily_prices(conn, pd.DataFrame([rows]))
    assert conn.execute("SELECT DISTINCT Symbol FROM adjustment_ledger WHERE Exchange = 'NSE'").fetchall() == [
        ("POCLNEW",)]
    assert conn.execute("SELECT count(*) FROM gap_history WHERE Symbol = 'POCLNEW'").fetchone()[0] == gaps == 2
    assert conn.execute("SELECT count(*) FROM eod_prices WHERE Exchange = 'NSE' AND Symbol = 'POCL'").fetchone()[0] == 0
    summary = al.maintain(conn, overrides, None, day_of(market.n), LOG)
    assert summary["reasons"]["NSE"].get("new") == 1
    assert al.verify(conn, overrides, None, day_of(market.n), LOG, repair=False)["status"] == "ok"
    assert al.check_prices(conn) == []
    conn.close()
