"""Decide every split, bonus and demerger factor from the raw prices and the exchanges' lists; rebuild the prices.

    refresh(conn, ...)   after every sync: fetch the lists around today, decide, rewrite the factor periods

This is the method of the market-data lake the screener was checked against
(2026-10): the stored prices are the exchanges' own, and the adjusted prices
are recomputed from them and a factor table on every run -- never divided in
place. Per symbol and day, in this order:

  1. a hand decision in Database/metadata/corporate_action_overrides.csv
     (Exchange, Symbol, Date, Factor, Note) -- for what no rule can see;
  2. BSE only: the NSE twin's decided factor (same ISIN), as it is -- the same
     shares, so the same factor, and the same adjusted series on both exchanges;
     while the twin trades on NSE no BSE gap rule runs (BSE's thin trading sees
     events NSE does not have);
  3. the exchange's own list (NSE matched by ISIN, BSE by scrip code -- never by
     today's name, which lost ACUTAAS's split when its name changed): the
     factors listed for that symbol-day multiplied (a bonus and a split are two
     rows), used when the day's open agrees within 25%, or, for a factor within
     15% of 1 (a 1:10 bonus), when the open did not gap by more than 30%. Where
     the open disagrees the action happened on another day, so it is not used;
  4. a listed demerger, capital reduction or ratio-less consolidation: the open
     over the previous close, as it is -- the price decides (INDIAGLYCO 2026-09-02);
  5. an open below 0.55x the previous close (priced Rs 5 and up) that nothing
     explains: a split or bonus 91.5% of the time, scaled by the most common
     ratio within ~17%; 0.55-0.80x or above 1.8x is FLAGGED and left alone.
A listed action is matched to the symbol's first trading day on or after its
ex-date, at most 10 days later. Prices before a factor's day are multiplied by
it; volume and delivery divided. Every decision goes to `price_adjustments`;
the last 30 days' flags and unconfirmed listings to corporate_action_review.csv.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

import action_lists
from action_factors import LISTED_KINDS, PRICE_KINDS

SURE_BELOW, UNSURE_BELOW, UNSURE_ABOVE = 0.55, 0.80, 1.8
MIN_PRICE, MAX_GAP_EVENTS = 5.0, 8
LIST_TOLERANCE, NEAR_ONE, NO_GAP = 0.22, 0.15, 0.3
LAG = pd.Timedelta(days=10)
ISIN_SECURITY = 9                       # country, issuer type, issuer and security type: kept through a split
REVIEW_DAYS = 30
# The ratios splits and bonuses come in, most common first: the ex-date's own move
# is often 5-15%, so the nearest ratio in a dense list is not the answer.
RATIO_RANKS = {1 / 2: 1, 1 / 3: 1, 1 / 4: 1, 1 / 5: 1, 1 / 10: 1,
               2 / 5: 2, 1 / 6: 2, 1 / 20: 2,
               3 / 7: 3, 4 / 9: 3, 3 / 8: 3, 2 / 7: 3, 1 / 7: 3, 1 / 8: 3, 1 / 15: 3, 1 / 25: 3, 1 / 50: 3, 1 / 100: 3}
RATIOS = np.array(sorted(RATIO_RANKS))
RANKS = np.array([RATIO_RANKS[r] for r in RATIOS])
RATIO_TOLERANCE = 0.16
OVERRIDE_COLUMNS = ["Exchange", "Symbol", "Date", "Factor", "Note"]


def nearest_ratio(gaps) -> np.ndarray:
    """The most common ratio within RATIO_TOLERANCE of each gap; the nearest one if none is."""
    gaps = np.clip(np.asarray(gaps, dtype=float), 1e-9, None)
    distance = np.abs(np.log(RATIOS)[None, :] - np.log(gaps)[:, None])
    score = np.where(distance < RATIO_TOLERANCE, RANKS[None, :] * 10 + distance, 1000 + distance)
    return RATIOS[score.argmin(axis=1)]


def with_gaps(raw: pd.DataFrame) -> pd.DataFrame:
    raw = raw.sort_values(["Symbol", "Date"]).reset_index(drop=True)
    previous = raw.groupby("Symbol")["Close"].shift(1)
    raw["Gap"] = (raw["Open"] / previous).where((raw["Open"] > 0) & (previous > 0))
    raw["_priced"] = previous >= MIN_PRICE
    return raw


def first_trading_day(keys: pd.DataFrame, days: pd.DataFrame) -> pd.DataFrame:
    """(Symbol, Ex_Date, ...) -> the same rows with `Date`, the symbol's first trading day within LAG on or after it."""
    if keys.empty:
        return keys.assign(Date=pd.Series(dtype="datetime64[ns]"))
    left = keys.assign(Ex_Date=pd.to_datetime(keys["Ex_Date"])).sort_values("Ex_Date")
    right = days[["Symbol", "Date"]].assign(Date=pd.to_datetime(days["Date"])).sort_values("Date")
    hit = pd.merge_asof(left, right.assign(_on=right["Date"]), left_on="Ex_Date", right_on="_on", by="Symbol",
                        direction="forward", tolerance=LAG)
    return hit.dropna(subset=["Date"]).drop(columns="_on")


