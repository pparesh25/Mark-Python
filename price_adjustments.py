"""Decide every split, bonus and demerger factor from the raw prices and the exchanges' lists; rebuild the prices.

    refresh(conn, ...)   after every sync: fetch the lists around today, decide, rewrite the factor periods

This is the method of the market-data lake the screener was checked against
(2026-10): the stored prices are the exchanges' own, and the adjusted prices
are recomputed from them and a factor table on every run -- never divided in
place. Per symbol and day, in this order:

  1. a hand decision in Database/metadata/corporate_action_overrides.csv
     (Exchange, Symbol, Date, Factor, Note) -- for what no rule can see. A row
     is used only when Exchange is NSE or BSE, Date is YYYY-MM-DD (01/02/2026
     would read as 2 January) and Factor is a plain
     number from 0.001 to 1000 ("1/2", "50%", "1.0x" and blanks are not
     numbers); any other row is skipped with a warning, and so is a valid row
     that matches no stored day of that symbol (a Date that was not a trading
     day, a mistyped Symbol). A factor of 0 made every read of `eod_prices`
     fail, a negative one made prices negative;
  2. BSE only: the NSE twin's decided factor (same ISIN), as it is -- the same
     shares, so the same factor, and the same adjusted series on both exchanges.
     It is placed on the BSE twin's first trading day on or after NSE's day,
     however late (every earlier BSE row is before the event), and is FLAGGED
     when that BSE day's open does not show it. While the twin trades on NSE no
     BSE gap rule runs (BSE's thin trading sees events NSE does not have). A
     BSE listing within 30 days of a twin factor with the same ratio (or none)
     is the same event, applied once: on the listing's day when BSE's open
     there confirms it and the twin's day does not (NSE, halted across the
     ex-date, decided it on its first day back), else on the twin's day, the
     listing becoming `nse_covers` -- a bonus BSE listed on another day was
     applied twice. A listing with another ratio is another event: the list
     rule decides it and it is FLAGGED;
  3. the exchange's own list (NSE matched by ISIN, else by today's name; BSE by
     scrip code only -- never by today's name, which lost ACUTAAS's split when
     its name changed and put another scrip's bonus on a namesake): the
     factors listed for that symbol-day multiplied (a bonus and a split are two
     rows), used when the day's open agrees within 25%, or, for a factor within
     15% of 1 (a 1:10 bonus), when the open did not gap by more than 30%. Where
     the open disagrees the action happened on another day, so it is not used;
  4. a listed demerger, capital reduction or ratio-less consolidation: the open
     over the previous close, as it is -- the price decides (INDIAGLYCO 2026-09-02);
  5. an open below 0.55x the previous close (priced Rs 5 and up) that nothing
     explains: a split or bonus 91.5% of the time, scaled by the most common
     ratio within ~17%; 0.55-0.80x or above 1.8x is FLAGGED and left alone.
A symbol back after more than 366 calendar days without a trade is a new
listing, as in the lake: its first day has no gap and so no gap or list factor
(BSE MPFSL: 96.09 on 2024-12-02, 2.49 on 2026-07-27 after a resolution plan,
was "split" 1/50), and it is FLAGGED. A listed action is matched to the
symbol's first trading day on or after its ex-date, at most 10 days later; one
that matched no day although the symbol traded again later is FLAGGED ("listed,
no trade within 10 days"). Prices before a factor's day are multiplied by it;
volume and delivery divided. Every decision goes to `price_adjustments`; the
last 30 days' flags and unconfirmed listings to corporate_action_review.csv.
Before anything is written every applied factor must be finite and above 0,
or the rebuild raises and the previous tables stay.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import action_lists
from action_factors import LISTED_KINDS, PRICE_KINDS

# The market's clock (config.MARKET_TIMEZONE), named here so this module needs no config.
MARKET_TZ = ZoneInfo("Asia/Kolkata")
SURE_BELOW, UNSURE_BELOW, UNSURE_ABOVE = 0.55, 0.80, 1.8
MIN_PRICE, MAX_GAP_EVENTS = 5.0, 8
LIST_TOLERANCE, NEAR_ONE, NO_GAP = 0.22, 0.15, 0.3
LAG = pd.Timedelta(days=10)
NEW_LISTING_AFTER_DAYS = 366            # the lake's rule: back after a year without a trade is a new listing
TWIN_SAME_EVENT = pd.Timedelta(days=30)
MIN_OVERRIDE, MAX_OVERRIDE = 0.001, 1000.0
ISIN_SECURITY = 9                       # country, issuer type, issuer and security type: kept through a split
REVIEW_DAYS = 30
FLAG_GAP = "unresolved gap"
FLAG_NEW_LISTING = f"new listing: back after more than {NEW_LISTING_AFTER_DAYS} days without a trade, no factor"
FLAG_TWIN = "NSE twin's factor; the BSE open does not show it"
FLAG_TWIN_DIFFERS = "BSE listing's ratio differs from the NSE twin's factor nearby"
FLAG_NO_TRADE = f"listed, no trade within {LAG.days} days"
# The ratios splits and bonuses come in, most common first: the ex-date's own move
# is often 5-15%, so the nearest ratio in a dense list is not the answer.
RATIO_RANKS = {1 / 2: 1, 1 / 3: 1, 1 / 4: 1, 1 / 5: 1, 1 / 10: 1,
               2 / 5: 2, 1 / 6: 2, 1 / 20: 2,
               3 / 7: 3, 4 / 9: 3, 3 / 8: 3, 2 / 7: 3, 1 / 7: 3, 1 / 8: 3, 1 / 15: 3, 1 / 25: 3, 1 / 50: 3, 1 / 100: 3}
RATIOS = np.array(sorted(RATIO_RANKS))
RANKS = np.array([RATIO_RANKS[r] for r in RATIOS])
RATIO_TOLERANCE = 0.16
OVERRIDE_COLUMNS = ["Exchange", "Symbol", "Date", "Factor", "Note"]
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
EVENT_COLUMNS = ["Exchange", "Symbol", "Date", "Gap", "Factor", "Event", "Flag", "Listed_Kind", "Listed_Factor"]

module_logger = logging.getLogger(__name__)


def market_today() -> date:
    """Today in India, whatever the machine's clock says."""
    return datetime.now(MARKET_TZ).date()


def nearest_ratio(gaps) -> np.ndarray:
    """The most common ratio within RATIO_TOLERANCE of each gap; the nearest one if none is."""
    gaps = np.clip(np.asarray(gaps, dtype=float), 1e-9, None)
    distance = np.abs(np.log(RATIOS)[None, :] - np.log(gaps)[:, None])
    score = np.where(distance < RATIO_TOLERANCE, RANKS[None, :] * 10 + distance, 1000 + distance)
    return RATIOS[score.argmin(axis=1)]


def with_gaps(raw: pd.DataFrame) -> pd.DataFrame:
    """Sorted rows with `Gap` (open over the previous close) and `_relisted` (back after a year without a trade)."""
    raw = raw.sort_values(["Symbol", "Date"]).reset_index(drop=True)
    previous = raw.groupby("Symbol")["Close"].shift(1)
    days = pd.to_datetime(raw["Date"])
    idle = (days - days.groupby(raw["Symbol"]).shift(1)).dt.days
    raw["_relisted"] = (idle > NEW_LISTING_AFTER_DAYS).to_numpy()
    raw["Gap"] = (raw["Open"] / previous).where((raw["Open"] > 0) & (previous > 0) & ~raw["_relisted"])
    raw["_priced"] = previous >= MIN_PRICE
    return raw


def _match_days(keys: pd.DataFrame, days: pd.DataFrame) -> pd.DataFrame:
    """(Symbol, Ex_Date, ...) -> the same rows with `Date`, the first trading day within LAG on or after it, or NaT."""
    left = keys.assign(Ex_Date=pd.to_datetime(keys["Ex_Date"])).sort_values("Ex_Date")
    right = days[["Symbol", "Date"]].assign(Date=pd.to_datetime(days["Date"])).sort_values("Date")
    hit = pd.merge_asof(left, right.assign(_on=right["Date"]), left_on="Ex_Date", right_on="_on", by="Symbol",
                        direction="forward", tolerance=LAG)
    return hit.drop(columns="_on")


def first_trading_day(keys: pd.DataFrame, days: pd.DataFrame) -> pd.DataFrame:
    """(Symbol, Ex_Date, ...) -> the same rows with `Date`, the symbol's first trading day within LAG on or after it."""
    if keys.empty:
        return keys.assign(Date=pd.Series(dtype="datetime64[ns]"))
    return _match_days(keys, days).dropna(subset=["Date"])