def symbols_for(actions: pd.DataFrame, securities: pd.DataFrame, exchange: str) -> pd.DataFrame:
    """Each listed action of `exchange` with the store's symbol: by ISIN (NSE) or scrip code (BSE), else by name."""
    sec = securities[securities["Exchange"] == exchange]
    acts = actions[actions["Exchange"] == exchange].copy()
    symbol = pd.Series(pd.NA, index=acts.index, dtype="object")
    if exchange == "NSE":
        by_isin = dict(zip(sec["ISIN"], sec["Symbol"]))
        prefix = sec.assign(_p=sec["ISIN"].str[:ISIN_SECURITY]).drop_duplicates("_p", keep=False)
        by_prefix = dict(zip(prefix["_p"], prefix["Symbol"]))
        symbol = acts["ISIN"].map(by_isin)
        symbol = symbol.fillna(acts["ISIN"].str[:ISIN_SECURITY].map(by_prefix))
    else:
        by_code = dict(zip(sec["Code"].dropna(), sec.loc[sec["Code"].notna(), "Symbol"]))
        symbol = acts["Code"].map(by_code)
    known = set(sec["Symbol"])
    plain = acts["Symbol"].where(acts["Symbol"].isin(known))
    sme = (acts["Symbol"] + "_SME").where((acts["Symbol"] + "_SME").isin(known))
    acts["Symbol"] = symbol.fillna(sme).fillna(plain)
    return acts.dropna(subset=["Symbol"])


def listed_days(actions: pd.DataFrame, securities: pd.DataFrame, raw: pd.DataFrame, exchange: str) -> pd.DataFrame:
    """(Symbol, Date, Listed_Kind, Listed_Factor, Price_Decides) from the exchange's list."""
    moving = actions["Kind"].fillna("").str.contains("|".join(LISTED_KINDS + PRICE_KINDS)) & actions["Ex_Date"].notna()
    acts = symbols_for(actions[moving], securities, exchange)
    hits = first_trading_day(acts[["Symbol", "Ex_Date", "Kind", "Factor"]], raw)
    if hits.empty:
        return pd.DataFrame(columns=["Symbol", "Date", "Listed_Kind", "Listed_Factor", "Price_Decides"])
    hits = hits.drop_duplicates(["Symbol", "Date", "Kind", "Factor"])
    hits["_decides"] = hits["Factor"].isna() & hits["Kind"].str.contains("|".join(PRICE_KINDS))
    grouped = hits.groupby(["Symbol", "Date"])
    out = pd.DataFrame({
        "Listed_Kind": grouped["Kind"].agg(lambda k: "+".join(sorted(k))),
        "Listed_Factor": grouped["Factor"].agg(lambda f: f.dropna().prod() if f.notna().any() else np.nan),
        "Price_Decides": grouped["_decides"].any(),
    }).reset_index()
    return out