def symbols_for(actions: pd.DataFrame, securities: pd.DataFrame, exchange: str) -> pd.DataFrame:
    """Each listed action of `exchange` with the store's symbol.

    NSE: by ISIN, then by the ISIN's first nine characters (kept through a split),
    then by today's name (NSE's SME list carries no ISIN). BSE: by scrip code
    only -- a name match put one scrip's bonus on another scrip of the same name.
    """
    sec = securities[securities["Exchange"] == exchange]
    acts = actions[actions["Exchange"] == exchange].copy()
    if exchange == "NSE":
        by_isin = dict(zip(sec["ISIN"], sec["Symbol"]))
        prefix = sec.assign(_p=sec["ISIN"].str[:ISIN_SECURITY]).drop_duplicates("_p", keep=False)
        by_prefix = dict(zip(prefix["_p"], prefix["Symbol"]))
        symbol = acts["ISIN"].map(by_isin).astype(object)
        symbol = symbol.where(symbol.notna(), acts["ISIN"].str[:ISIN_SECURITY].map(by_prefix))
        known = set(sec["Symbol"])
        plain = acts["Symbol"].where(acts["Symbol"].isin(known))
        sme = (acts["Symbol"] + "_SME").where((acts["Symbol"] + "_SME").isin(known))
        symbol = symbol.where(symbol.notna(), sme)
        symbol = symbol.where(symbol.notna(), plain)
    else:
        by_code = dict(zip(sec["Code"].dropna(), sec.loc[sec["Code"].notna(), "Symbol"]))
        symbol = acts["Code"].map(by_code)
    acts["Symbol"] = symbol
    return acts.dropna(subset=["Symbol"])


def _moving_actions(actions: pd.DataFrame, securities: pd.DataFrame, exchange: str) -> pd.DataFrame:
    moving = actions["Kind"].fillna("").str.contains("|".join(LISTED_KINDS + PRICE_KINDS)) & actions["Ex_Date"].notna()
    return symbols_for(actions[moving], securities, exchange)[["Symbol", "Ex_Date", "Kind", "Factor"]]


def listed_days(actions: pd.DataFrame, securities: pd.DataFrame, raw: pd.DataFrame, exchange: str) -> pd.DataFrame:
    """(Symbol, Date, Listed_Kind, Listed_Factor, Price_Decides) from the exchange's list."""
    hits = first_trading_day(_moving_actions(actions, securities, exchange), raw)
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


def unmatched_listings(actions: pd.DataFrame, securities: pd.DataFrame, raw: pd.DataFrame, exchange: str,
                       today: date) -> pd.DataFrame:
    """Listed actions no trading day within LAG matched, as FLAGGED rows on their ex-date.

    Only an action resolved to a stored symbol, with an ex-date inside the stored
    range, at least LAG old (today and the store's last day), and a symbol that
    traded again later: its prices before and after are on different bases. A
    symbol that never traded again needs no factor.
    """
    empty = pd.DataFrame(columns=["Symbol", "Date", "Gap", "Factor", "Event", "Flag", "Listed_Kind", "Listed_Factor"])
    keys = _moving_actions(actions, securities, exchange)
    if keys.empty or raw.empty:
        return empty
    days = pd.to_datetime(raw["Date"])
    first, last = days.min(), days.max()
    keys = keys.assign(Ex_Date=pd.to_datetime(keys["Ex_Date"])).drop_duplicates(["Symbol", "Ex_Date", "Kind", "Factor"])
    keys = keys[(keys["Ex_Date"] >= first) & (keys["Ex_Date"] + LAG <= last)
                & (keys["Ex_Date"] + LAG <= pd.Timestamp(today))]
    if keys.empty:
        return empty
    missed = _match_days(keys, raw)
    missed = missed[missed["Date"].isna()]
    last_trade = days.groupby(raw["Symbol"]).max()
    missed = missed[missed["Symbol"].map(last_trade) > missed["Ex_Date"] + LAG]
    if missed.empty:
        return empty
    grouped = missed.groupby(["Symbol", "Ex_Date"])
    out = pd.DataFrame({
        "Listed_Kind": grouped["Kind"].agg(lambda k: "+".join(sorted(k))),
        "Listed_Factor": grouped["Factor"].agg(lambda f: f.dropna().prod() if f.notna().any() else np.nan),
    }).reset_index().rename(columns={"Ex_Date": "Date"})
    return out.assign(Gap=np.nan, Factor=1.0, Event=None, Flag=FLAG_NO_TRADE)[empty.columns]


def _agrees(gap: pd.Series, factor: pd.Series) -> np.ndarray:
    """The open confirms `factor`: within LIST_TOLERANCE of it, or, for a factor near 1, no gap beyond NO_GAP."""
    with np.errstate(divide="ignore", invalid="ignore"):
        near_one = np.abs(np.log(factor)) < NEAR_ONE
        return np.where(near_one, np.abs(np.log(gap)) < NO_GAP, np.abs(np.log(gap / factor)) < LIST_TOLERANCE)


def _same_ratio(listed: pd.Series, twin: pd.Series) -> np.ndarray:
    """Two factors of one event: within LIST_TOLERANCE of each other, or both within NEAR_ONE of 1."""
    with np.errstate(divide="ignore", invalid="ignore"):
        both_near_one = (np.abs(np.log(listed)) < NEAR_ONE) & (np.abs(np.log(twin)) < NEAR_ONE)
        return (np.abs(np.log(listed / twin)) < LIST_TOLERANCE) | both_near_one


def _twin_meets_listing(df: pd.DataFrame, twin: pd.DataFrame | None):
    """Match the NSE twin's factors with BSE's own listings of the same event.

    A BSE listing within TWIN_SAME_EVENT of a twin factor is the SAME event when
    its ratio matches the twin's (or it has none: a demerger the price decides).
    The factor is then applied once: on the listing's day when BSE's open there
    confirms it (NSE may have decided it on its first day back from a halt, after
    BSE's real ex-date), else on the twin's placement day, the listing becoming
    `nse_covers`. A listing whose ratio differs is another event: it keeps the
    normal list rule and is FLAGGED. Returns (twin moved where confirmed,
    nse_covers rows, differing rows), the rows as boolean Series on df.
    """
    covers = pd.Series(False, index=df.index)
    differs = pd.Series(False, index=df.index)
    listing = df["Listed_Factor"].notna() | df["Price_Decides"]
    if twin is None or twin.empty or not listing.any():
        return twin, covers, differs
    twin = twin.reset_index(drop=True)
    events = twin.assign(_event=twin.index, _bse_day=twin["Date"],
                         _nse_day=pd.to_datetime(twin["NSE_Date"]) if "NSE_Date" in twin.columns else twin["Date"])
    rows = df.loc[listing, ["Symbol", "Date", "Gap", "Listed_Factor"]].reset_index()
    pairs = rows.merge(events[["Symbol", "Factor", "_event", "_bse_day", "_nse_day"]], on="Symbol")
    pairs = pairs[((pairs["Date"] - pairs["_nse_day"]).abs() <= TWIN_SAME_EVENT)
                  | ((pairs["Date"] - pairs["_bse_day"]).abs() <= TWIN_SAME_EVENT)]
    if pairs.empty:
        return twin, covers, differs
    same = pairs["Listed_Factor"].isna() | pd.Series(_same_ratio(pairs["Listed_Factor"], pairs["Factor"]),
                                                      index=pairs.index).fillna(False).astype(bool)
    confirmed = same & pairs["Gap"].notna() & pd.Series(_agrees(pairs["Gap"], pairs["Factor"]),
                                                         index=pairs.index).astype(bool)
    # The twin's own day keeps the factor when BSE's open there shows it too (always so for a
    # factor near 1, which no open pins to a day): only a factor that day cannot explain moves.
    gap_on = df[["Symbol", "Date", "Gap"]].rename(columns={"Date": "_bse_day", "Gap": "_twin_gap"})
    pairs = pairs.merge(gap_on, on=["Symbol", "_bse_day"], how="left")
    at_twin_day = pairs["_twin_gap"].notna() & pd.Series(_agrees(pairs["_twin_gap"], pairs["Factor"]),
                                                          index=pairs.index).astype(bool)
    pairs = pairs.assign(_same=same.to_numpy(), _confirmed=confirmed.to_numpy() & ~at_twin_day.to_numpy(),
                         _distance=(pairs["Date"] - pairs["_bse_day"]).abs())
    chosen = (pairs[pairs["_confirmed"]].sort_values(["_event", "_distance"]).drop_duplicates("_event"))
    twin.loc[chosen["_event"].to_numpy(), "Date"] = chosen["Date"].to_numpy()            # applied on BSE's ex-date
    landed = set(zip(twin["Symbol"], twin["Date"]))
    same_rows = pairs[pairs["_same"]]
    covers[same_rows.loc[[(s, d) not in landed for s, d in zip(same_rows["Symbol"], same_rows["Date"])],
                         "index"].unique()] = True
    differs[pairs.loc[~pairs["index"].isin(same_rows["index"]), "index"].unique()] = True
    if not twin.empty:
        agg = {"Factor": ("Factor", "prod")}
        if "NSE_Date" in twin.columns:
            agg["NSE_Date"] = ("NSE_Date", "min")
        twin = twin.groupby(["Symbol", "Date"]).agg(**agg).reset_index()
    return twin, covers, differs