def decide(raw: pd.DataFrame, listed: pd.DataFrame, overrides: pd.DataFrame, twin: pd.DataFrame | None = None,
           covered: pd.Series | None = None) -> pd.DataFrame:
    """Factor, event and flag for every row of one exchange; `Adj` multiplies the row's prices."""
    df = with_gaps(raw)
    df["Date"] = pd.to_datetime(df["Date"])
    for frame in (listed, overrides) + ((twin,) if twin is not None else ()):
        frame["Date"] = pd.to_datetime(frame["Date"])
    df = df.merge(listed, on=["Symbol", "Date"], how="left")
    df = df.merge(overrides[["Symbol", "Date", "Factor"]].drop_duplicates(["Symbol", "Date"], keep="last")
                  .rename(columns={"Factor": "_override"}), on=["Symbol", "Date"], how="left")
    df["_twin"] = np.nan
    if twin is not None and not twin.empty:
        df = df.drop(columns="_twin").merge(twin.rename(columns={"Factor": "_twin"})[["Symbol", "Date", "_twin"]],
                                           on=["Symbol", "Date"], how="left")
    df["_covered"] = covered.to_numpy() if covered is not None else False
    for column in ("Listed_Factor", "_override", "_twin"):                 # an empty list merges as object
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["Price_Decides"] = df["Price_Decides"].eq(True)

    gap = df["Gap"]
    gap_off = np.abs(np.log(gap))
    priced = df["_priced"].fillna(False)
    covered_ = df["_covered"].astype(bool)
    sure = priced & ~covered_ & (gap < SURE_BELOW)
    noisy = sure.groupby(df["Symbol"]).transform("sum") > MAX_GAP_EVENTS
    sure = sure & ~noisy
    unsure = priced & ~covered_ & (((gap >= SURE_BELOW) & (gap < UNSURE_BELOW)) | (gap >= UNSURE_ABOVE)
                                   | ((gap < SURE_BELOW) & noisy))
    listed_factor = df["Listed_Factor"]
    near_one = np.abs(np.log(listed_factor)) < NEAR_ONE
    by_list = listed_factor.notna() & gap.notna() & np.where(
        near_one, gap_off < NO_GAP, np.abs(np.log(gap / listed_factor)) < LIST_TOLERANCE)
    by_list = pd.Series(by_list, index=df.index).fillna(False).astype(bool)
    override = df["_override"].notna()
    by_twin = df["_twin"].notna() & ~override
    by_price = (df["Price_Decides"] & gap.notna() & (gap_off > 0.03) & priced
                & ~by_list & ~by_twin)
    snap = pd.Series(1.0, index=df.index)
    candidates = sure & gap.notna()
    snap[candidates] = nearest_ratio(gap[candidates].to_numpy())

    factor = np.select([override, by_twin, by_list, by_price, sure],
                       [df["_override"], df["_twin"], listed_factor, gap, snap], default=1.0)
    event = np.select([override, by_twin, by_list, by_price, sure],
                      ["override", "nse", "list", "list_price", "gap"], default=None)
    explained = override | by_twin | by_list | by_price
    df["Factor"] = factor.astype(float)
    df["Event"] = event
    df["Flag"] = np.where(unsure & ~explained, "unresolved gap", None)
    # Each row is multiplied by the product of the factors on LATER days of its symbol.
    log_f = np.log(df["Factor"])
    after = log_f.groupby(df["Symbol"]).transform("sum") - log_f.groupby(df["Symbol"]).cumsum()
    df["Adj"] = np.exp(after)
    return df.drop(columns=["_priced", "_override", "_twin", "_covered"])