def decide(raw: pd.DataFrame, listed: pd.DataFrame, overrides: pd.DataFrame, twin: pd.DataFrame | None = None,
           covered: pd.Series | None = None) -> pd.DataFrame:
    """Factor, event and flag for every row of one exchange; `Adj` multiplies the row's prices."""
    df = with_gaps(raw)
    df["Date"] = pd.to_datetime(df["Date"])
    listed, overrides = listed.copy(), overrides.copy()
    twin = twin.copy() if twin is not None else None
    for frame in (listed, overrides) + ((twin,) if twin is not None else ()):
        frame["Date"] = pd.to_datetime(frame["Date"])
    df = df.merge(listed, on=["Symbol", "Date"], how="left")
    df = df.merge(overrides[["Symbol", "Date", "Factor"]].drop_duplicates(["Symbol", "Date"], keep="last")
                  .rename(columns={"Factor": "_override"}), on=["Symbol", "Date"], how="left")
    df["_covered"] = covered.to_numpy() if covered is not None else False
    for column in ("Listed_Factor", "_override"):                          # an empty list merges as object
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["Price_Decides"] = df["Price_Decides"].eq(True)
    twin, nse_covers, twin_differs = _twin_meets_listing(df, twin)
    df["_twin"] = np.nan
    if twin is not None and not twin.empty:                                # a left merge keeps the row order
        df = df.drop(columns="_twin").merge(twin.rename(columns={"Factor": "_twin"})[["Symbol", "Date", "_twin"]],
                                           on=["Symbol", "Date"], how="left")
        df["_twin"] = pd.to_numeric(df["_twin"], errors="coerce")
    nse_covers = pd.Series(nse_covers.to_numpy(), index=df.index)
    twin_differs = pd.Series(twin_differs.to_numpy(), index=df.index)

    gap = df["Gap"]
    with np.errstate(divide="ignore", invalid="ignore"):
        gap_off = np.abs(np.log(gap))
    priced = df["_priced"].fillna(False)
    covered_ = df["_covered"].astype(bool)
    relisted = df["_relisted"].astype(bool)
    sure = priced & ~covered_ & (gap < SURE_BELOW)
    noisy = sure.groupby(df["Symbol"]).transform("sum") > MAX_GAP_EVENTS
    sure = sure & ~noisy
    unsure = priced & ~covered_ & (((gap >= SURE_BELOW) & (gap < UNSURE_BELOW)) | (gap >= UNSURE_ABOVE)
                                   | ((gap < SURE_BELOW) & noisy))
    listed_factor = df["Listed_Factor"]
    override = df["_override"].notna()
    by_twin = df["_twin"].notna() & ~override
    nse_covers = nse_covers & ~override & ~by_twin
    by_list = listed_factor.notna() & gap.notna() & _agrees(gap, listed_factor)
    by_list = pd.Series(by_list, index=df.index).fillna(False).astype(bool) & ~nse_covers
    by_price = (df["Price_Decides"] & gap.notna() & (gap_off > 0.03) & priced
                & ~by_list & ~by_twin & ~nse_covers)
    snap = pd.Series(1.0, index=df.index)
    candidates = sure & gap.notna()
    snap[candidates] = nearest_ratio(gap[candidates].to_numpy())

    conditions = [override, by_twin, by_list, by_price, nse_covers, sure]
    factor = np.select(conditions, [df["_override"], df["_twin"], listed_factor, gap, 1.0, snap], default=1.0)
    event = np.select(conditions, ["override", "nse", "list", "list_price", "nse_covers", "gap"], default=None)
    explained = override | by_twin | by_list | by_price | nse_covers
    twin_unconfirmed = by_twin & gap.notna() & ~pd.Series(_agrees(gap, df["_twin"]), index=df.index).astype(bool)
    df["Factor"] = factor.astype(float)
    df["Event"] = event
    df["Flag"] = np.select([unsure & ~explained, twin_differs & ~override, twin_unconfirmed,
                            relisted & ~(override | by_twin)],
                           [FLAG_GAP, FLAG_TWIN_DIFFERS, FLAG_TWIN, FLAG_NEW_LISTING], default=None)
    # Each row is multiplied by the product of the factors on LATER days of its symbol.
    with np.errstate(divide="ignore", invalid="ignore"):      # a bad factor is refused in rebuild (check_factors)
        log_f = np.log(df["Factor"])
        after = log_f.groupby(df["Symbol"]).transform("sum") - log_f.groupby(df["Symbol"]).cumsum()
        df["Adj"] = np.exp(after)
    return df.drop(columns=["_priced", "_override", "_twin", "_covered", "_relisted"])


def twin_inputs(bse_raw: pd.DataFrame, nse_decided: pd.DataFrame, securities: pd.DataFrame):
    """(twin factors on BSE days, covered flags aligned with with_gaps(bse_raw)) from NSE's decisions.

    A factor NSE decided lands on the BSE twin's first trading day on or after
    NSE's day, with no limit on how much later: the shares are the same, so every
    earlier BSE row is before the event (`NSE_Date` keeps NSE's day). A BSE series
    whose first stored day is after NSE's day gets none: it has no earlier row.
    """
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
    events = events.rename(columns={"Symbol": "Twin", "Date": "NSE_Date"})
    events["NSE_Date"] = pd.to_datetime(events["NSE_Date"])
    days = rows.dropna(subset=["Twin"])
    twin = pd.DataFrame(columns=["Symbol", "Date", "Factor", "NSE_Date"])
    if not events.empty and not days.empty:
        pairs = events.merge(days[["Symbol", "Twin"]].drop_duplicates(), on="Twin")       # one row per BSE symbol
        if not pairs.empty:
            hit = pd.merge_asof(pairs.sort_values("NSE_Date"),
                                days[["Symbol", "Date"]].assign(_on=lambda d: d["Date"]).sort_values("_on"),
                                left_on="NSE_Date", right_on="_on", by="Symbol", direction="forward")
            # A BSE series that starts after NSE's day has no row before the event: nothing to scale.
            first_bse = days.groupby("Symbol")["Date"].min()
            hit = hit[hit["Date"].notna() & (hit["Symbol"].map(first_bse) < hit["NSE_Date"])]
            twin = (hit.groupby(["Symbol", "Date"]).agg(Factor=("Factor", "prod"), NSE_Date=("NSE_Date", "min"))
                       .reset_index())
    nse_days = nse_decided[["Symbol", "Date"]].rename(columns={"Symbol": "Twin", "Date": "NSE_Date"})
    nse_days = nse_days.assign(NSE_Date=pd.to_datetime(nse_days["NSE_Date"]))
    probe = rows.reset_index().dropna(subset=["Twin"]).sort_values("Date")
    covered = pd.Series(False, index=rows.index)
    if not probe.empty and not nse_days.empty:
        hit = pd.merge_asof(probe, nse_days.sort_values("NSE_Date").assign(_on=lambda d: d["NSE_Date"]),
                            left_on="Date", right_on="_on", by="Twin", direction="backward", tolerance=LAG)
        covered[hit.loc[hit["NSE_Date"].notna(), "index"].to_numpy()] = True
    return twin, covered


def read_overrides(path: Path | None, logger: logging.Logger | None = None) -> pd.DataFrame:
    """The valid rows of the overrides file; every other row is skipped with one warning saying why."""
    logger = logger or module_logger
    empty = pd.DataFrame(columns=OVERRIDE_COLUMNS)
    if path is None or not Path(path).is_file():
        return empty
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, ValueError) as error:
        logger.warning(f"Corporate-action overrides unreadable ({path}: {error}); none used.")
        return empty
    frame = frame.rename(columns=lambda c: str(c).strip())
    missing = [c for c in OVERRIDE_COLUMNS[:4] if c not in frame.columns]
    if missing:
        logger.warning(f"Corporate-action overrides {path} lack columns {missing}; none used.")
        return empty
    if "Note" not in frame.columns:
        frame["Note"] = ""
    kept = []
    for number, row in frame.iterrows():
        exchange = str(row["Exchange"]).strip().upper()
        symbol = str(row["Symbol"]).strip().upper()
        text = str(row["Date"]).strip()
        # ISO only: without a format 01/02/2026 reads as 2 January, which a user in India means as 1 February.
        day = (pd.to_datetime(text, format="%Y-%m-%d", errors="coerce") if ISO_DATE.fullmatch(text)
               else pd.NaT)
        factor = pd.to_numeric(str(row["Factor"]).strip(), errors="coerce")
        if exchange not in ("NSE", "BSE"):
            reason = "Exchange must be NSE or BSE"
        elif not symbol:
            reason = "Symbol is empty"
        elif pd.isna(day):
            reason = "Date must be a real date written YYYY-MM-DD"
        elif not np.isfinite(factor):
            reason = "Factor is not a plain finite number"
        elif not MIN_OVERRIDE <= factor <= MAX_OVERRIDE:
            reason = f"Factor must be from {MIN_OVERRIDE} to {MAX_OVERRIDE:g}"
        else:
            kept.append({"Exchange": exchange, "Symbol": symbol, "Date": day, "Factor": float(factor),
                         "Note": str(row["Note"])})
            continue
        logger.warning(f"Corporate-action override skipped (line {number + 2}: {row.to_dict()}): {reason}.")
    return pd.DataFrame(kept, columns=OVERRIDE_COLUMNS) if kept else empty