def twin_inputs(bse_raw: pd.DataFrame, nse_decided: pd.DataFrame, securities: pd.DataFrame):
    """(twin factors on BSE days, covered flags aligned with with_gaps(bse_raw)) from NSE's decisions."""
    nse_sec = securities[securities["Exchange"] == "NSE"]
    bse_sec = securities[securities["Exchange"] == "BSE"]
    exact = dict(zip(nse_sec["ISIN"], nse_sec["Symbol"]))
    prefix = nse_sec.assign(_p=nse_sec["ISIN"].str[:ISIN_SECURITY]).drop_duplicates("_p", keep=False)
    by_prefix = dict(zip(prefix["_p"], prefix["Symbol"]))
    twins = bse_sec["ISIN"].map(exact).fillna(bse_sec["ISIN"].str[:ISIN_SECURITY].map(by_prefix))
    twin_of = dict(zip(bse_sec["Symbol"], twins))
    rows = with_gaps(bse_raw)[["Symbol", "Date"]].copy()
    rows["Date"] = pd.to_datetime(rows["Date"])
    rows["Twin"] = rows["Symbol"].map(twin_of)
    events = nse_decided[nse_decided["Event"].notna() & (nse_decided["Factor"] != 1.0)][["Symbol", "Date", "Factor"]]
    events = events.rename(columns={"Symbol": "Twin"})
    days = rows.dropna(subset=["Twin"])
    twin = pd.DataFrame(columns=["Symbol", "Date", "Factor"])
    if not events.empty and not days.empty:
        hit = pd.merge_asof(events.assign(Date=pd.to_datetime(events["Date"])).sort_values("Date"),
                            days.rename(columns={"Date": "BSE_Date"}).assign(_on=lambda d: d["BSE_Date"]).sort_values("_on"),
                            left_on="Date", right_on="_on", by="Twin", direction="forward", tolerance=LAG)
        hit = hit.dropna(subset=["Symbol"])
        twin = (hit.groupby(["Symbol", "BSE_Date"])["Factor"].prod().reset_index()
                   .rename(columns={"BSE_Date": "Date"}))
    nse_days = nse_decided[["Symbol", "Date"]].rename(columns={"Symbol": "Twin", "Date": "NSE_Date"})
    nse_days = nse_days.assign(NSE_Date=pd.to_datetime(nse_days["NSE_Date"]))
    probe = rows.reset_index().dropna(subset=["Twin"]).sort_values("Date")
    covered = pd.Series(False, index=rows.index)
    if not probe.empty and not nse_days.empty:
        hit = pd.merge_asof(probe, nse_days.sort_values("NSE_Date").assign(_on=lambda d: d["NSE_Date"]),
                            left_on="Date", right_on="_on", by="Twin", direction="backward", tolerance=LAG)
        covered[hit.loc[hit["NSE_Date"].notna(), "index"].to_numpy()] = True
    return twin, covered


def read_overrides(path: Path | None) -> pd.DataFrame:
    if path is None or not Path(path).is_file():
        return pd.DataFrame(columns=OVERRIDE_COLUMNS)
    frame = pd.read_csv(path)
    frame["Date"] = pd.to_datetime(frame["Date"])
    frame["Symbol"] = frame["Symbol"].astype(str).str.strip().str.upper()
    frame["Exchange"] = frame["Exchange"].astype(str).str.strip().str.upper()
    return frame