def warn_unmatched_overrides(overrides: pd.DataFrame, raw: pd.DataFrame, logger: logging.Logger) -> int:
    """Warn for each valid override that matches no stored row (exchange, symbol, trading day); returns how many."""
    if overrides.empty:
        return 0
    stored = raw[["Exchange", "Symbol", "Date"]].assign(Date=pd.to_datetime(raw["Date"]), _stored=True)
    probe = overrides.assign(Date=pd.to_datetime(overrides["Date"])).merge(
        stored.drop_duplicates(["Exchange", "Symbol", "Date"]), on=["Exchange", "Symbol", "Date"], how="left")
    unmatched = probe[probe["_stored"].isna()]
    for row in unmatched.itertuples():
        logger.warning(f"Corporate-action override {row.Exchange} {row.Symbol} {row.Date:%Y-%m-%d} (factor "
                       f"{row.Factor:g}) matches no stored row -- not a trading day of that symbol, or no such "
                       "symbol; not used.")
    return len(unmatched)


def check_factors(events: pd.DataFrame, periods: pd.DataFrame) -> None:
    """Raise unless every applied factor and period multiplier is finite and above 0."""
    applied = events[events["Event"].notna()]
    factor = pd.to_numeric(applied["Factor"], errors="coerce")
    bad = applied[~np.isfinite(factor) | (factor <= 0)]
    adj = pd.to_numeric(periods["Adj"], errors="coerce")
    if not bad.empty or not (np.isfinite(adj) & (adj > 0)).all():
        raise ValueError("refusing to write corporate-action factors that are not finite and above 0: "
                         f"{bad[['Exchange', 'Symbol', 'Date', 'Event', 'Factor']].head(5).to_dict('records')}")


def rebuild(conn, overrides_path: Path | None = None, review_path: Path | None = None, today: date | None = None,
            logger: logging.Logger | None = None) -> dict:
    """Decide every factor from what is stored; rewrite `adjustment_periods` and `price_adjustments` in one transaction."""
    logger = logger or module_logger
    today = today or market_today()
    raw = conn.execute("SELECT * FROM raw_prices").df()
    securities = conn.execute("SELECT Exchange, Symbol, ISIN, Series, Code FROM securities").df()
    actions = conn.execute("SELECT * FROM exchange_actions").df()
    overrides = read_overrides(overrides_path, logger)
    unmatched_overrides = warn_unmatched_overrides(overrides, raw, logger)
    decided, unmatched = {}, []
    for exchange in ("NSE", "BSE"):
        part = raw[raw["Exchange"] == exchange].drop(columns="Exchange")
        listed = listed_days(actions, securities, part, exchange)
        mine = overrides[overrides["Exchange"] == exchange]
        twin, covered = (twin_inputs(part, decided["NSE"], securities) if exchange == "BSE" and "NSE" in decided
                         else (None, None))
        decided[exchange] = decide(part, listed, mine, twin, covered).assign(Exchange=exchange)
        missed = unmatched_listings(actions, securities, part, exchange, today)
        if not missed.empty:
            unmatched.append(missed.assign(Exchange=exchange))
    frames = [d for d in decided.values() if not d.empty]
    if not frames:                                                   # an empty store: nothing to decide
        frames = [pd.DataFrame(columns=EVENT_COLUMNS)]
    both = pd.concat(frames, ignore_index=True)
    events = both[both["Event"].notna() | both["Flag"].notna() | both["Listed_Kind"].notna()][EVENT_COLUMNS]
    if unmatched:
        events = pd.concat([events] + [m[EVENT_COLUMNS] for m in unmatched], ignore_index=True)
    events = events.assign(Date=pd.to_datetime(events["Date"]).dt.date)
    periods = adjustment_periods(events)
    check_factors(events, periods)                                   # before anything is written
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
    review = review_list(events, today, securities)
    if review_path is not None:
        review.to_csv(review_path, index=False)
    applied = events[events["Event"].notna() & (events["Factor"] != 1.0)]
    factors = {f"{exchange} {event}": int(n) for (exchange, event), n in applied.groupby(["Exchange", "Event"]).size().items()}
    return {"rows": len(both), "factors": factors, "flagged": int(events["Flag"].notna().sum()),
            "review_rows": len(review), "overrides_unmatched": unmatched_overrides}


def adjustment_periods(events: pd.DataFrame) -> pd.DataFrame:
    """(Exchange, Symbol, From_Date, Adj): from each factor's day on, the product of the symbol's later factors.

    The first period starts on 1900-01-01 and holds the product of all of them;
    the last starts on the last factor's day and holds 1.
    """
    applied = events[events["Event"].notna() & (events["Factor"] != 1.0)][["Exchange", "Symbol", "Date", "Factor"]]
    rows = []
    for (exchange, symbol), part in applied.sort_values("Date").groupby(["Exchange", "Symbol"]):
        factors, days = part["Factor"].to_numpy(dtype=float), list(part["Date"])
        later = np.cumprod(factors[::-1])[::-1]                # product of this and every later factor
        rows.append((exchange, symbol, date(1900, 1, 1), float(later[0])))
        for i, day in enumerate(days):
            rows.append((exchange, symbol, day, float(later[i + 1]) if i + 1 < len(days) else 1.0))
    return pd.DataFrame(rows, columns=["Exchange", "Symbol", "From_Date", "Adj"])


def is_equity(isin: pd.Series) -> pd.Series:
    """An Indian equity share: issuer type E or 9 and security-type digits 01 (bonds, NCDs and funds are not)."""
    isin = isin.fillna("").astype(str)
    return isin.str.match(r"^IN[E9]") & (isin.str[7:9] == "01")


def review_list(events: pd.DataFrame, today: date, securities: pd.DataFrame | None = None) -> pd.DataFrame:
    """The last 30 days' flags (unexplained gaps, new listings, unconfirmed twin factors, listings no
    trading day matched) and listed factors the price did not confirm, on shares.

    Bonds and rights entitlements gap all the time and the screener never reads
    them (on 2026-10-01, 13 of 14 lines were BSE bonds and one -RE), so they are
    left out of the list -- not out of the decisions.
    """
    since = pd.Timestamp(today) - pd.Timedelta(days=REVIEW_DAYS)
    recent = events[pd.to_datetime(events["Date"]) >= since]
    if securities is not None:
        shares = securities[is_equity(securities["ISIN"]) & ~securities["Symbol"].str.contains(r"-RE\d*(?:_SME)?$")]
        recent = recent.merge(shares[["Exchange", "Symbol"]], on=["Exchange", "Symbol"])
    unconfirmed = recent["Listed_Factor"].notna() & ~recent["Event"].isin(["list", "nse", "override", "nse_covers"])
    return recent[recent["Flag"].notna() | unconfirmed].sort_values(["Date", "Exchange", "Symbol"])


def refresh(conn, nse, logger: logging.Logger, overrides_path: Path | None = None, review_path: Path | None = None,
            today: date | None = None, list_start: date | None = None) -> dict:
    """After a sync: fetch the lists around today (or from `list_start`) and, when due, the whole retained
    history; then decide and rebuild. `today` is the market's date (IST)."""
    today = today or market_today()
    start, end = action_lists.window(today)
    lists = action_lists.refresh(conn, nse, list_start or start, end, logger)
    history = action_lists.refresh_history_if_due(conn, nse, today, end, logger)
    summary = rebuild(conn, overrides_path, review_path, today, logger)
    logger.info(f"Prices rebuilt from raw: {summary}")
    if summary["review_rows"]:
        logger.warning(f"{summary['review_rows']} recent price gaps need a look: {review_path}")
    return {"lists": lists, "history": history, **summary}