def rebuild(conn, overrides_path: Path | None = None, review_path: Path | None = None, today: date | None = None) -> dict:
    """Decide every factor from what is stored; rewrite `adjustment_periods` and `price_adjustments` in one transaction."""
    raw = conn.execute("SELECT * FROM raw_prices").df()
    securities = conn.execute("SELECT Exchange, Symbol, ISIN, Series, Code FROM securities").df()
    actions = conn.execute("SELECT * FROM exchange_actions").df()
    overrides = read_overrides(overrides_path)
    decided = {}
    for exchange in ("NSE", "BSE"):
        part = raw[raw["Exchange"] == exchange].drop(columns="Exchange")
        listed = listed_days(actions, securities, part, exchange)
        mine = overrides[overrides["Exchange"] == exchange]
        twin, covered = (twin_inputs(part, decided["NSE"], securities) if exchange == "BSE" and "NSE" in decided
                         else (None, None))
        decided[exchange] = decide(part, listed, mine, twin, covered).assign(Exchange=exchange)
    frames = [d for d in decided.values() if not d.empty]
    if not frames:                                                   # an empty store: nothing to decide
        frames = [pd.DataFrame(columns=["Exchange", "Symbol", "Date", "Gap", "Factor", "Event", "Flag",
                                        "Listed_Kind", "Listed_Factor"])]
    both = pd.concat(frames, ignore_index=True)
    both["Date"] = pd.to_datetime(both["Date"]).dt.date
    events = both[both["Event"].notna() | both["Flag"].notna() | both["Listed_Kind"].notna()]
    events = events[["Exchange", "Symbol", "Date", "Gap", "Factor", "Event", "Flag", "Listed_Kind", "Listed_Factor"]]
    periods = adjustment_periods(events)
    conn.register("decided_periods", periods)
    conn.register("decided_events", events)
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("DELETE FROM adjustment_periods")
        conn.execute("INSERT INTO adjustment_periods SELECT * FROM decided_periods")
        conn.execute("DELETE FROM price_adjustments")
        conn.execute("INSERT INTO price_adjustments SELECT * FROM decided_events")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.unregister("decided_periods")
        conn.unregister("decided_events")
    review = review_list(events, today or date.today())
    if review_path is not None:
        review.to_csv(review_path, index=False)
    applied = events[events["Event"].notna() & (events["Factor"] != 1.0)]
    factors = {f"{exchange} {event}": int(n) for (exchange, event), n in applied.groupby(["Exchange", "Event"]).size().items()}
    return {"rows": len(both), "factors": factors,
            "flagged": int(events["Flag"].notna().sum()), "review_rows": len(review)}


def adjustment_periods(events: pd.DataFrame) -> pd.DataFrame:
    """(Exchange, Symbol, From_Date, Adj): from each factor's day on, the product of the symbol's later factors.

    The first period starts on 1900-01-01 and holds the product of all of them;
    the last starts on the last factor's day and holds 1.
    """
    applied = events[events["Event"].notna() & (events["Factor"] != 1.0)][["Exchange", "Symbol", "Date", "Factor"]]
    rows = []
    for (exchange, symbol), part in applied.sort_values("Date").groupby(["Exchange", "Symbol"]):
        factors, days = part["Factor"].to_numpy(), list(part["Date"])
        later = np.cumprod(factors[::-1])[::-1]                # product of this and every later factor
        rows.append((exchange, symbol, date(1900, 1, 1), float(later[0])))
        for i, day in enumerate(days):
            rows.append((exchange, symbol, day, float(later[i + 1]) if i + 1 < len(days) else 1.0))
    return pd.DataFrame(rows, columns=["Exchange", "Symbol", "From_Date", "Adj"])


def review_list(events: pd.DataFrame, today: date) -> pd.DataFrame:
    """The last 30 days' unexplained gaps and listed factors the price did not confirm."""
    since = pd.Timestamp(today) - pd.Timedelta(days=REVIEW_DAYS)
    recent = events[pd.to_datetime(events["Date"]) >= since]
    unconfirmed = recent["Listed_Factor"].notna() & ~recent["Event"].isin(["list", "nse", "override"])
    return recent[recent["Flag"].notna() | unconfirmed].sort_values(["Date", "Exchange", "Symbol"])


def refresh(conn, nse, logger: logging.Logger, overrides_path: Path | None = None, review_path: Path | None = None,
            today: date | None = None, list_start: date | None = None) -> dict:
    """After a sync: fetch the lists around today (or from `list_start`), then decide and rebuild."""
    today = today or datetime.now().date()
    start, end = action_lists.window(today)
    lists = action_lists.refresh(conn, nse, list_start or start, end, logger)
    summary = rebuild(conn, overrides_path, review_path, today)
    logger.info(f"Prices rebuilt from raw: {summary}")
    if summary["review_rows"]:
        logger.warning(f"{summary['review_rows']} recent price gaps need a look: {review_path}")
    return {"lists": lists, **summary}
