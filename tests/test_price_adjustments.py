"""Raw prices, the exchanges' lists and the factors decided from them -- on data made by hand, no network."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd
import pytest

import action_lists
import price_adjustments as pa
from action_factors import parse_purpose
from eod_store import apply_retention, create_schema, schema_version, upsert_daily_prices

DAYS = pd.bdate_range("2025-04-01", periods=40)


def raw(symbol, closes, opens=None, days=DAYS):
    opens = opens or closes
    n = len(closes)
    return pd.DataFrame({"Symbol": symbol, "Date": days[:n], "Open": [float(o) for o in opens],
                         "High": [float(max(o, c)) for o, c in zip(opens, closes)],
                         "Low": [float(min(o, c)) for o, c in zip(opens, closes)], "Close": [float(c) for c in closes],
                         "Volume": 1000, "Total_Trades": 10, "Dlv_Qty": 400})


def listed(symbol, day, kind, factor, price_decides=False):
    return pd.DataFrame({"Symbol": [symbol], "Date": [DAYS[day]], "Listed_Kind": [kind],
                         "Listed_Factor": [factor], "Price_Decides": [price_decides]})


NO_LIST = pd.DataFrame(columns=["Symbol", "Date", "Listed_Kind", "Listed_Factor", "Price_Decides"])
NO_OVERRIDES = pd.DataFrame(columns=pa.OVERRIDE_COLUMNS)


def events(out):
    return out[out["Event"].notna()][["Event", "Factor"]].values.tolist()


@pytest.mark.parametrize("text, kind, factor", [
    ("Bonus 10:1", "bonus", 1 / 11),
    ("Face Value Split (Sub-Division) - From Rs 5/- Per Share To Rs 2/- Per Share", "split", 0.4),
    ("Stock  Split From Rs.10/- to Rs.5/-", "split", 0.5),
    ("Bonus 1:1 / Face Value Split From Rs.4/- To Rs.2/-", "bonus+split", 0.25),
    ("Consolidation of shares From Rs 1/- Per Share To Rs 10/- Per Share", "consolidation", 10.0),
    ("Consolidation of Shares ", "consolidation", None),
    ("Demerger", "demerger", None),
    ("Spin Off ", "demerger", None),
    ("Scheme Of Arrangement - Bonus Ncrps 4:1", "scheme", None),
    ("Dividend - Re 0.40 Per Sh", "dividend", None),
])
def test_the_factor_is_read_from_the_purpose(text, kind, factor):
    got_kind, got_factor, _ = parse_purpose(text)
    assert got_kind == kind
    assert got_factor == (pytest.approx(factor) if factor is not None else None)


def test_a_listed_split_the_open_confirms_is_taken_exactly_and_one_it_does_not_show_is_not():
    out = pa.decide(raw("SPLT", [100] * 20 + [21] * 20), listed("SPLT", 20, "split", 0.2), NO_OVERRIDES)
    assert events(out) == [["list", pytest.approx(0.2)]] and out["Adj"].iloc[0] == pytest.approx(0.2)
    flat = pa.decide(raw("ELSE", [100] * 40), listed("ELSE", 20, "split", 0.2), NO_OVERRIDES)
    assert events(flat) == [] and (flat["Adj"] == 1).all()


def test_a_bonus_too_small_for_the_price_to_show_comes_from_the_list_and_a_list_resolves_a_flag():
    out = pa.decide(raw("SMALL", [100] * 20 + [93] * 20), listed("SMALL", 20, "bonus", 10 / 11), NO_OVERRIDES)
    assert events(out) == [["list", pytest.approx(10 / 11)]]
    alone = pa.decide(raw("THIRD", [90] * 20 + [61] * 20), NO_LIST.copy(), NO_OVERRIDES)
    assert alone["Flag"].notna().sum() == 1 and events(alone) == []          # 0.68: flagged without the list
    out = pa.decide(raw("THIRD", [90] * 20 + [61] * 20), listed("THIRD", 20, "bonus", 2 / 3), NO_OVERRIDES)
    assert out["Flag"].isna().all() and events(out) == [["list", pytest.approx(2 / 3)]]


def test_a_listed_demerger_takes_the_open_over_the_previous_close():
    prices = raw("SPUN", [1000] * 20 + [230] * 20, opens=[1000] * 20 + [225] + [230] * 19)
    out = pa.decide(prices, listed("SPUN", 20, "demerger", np.nan, price_decides=True), NO_OVERRIDES)
    assert events(out) == [["list_price", pytest.approx(0.225)]]


def test_an_unlisted_halving_is_snapped_a_middle_gap_flagged_a_hand_override_wins():
    out = pa.decide(raw("GONE", [300] * 20 + [101] * 20), NO_LIST.copy(), NO_OVERRIDES)
    assert events(out) == [["gap", pytest.approx(1 / 3)]]
    hand = pd.DataFrame({"Exchange": ["NSE"], "Symbol": ["GONE"], "Date": [DAYS[20]], "Factor": [1.0], "Note": ["crash"]})
    out = pa.decide(raw("GONE", [300] * 20 + [101] * 20), NO_LIST.copy(), hand)
    assert events(out) == [["override", 1.0]] and (out["Adj"] == 1).all()


def test_volume_and_delivery_scale_like_prices_and_rows_after_the_event_stay_raw():
    out = pa.decide(raw("KEPT", [100] * 20 + [50] * 20), listed("KEPT", 20, "bonus", 0.5), NO_OVERRIDES)
    assert out["Adj"].tolist() == pytest.approx([0.5] * 20 + [1.0] * 20)


def store(tmp_path):
    conn = duckdb.connect(str(tmp_path / "prices.duckdb"))
    create_schema(conn)
    return conn


def day_rows(exchange, symbol, isin, days, closes, opens=None, code=None, series="EQ"):
    opens = opens or closes
    return pd.DataFrame({"Date": days, "Symbol": symbol, "Exchange": exchange, "ISIN": isin, "Series": series,
                         "Code": code, "Open": opens, "High": closes, "Low": closes, "Close": closes,
                         "Volume": 1000, "Total_Trades": 10, "Dlv_Qty": 600})


def test_the_rebuild_adjusts_from_raw_and_running_or_replaying_it_again_changes_nothing(tmp_path):
    conn = store(tmp_path)
    days = list(DAYS[:30])
    nse = day_rows("NSE", "POCL", "INE063E01061", days, [1400.0] * 20 + [560.0] * 10, [1400.0] * 20 + [547.0] + [560.0] * 9)
    for day, part in nse.groupby("Date"):
        upsert_daily_prices(conn, part)
    action_lists.store(conn, action_lists.nse_rows([{
        "symbol": "POCL", "series": "EQ", "isin": "INE063E01061", "comp": "Pondy Oxides", "exDate": DAYS[20].strftime("%d-%b-%Y"),
        "subject": "Face Value Split (Sub-Division) - From Rs 5/- Per Share To Rs 2/- Per Share"}], "equities"))
    pa.rebuild(conn)
    first = conn.execute("SELECT Date, Close, Volume, Dlv_Qty FROM eod_prices ORDER BY Date").fetchall()
    assert first[0][1:] == (pytest.approx(560.0), 2500, 1500)            # 1400 x 0.4; shares / 0.4
    for _ in range(2):                                                    # the old store divided again on a replay
        upsert_daily_prices(conn, nse[nse["Date"] == DAYS[21]])
        pa.rebuild(conn)
    assert conn.execute("SELECT Date, Close, Volume, Dlv_Qty FROM eod_prices ORDER BY Date").fetchall() == first
    assert conn.execute("SELECT Event, Factor FROM price_adjustments WHERE Event IS NOT NULL").fetchall() == [("list", pytest.approx(0.4))]
    view = conn.execute("SELECT * FROM eod_data ORDER BY Date LIMIT 1").fetchone()
    assert view[3] == "INE063E01061" and view[10] == 10 and view[12] == 1500        # ISIN, Total_Trades, Dlv_Qty
    conn.close()


def test_bse_takes_its_nse_twins_factor_and_keeps_its_own_list_by_scrip_code(tmp_path):
    conn = store(tmp_path)
    days = list(DAYS[:30])
    upsert_daily_prices(conn, day_rows("NSE", "ACUTAAS", "INE00FF01025", days, [2200.0] * 20 + [1068.0] * 10))
    # BSE did not trade it on the ex-date: NSE's factor lands on BSE's next day
    bse_days = days[:20] + days[21:]
    upsert_daily_prices(conn, day_rows("BSE", "ACUTAAS", "INE00FF01025", bse_days, [2201.0] * 20 + [1070.0] * 9,
                                       code="543349"))
    upsert_daily_prices(conn, day_rows("BSE", "ONLYBSE", "INE999Z01011", days, [300.0] * 20 + [101.0] * 10, code="590001"))
    action_lists.store(conn, action_lists.nse_rows([{
        "symbol": "AMIORG", "series": "EQ", "isin": "INE00FF01025", "comp": "Ami Organics", "exDate": DAYS[20].strftime("%d-%b-%Y"),
        "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share"}], "equities"))
    action_lists.store(conn, action_lists.bse_rows(
        "Security Code,Security Name,Company Name,Ex Date,Purpose,Record Date\n"
        f"590001,RENAMEDNOW,Only Bse Ltd,{DAYS[20]:%d %b %Y},Bonus issue 2:1,{DAYS[20]:%d %b %Y}\n"))
    pa.rebuild(conn)
    got = conn.execute("SELECT Exchange, Symbol, Event, Factor FROM price_adjustments WHERE Event IS NOT NULL "
                       "ORDER BY Exchange, Symbol").fetchall()
    assert got == [("BSE", "ACUTAAS", "nse", pytest.approx(0.5)), ("BSE", "ONLYBSE", "list", pytest.approx(1 / 3)),
                   ("NSE", "ACUTAAS", "list", pytest.approx(0.5))]
    closes = conn.execute("SELECT Exchange, Close FROM eod_prices WHERE Symbol = 'ACUTAAS' AND Date = ?",
                          [DAYS[0].date()]).fetchall()
    assert sorted(closes) == [("BSE", pytest.approx(1100.5)), ("NSE", pytest.approx(1100.0))]
    conn.close()


def test_a_bse_gap_its_nse_twin_never_had_is_not_taken_while_the_twin_trades():
    nse_decided = pa.decide(raw("KESAR", [500] * 40), NO_LIST.copy(), NO_OVERRIDES)
    bse = raw("KESAR", [500] * 15 + [240] * 25)                  # a thin-trading 0.48 open on BSE only
    securities = pd.DataFrame({"Exchange": ["NSE", "BSE"], "Symbol": ["KESAR", "KESAR"],
                               "ISIN": ["INE000A01011"] * 2, "Series": ["EQ", "B"], "Code": [None, "507180"]})
    twin, covered = pa.twin_inputs(bse, nse_decided.assign(Date=pd.to_datetime(nse_decided["Date"])), securities)
    out = pa.decide(bse, NO_LIST.copy(), NO_OVERRIDES, twin, covered)
    assert events(out) == [] and out["Flag"].isna().all()


def test_the_store_keeps_raw_days_moves_history_on_a_rename_and_keeps_two_years(tmp_path):
    conn = store(tmp_path)
    upsert_daily_prices(conn, day_rows("NSE", "AMIORG", "INE00FF01025", list(DAYS[:3]), [10.0, 11.0, 12.0]))
    upsert_daily_prices(conn, day_rows("NSE", "ACUTAAS", "INE00FF01025", [DAYS[3]], [13.0]))
    assert conn.execute("SELECT DISTINCT Symbol FROM raw_prices").fetchall() == [("ACUTAAS",)]
    upsert_daily_prices(conn, day_rows("NSE", "ACUTAAS", "INE00FF01025", [DAYS[3]], [13.5]))       # a re-sent day
    assert conn.execute("SELECT count(*), max(Close) FROM raw_prices").fetchone() == (4, 13.5)
    upsert_daily_prices(conn, day_rows("NSE", "ACUTAAS", "INE00FF01025", [DAYS[3] + timedelta(days=800)], [20.0]))
    assert apply_retention(conn, 2) == 4 and conn.execute("SELECT count(*) FROM raw_prices").fetchone()[0] == 1
    conn.close()


def test_an_old_in_place_store_is_recognised_and_not_written_into(tmp_path):
    conn = duckdb.connect(str(tmp_path / "old.duckdb"))
    conn.execute("CREATE TABLE eod_prices (Date DATE, Symbol VARCHAR, Exchange VARCHAR, Close DOUBLE)")
    assert schema_version(conn) == 1
    with pytest.raises(RuntimeError, match="rebuild_eod_database"):
        create_schema(conn)
    conn.close()


def test_a_list_an_exchange_refuses_is_logged_and_the_stored_one_used(tmp_path, caplog):
    conn = store(tmp_path)

    class Refused:
        status_code, text = 403, "<HTML>Access Denied</HTML>"

    with caplog.at_level(logging.WARNING):
        result = action_lists.refresh(conn, None, date(2026, 9, 1), date(2026, 10, 1), logging.getLogger("t"),
                                      get=lambda *a, **k: Refused())
    assert result["BSE"].startswith("failed") and "unavailable" in caplog.text
    conn.close()


def test_the_review_list_holds_shares_only():
    events = pd.DataFrame({"Exchange": ["BSE", "BSE", "NSE"], "Symbol": ["1175MBFL27", "RATNA-RE", "TURTLEMINT"],
                           "Date": [date(2026, 9, 28)] * 3, "Gap": [0.8, 0.6, 0.8], "Factor": [1.0] * 3,
                           "Event": [None] * 3, "Flag": ["unresolved gap"] * 3, "Listed_Kind": [None] * 3,
                           "Listed_Factor": [np.nan] * 3})
    securities = pd.DataFrame({"Exchange": ["BSE", "BSE", "NSE"], "Symbol": ["1175MBFL27", "RATNA-RE", "TURTLEMINT"],
                               "ISIN": ["INE721A07RM6", "INE703A20015", "INE0ABC01016"]})
    out = pa.review_list(events, date(2026, 10, 1), securities)
    assert out["Symbol"].tolist() == ["TURTLEMINT"]


# --- 1. hand overrides are validated ------------------------------------------------------------

def write_overrides(path, rows):
    path.write_text("Exchange,Symbol,Date,Factor,Note\n" + "".join(f"{r}\n" for r in rows), encoding="utf-8")
    return path


def test_override_rows_that_are_not_plain_sane_numbers_are_skipped_one_warning_each(tmp_path, caplog):
    path = write_overrides(tmp_path / "overrides.csv", [
        "NSE,GOOD,2025-04-21,0.5,split 1:2",
        "NSE,FRACTION,2025-04-21,1/2,", "NSE,PERCENT,2025-04-21,50%,", "NSE,BLANK,2025-04-21,,",
        "NSE,SUFFIX,2025-04-21,1.0x,", "NSE,ZERO,2025-04-21,0,", "NSE,NEGATIVE,2025-04-21,-0.5,",
        "NSE,INFINITE,2025-04-21,inf,", "NSE,HUGE,2025-04-21,5000,", "NSE,BADDATE,2025-13-45,0.5,",
        "NYSE,ELSEWHERE,2025-04-21,0.5,", " bse , lower ,2025-04-21, 2 ,consolidation"])
    with caplog.at_level(logging.WARNING):
        got = pa.read_overrides(path, logging.getLogger("t"))
    assert got[["Exchange", "Symbol", "Factor"]].values.tolist() == [["NSE", "GOOD", 0.5], ["BSE", "LOWER", 2.0]]
    skipped = [r for r in caplog.records if "override skipped" in r.getMessage()]
    assert len(skipped) == 10
    assert "FRACTION" in skipped[0].getMessage() and "not a plain finite number" in skipped[0].getMessage()
    assert "from 0.001 to 1000" in skipped[4].getMessage()                     # ZERO
    assert "YYYY-MM-DD" in skipped[8].getMessage() and "NSE or BSE" in skipped[9].getMessage()


def test_an_override_date_must_be_written_yyyy_mm_dd(tmp_path, caplog):
    # Without a format 01/02/2026 reads as 2 January; the user in India meant 1 February.
    path = write_overrides(tmp_path / "overrides.csv", [
        "NSE,ISO,2026-02-01,0.5,", "NSE,DAYFIRST,01/02/2026,0.5,", "NSE,DOTTED,01.02.2026,0.5,",
        "NSE,UNPADDED,2026-2-1,0.5,", "NSE,NOSUCHDAY,2026-02-30,0.5,", "NSE,WITHTIME,2026-02-01 10:00,0.5,"])
    with caplog.at_level(logging.WARNING):
        got = pa.read_overrides(path, logging.getLogger("t"))
    assert got[["Symbol", "Date"]].values.tolist() == [["ISO", pd.Timestamp("2026-02-01")]]
    skipped = [r.getMessage() for r in caplog.records if "override skipped" in r.getMessage()]
    assert len(skipped) == 5 and all("YYYY-MM-DD" in m for m in skipped)


def pocl_store(tmp_path):
    conn = store(tmp_path)
    days = list(DAYS[:30])
    nse = day_rows("NSE", "POCL", "INE063E01061", days, [1400.0] * 20 + [560.0] * 10)
    upsert_daily_prices(conn, nse)
    return conn


def test_a_zero_or_negative_factor_is_never_committed_and_eod_prices_stays_readable(tmp_path, monkeypatch):
    conn = pocl_store(tmp_path)
    path = write_overrides(tmp_path / "overrides.csv", [f"NSE,POCL,{DAYS[20]:%Y-%m-%d},0,typo",
                                                        f"NSE,POCL,{DAYS[5]:%Y-%m-%d},-2,typo"])
    pa.rebuild(conn, path)                                                 # both rows skipped: the gap rule decides
    assert conn.execute("SELECT Event, Factor FROM price_adjustments WHERE Event IS NOT NULL").fetchall() == [
        ("gap", pytest.approx(0.4))]
    before = conn.execute("SELECT * FROM adjustment_periods ORDER BY From_Date").fetchall()
    conn.execute("SELECT * FROM eod_data").fetchall()                      # no ConversionException
    # Defense in depth: a zero that reaches the decision anyway is refused before anything is written.
    zero = pd.DataFrame({"Exchange": ["NSE"], "Symbol": ["POCL"], "Date": [DAYS[20]], "Factor": [0.0], "Note": [""]})
    monkeypatch.setattr(pa, "read_overrides", lambda *a, **k: zero)
    with pytest.raises(ValueError, match="finite and above 0"):
        pa.rebuild(conn, path)
    assert conn.execute("SELECT * FROM adjustment_periods ORDER BY From_Date").fetchall() == before
    assert len(conn.execute("SELECT * FROM eod_data").fetchall()) == 30
    conn.close()


def test_a_valid_override_that_matches_no_stored_row_is_warned(tmp_path, caplog):
    conn = pocl_store(tmp_path)
    saturday = DAYS[20] + timedelta(days=(5 - DAYS[20].weekday()) % 7)
    path = write_overrides(tmp_path / "overrides.csv", [f"NSE,POCL,{saturday:%Y-%m-%d},0.4,weekend",
                                                        f"NSE,POCLL,{DAYS[20]:%Y-%m-%d},0.4,typo",
                                                        f"BSE,POCL,{DAYS[20]:%Y-%m-%d},0.4,not on BSE",
                                                        f"NSE,POCL,{DAYS[20]:%Y-%m-%d},0.4,good"])
    with caplog.at_level(logging.WARNING):
        summary = pa.rebuild(conn, path, logger=logging.getLogger("t"))
    assert summary["overrides_unmatched"] == 3
    unmatched = [r.getMessage() for r in caplog.records if "matches no stored row" in r.getMessage()]
    assert len(unmatched) == 3 and any("POCLL" in m for m in unmatched)
    assert conn.execute("SELECT Event FROM price_adjustments WHERE Event IS NOT NULL").fetchall() == [("override",)]
    conn.close()


# --- 2. a successful list fetch replaces what it covered; a failed or empty one deletes nothing -----

class ListNSE:
    """An NSE session whose equity list is `records` (or raises `error`)."""

    def __init__(self, records=(), error=None):
        self.records, self.error = list(records), error

    def actions(self, segment, from_date, to_date):
        if self.error:
            raise self.error
        if segment != "equities":
            return []
        return [r for r in self.records if from_date <= datetime.strptime(r["exDate"], "%d-%b-%Y") <= to_date]


def bonus(day, text, symbol="SMALLBON", isin="INE111A01011"):
    return {"symbol": symbol, "series": "EQ", "isin": isin, "comp": "Small Bonus", "exDate": f"{day:%d-%b-%Y}",
            "subject": text}


def empty_bse(*args, **kwargs):
    class Answer:
        status_code, text = 200, "Security Code,Security Name,Company Name,Ex Date,Purpose,Record Date\n"
    return Answer()


def adjusted_first_close(conn, symbol):
    return conn.execute("SELECT Close FROM eod_prices WHERE Symbol = ? ORDER BY Date LIMIT 1", [symbol]).fetchone()[0]


def test_a_revised_ex_date_replaces_the_stale_row_so_a_near_1_bonus_is_applied_once(tmp_path):
    conn = store(tmp_path)
    upsert_daily_prices(conn, day_rows("NSE", "SMALLBON", "INE111A01011", list(DAYS[:30]),
                                       [100.0] * 22 + [90.91] * 8))
    log, window = logging.getLogger("t"), (DAYS[0].date(), DAYS[29].date())
    action_lists.refresh(conn, ListNSE([bonus(DAYS[20], "Bonus 1:10")]), *window, log, get=empty_bse)
    action_lists.refresh(conn, ListNSE([bonus(DAYS[22], "Bonus 1:10")]), *window, log, get=empty_bse)   # revised
    pa.rebuild(conn)
    assert conn.execute("SELECT count(*) FROM exchange_actions").fetchone()[0] == 1
    assert adjusted_first_close(conn, "SMALLBON") == pytest.approx(90.91, abs=0.01)    # once, not 82.64
    conn.close()


def test_a_corrected_ratio_replaces_the_stale_one_instead_of_multiplying_with_it(tmp_path):
    conn = store(tmp_path)
    upsert_daily_prices(conn, day_rows("NSE", "SMALLBON", "INE111A01011", list(DAYS[:30]),
                                       [300.0] * 20 + [200.0] * 10))
    log, window = logging.getLogger("t"), (DAYS[0].date(), DAYS[29].date())
    action_lists.refresh(conn, ListNSE([bonus(DAYS[20], "Bonus 1:3")]), *window, log, get=empty_bse)
    result = action_lists.refresh(conn, ListNSE([bonus(DAYS[20], "Bonus 1:2")]), *window, log, get=empty_bse)
    assert "1 no longer listed" in result["NSE"]
    pa.rebuild(conn)
    # 3/4 x 2/3 = 1/2 disagreed with the 0.67 open, too small a gap for the gap rule: the bonus was lost
    assert conn.execute("SELECT Event, Factor, Flag FROM price_adjustments").fetchall() == [
        ("list", pytest.approx(2 / 3), None)]
    conn.close()


def test_a_failed_or_empty_list_fetch_deletes_nothing(tmp_path):
    conn = store(tmp_path)
    log, window = logging.getLogger("t"), (DAYS[0].date(), DAYS[29].date())
    action_lists.refresh(conn, ListNSE([bonus(DAYS[20], "Bonus 1:1")]), *window, log, get=empty_bse)
    action_lists.store(conn, action_lists.bse_rows(
        "Security Code,Security Name,Company Name,Ex Date,Purpose,Record Date\n"
        f"590001,ONLYBSE,Only Bse Ltd,{DAYS[20]:%d %b %Y},Bonus issue 2:1,{DAYS[20]:%d %b %Y}\n"))
    stored = conn.execute("SELECT * EXCLUDE (Fetched_At) FROM exchange_actions ORDER BY Exchange").fetchall()

    class Refused:
        status_code, text = 403, "<HTML>Access Denied</HTML>"

    for nse, get in ((ListNSE(error=ConnectionError("reset")), lambda *a, **k: Refused()),
                     (ListNSE([]), empty_bse)):                      # a failure, then an empty answer
        result = action_lists.refresh(conn, nse, *window, log, get=get)
        assert all(text.startswith(("failed", "0 listed")) for text in result.values())
    assert conn.execute("SELECT * EXCLUDE (Fetched_At) FROM exchange_actions ORDER BY Exchange").fetchall() == stored
    conn.close()


def bse_answer(*rows):
    """A BSE action-list fetcher answering `rows` (code, name, day, purpose)."""
    text = "Security Code,Security Name,Company Name,Ex Date,Purpose,Record Date\n" + "".join(
        f"{code},{name},{name} Ltd,{day:%d %b %Y},{purpose},{day:%d %b %Y}\n" for code, name, day, purpose in rows)

    def get(*args, **kwargs):
        class Answer:
            status_code = 200
        Answer.text = text
        return Answer()
    return get


def stored_bse(conn):
    return conn.execute("SELECT Code, Ex_Date, Purpose FROM exchange_actions WHERE Exchange = 'BSE' "
                        "ORDER BY Code, Ex_Date").fetchall()


def test_a_list_answer_replaces_only_the_securities_it_lists(tmp_path):
    # BSE's list leaves out scrips delisted or amalgamated since (GSPL, JBCHEPHARM...): a
    # non-empty answer is not complete, and a one-row answer deleted a stored 1:10 bonus.
    conn = store(tmp_path)
    log, window = logging.getLogger("t"), (DAYS[0].date(), DAYS[29].date())
    action_lists.refresh(conn, None, *window, log, get=bse_answer(
        ("500001", "SMALLBON", DAYS[5], "Bonus issue 1:10"), ("500002", "GONE", DAYS[6], "Amalgamation"),
        ("500003", "REVISED", DAYS[7], "Bonus issue 1:1")))
    result = action_lists.refresh(conn, None, *window, log, get=bse_answer(
        ("500003", "REVISEDNEW", DAYS[9], "Bonus issue 1:1"), ("500004", "OTHER", DAYS[8], "Dividend - Rs. - 1.0000")))
    assert "1 no longer listed" in result["BSE"]                      # REVISED's old ex-date, nothing else
    assert stored_bse(conn) == [("500001", DAYS[5].date(), "Bonus issue 1:10"),
                                ("500002", DAYS[6].date(), "Amalgamation"),
                                ("500003", DAYS[9].date(), "Bonus issue 1:1"),
                                ("500004", DAYS[8].date(), "Dividend - Rs. - 1.0000")]
    conn.close()


def test_nses_no_data_found_answer_is_an_empty_list_that_deletes_nothing(tmp_path):
    conn = store(tmp_path)
    log, window = logging.getLogger("t"), (DAYS[0].date(), DAYS[29].date())
    action_lists.refresh(conn, ListNSE([bonus(DAYS[20], "Bonus 1:1")]), *window, log, get=empty_bse)

    class NoData:
        def actions(self, segment, from_date, to_date):
            return {"msg": "no data found"}

    assert action_lists.nse_rows({"msg": "no data found"}, "equities").empty
    result = action_lists.refresh(conn, NoData(), *window, log, get=empty_bse)
    assert result["NSE"].startswith("0 listed")
    assert conn.execute("SELECT count(*) FROM exchange_actions WHERE Exchange = 'NSE'").fetchone()[0] == 1
    conn.close()


# --- 3. the whole retained history is asked again when its last full fetch is missing or old --------

def test_the_whole_list_history_is_fetched_when_due_and_recorded_per_exchange(tmp_path, caplog):
    conn = store(tmp_path)
    upsert_daily_prices(conn, day_rows("NSE", "SMALLBON", "INE111A01011", list(DAYS[:30]), [100.0] * 30))
    asked = []

    class Recording(ListNSE):
        def actions(self, segment, from_date, to_date):
            asked.append(from_date.date())
            return super().actions(segment, from_date, to_date)

    late = Recording([bonus(DAYS[3], "Bonus 1:1")])                 # listed long after its ex-date
    today = DAYS[29].date() + timedelta(days=90)

    class Refused:
        status_code, text = 403, "<HTML>Access Denied</HTML>"

    log = logging.getLogger("t")
    result = action_lists.refresh_history_if_due(conn, late, today, today + timedelta(45), log, get=lambda *a, **k: Refused())
    assert result["NSE"].startswith("1 listed") and result["BSE"].startswith("failed")
    assert min(asked) == DAYS[0].date() - timedelta(days=10)
    assert conn.execute("SELECT Exchange, Fetched_On FROM action_list_history").fetchall() == [("NSE", today)]
    # Only the exchange that failed is due the next day; nothing is due once both are recent.
    asked.clear()
    result = action_lists.refresh_history_if_due(conn, late, today + timedelta(1), today + timedelta(46), log,
                                                 get=empty_bse)
    assert set(result) == {"BSE"} and asked == []                 # an empty answer is not recorded either
    action_lists.record_history(conn, "BSE", DAYS[0].date(), today, today + timedelta(1))
    assert action_lists.refresh_history_if_due(conn, late, today + timedelta(2), today, log, get=empty_bse) == {}
    assert action_lists.history_due(conn, "NSE", today + timedelta(30))
    # A refresh that blows up is logged and returned, never raised.
    with caplog.at_level(logging.WARNING):
        broken = action_lists.refresh_history_if_due(None, late, today, today, log)
    assert "failed" in broken and "history refresh failed" in caplog.text
    conn.close()


# --- 4. a symbol back after more than a year without a trade is a new listing ---------------------

def test_a_return_after_more_than_366_days_without_a_trade_is_a_new_listing_not_a_split():
    # BSE MPFSL: 96.09 until 2024-12-02, then 2.49 on 2026-07-27 after a resolution plan -- was "split" 1/50.
    days = list(pd.bdate_range("2024-11-01", "2024-12-02")) + list(pd.bdate_range("2026-07-27", periods=10))
    prices = raw("MPFSL", [96.09] * 22 + [2.49] * 10, days=days)
    out = pa.decide(prices, NO_LIST.copy(), NO_OVERRIDES)
    assert events(out) == [] and (out["Adj"] == 1).all()
    assert out.loc[out["Flag"].notna(), "Date"].tolist() == [pd.Timestamp("2026-07-27")]
    assert out["Flag"].dropna().iloc[0].startswith("new listing")
    flagged = out[out["Flag"].notna()].assign(Exchange="BSE")[pa.EVENT_COLUMNS]
    assert pa.review_list(flagged, date(2026, 8, 10))["Symbol"].tolist() == ["MPFSL"]
    # a year without trades exactly is still the same listing: the gap rule still runs
    days = list(pd.bdate_range("2024-11-01", "2024-12-02")) + [pd.Timestamp("2025-12-03")]
    out = pa.decide(raw("HALTED", [96.09] * 22 + [31.0], days=days), NO_LIST.copy(), NO_OVERRIDES)
    assert events(out) == [["gap", pytest.approx(1 / 3)]]


# --- 5. BSE is matched by scrip code only -----------------------------------------------------------

def test_bse_actions_are_matched_by_scrip_code_only_nse_keeps_its_name_fallbacks():
    securities = pd.DataFrame({"Exchange": ["BSE", "BSE", "NSE"], "Symbol": ["SAMENAME", "OTHER", "TINY_SME"],
                               "ISIN": ["INE001A01011", "INE002A01011", "INE003A01011"], "Series": ["A", "A", "SM"],
                               "Code": ["500001", "500002", None]})
    actions = pd.DataFrame({"Exchange": ["BSE", "BSE", "NSE"], "Symbol": ["SAMENAME", "ANYNAME", "TINY"],
                            "Code": ["999999", "500002", None], "ISIN": [None, None, None],
                            "Ex_Date": [DAYS[20]] * 3, "Kind": ["bonus"] * 3, "Factor": [0.5] * 3})
    assert pa.symbols_for(actions, securities, "BSE")["Symbol"].tolist() == ["OTHER"]      # not SAMENAME by name
    assert pa.symbols_for(actions, securities, "NSE")["Symbol"].tolist() == ["TINY_SME"]


def test_a_bonus_listed_under_an_unknown_scrip_code_is_not_put_on_a_namesake(tmp_path):
    conn = store(tmp_path)
    upsert_daily_prices(conn, day_rows("BSE", "SAMENAME", "INE001A01011", list(DAYS[:30]), [100.0] * 20 + [92.0] * 10,
                                       code="500001", series="A"))
    action_lists.store(conn, action_lists.bse_rows(
        "Security Code,Security Name,Company Name,Ex Date,Purpose,Record Date\n"
        f"599999,SAMENAME,Another Co,{DAYS[20]:%d %b %Y},Bonus issue 1:10,{DAYS[20]:%d %b %Y}\n"))
    pa.rebuild(conn)
    assert conn.execute("SELECT count(*) FROM price_adjustments WHERE Event IS NOT NULL").fetchone()[0] == 0
    conn.close()


# --- 6. the NSE twin on BSE ---------------------------------------------------------------------------

TWINS = pd.DataFrame({"Exchange": ["NSE", "BSE"], "Symbol": ["ACUTAAS", "ACUTAAS"],
                      "ISIN": ["INE00FF01025"] * 2, "Series": ["EQ", "A"], "Code": [None, "543349"]})


def bse_decision(nse_prices, nse_listed, bse_prices, bse_listed=None):
    nse_decided = pa.decide(nse_prices, nse_listed, NO_OVERRIDES)
    twin, covered = pa.twin_inputs(bse_prices, nse_decided, TWINS)
    return pa.decide(bse_prices, bse_listed if bse_listed is not None else NO_LIST.copy(), NO_OVERRIDES, twin, covered)


def test_the_twin_factor_lands_on_bses_next_trading_day_however_late():
    # BSE did not trade it for 15 sessions (21 calendar days) after NSE's ex-date.
    nse = raw("ACUTAAS", [2200] * 20 + [1100] * 20)
    bse_days = list(DAYS[:20]) + list(DAYS[35:40])
    bse = raw("ACUTAAS", [2201] * 20 + [1101] * 5, days=bse_days)
    out = bse_decision(nse, listed("ACUTAAS", 20, "split", 0.5), bse)
    assert events(out) == [["nse", pytest.approx(0.5)]] and out["Adj"].iloc[0] == pytest.approx(0.5)
    assert out.loc[out["Event"].notna(), "Date"].tolist() == [DAYS[35]] and out["Flag"].isna().all()
    # where BSE's open does not show the factor, the factor is still taken and the day flagged for review
    flat = bse_decision(nse, listed("ACUTAAS", 20, "split", 0.5), raw("ACUTAAS", [2201] * 25, days=bse_days))
    assert events(flat) == [["nse", pytest.approx(0.5)]]
    assert flat["Flag"].dropna().tolist() == [pa.FLAG_TWIN]
    # a BSE listing that began after NSE's split has no earlier row: no factor (JASH, BSE from 2025-05-20)
    later = bse_decision(nse, listed("ACUTAAS", 20, "split", 0.5), raw("ACUTAAS", [1101] * 5, days=DAYS[35:40]))
    assert events(later) == [] and later["Flag"].isna().all()


def test_a_bonus_in_bses_own_list_on_another_day_is_not_applied_on_top_of_the_twins():
    nse = raw("ACUTAAS", [100] * 20 + [90.91] * 20)
    bse = raw("ACUTAAS", [100.2] * 20 + [91.0] * 20)
    out = bse_decision(nse, listed("ACUTAAS", 20, "bonus", 10 / 11), bse, listed("ACUTAAS", 23, "bonus", 10 / 11))
    assert events(out) == [["nse", pytest.approx(10 / 11)], ["nse_covers", 1.0]]
    assert out["Adj"].iloc[0] == pytest.approx(10 / 11)                       # once, not (10/11)^2
    # far from any twin factor BSE's own list still counts (a BSE-only symbol keeps it always: test above)
    alone = bse_decision(raw("ACUTAAS", [100] * 40), NO_LIST.copy(), bse, listed("ACUTAAS", 20, "bonus", 10 / 11))
    assert events(alone) == [["list", pytest.approx(10 / 11)]]


def test_a_bse_listing_its_open_confirms_takes_the_twins_factor_when_nse_was_halted_across_the_ex_date():
    # NSE halted from day 10 to 19 and decided the 1:1 bonus by its gap on day 20;
    # BSE traded on the real ex-date (day 12), where its own list and open agree.
    nse = raw("ACUTAAS", [50] * 10 + [25] * 20, days=list(DAYS[:10]) + list(DAYS[20:40]))
    bse = raw("ACUTAAS", [50.1] * 12 + [25.1] * 28)
    out = bse_decision(nse, NO_LIST.copy(), bse, listed("ACUTAAS", 12, "bonus", 0.5))
    assert events(out) == [["nse", pytest.approx(0.5)]]
    assert out.loc[out["Event"].notna(), "Date"].tolist() == [DAYS[12]]
    assert (out["Close"] * out["Adj"]).tolist() == pytest.approx([25.05] * 12 + [25.1] * 28)   # not 50/25/25
    assert out["Flag"].isna().all()


def test_a_bse_listing_with_another_ratio_near_a_twin_factor_is_its_own_event_and_flagged():
    nse = raw("ACUTAAS", [100] * 20 + [50] * 20)
    bse = raw("ACUTAAS", [100.1] * 20 + [50.1] * 5 + [45.5] * 15)
    out = bse_decision(nse, listed("ACUTAAS", 20, "split", 0.5), bse, listed("ACUTAAS", 25, "bonus", 10 / 11))
    assert events(out) == [["nse", pytest.approx(0.5)], ["list", pytest.approx(10 / 11)]]
    assert out.loc[out["Flag"].notna(), "Date"].tolist() == [DAYS[25]]
    flagged = out[out["Flag"].notna()].assign(Exchange="BSE")[pa.EVENT_COLUMNS]
    assert pa.review_list(flagged, DAYS[30].date())["Flag"].tolist() == [pa.FLAG_TWIN_DIFFERS]


# --- 7. a listed action no trading day matched goes to the review list ---------------------------

def test_a_listed_split_with_no_trade_within_10_days_is_flagged_for_review(tmp_path):
    conn = store(tmp_path)
    halted = list(DAYS[:10]) + list(DAYS[25:40])                      # suspended across the ex-date
    upsert_daily_prices(conn, day_rows("NSE", "HALTCO", "INE222A01011", halted, [500.0] * 10 + [250.0] * 15))
    upsert_daily_prices(conn, day_rows("NSE", "GONECO", "INE333A01011", list(DAYS[:10]), [80.0] * 10))   # never again
    action_lists.store(conn, action_lists.nse_rows([
        bonus(DAYS[12], "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share", "HALTCO",
              "INE222A01011"),
        bonus(DAYS[12], "Bonus 1:1", "GONECO", "INE333A01011")], "equities"))
    review = tmp_path / "review.csv"
    summary = pa.rebuild(conn, review_path=review, today=DAYS[30].date())
    flagged = conn.execute("SELECT Symbol, Date, Flag, Listed_Factor FROM price_adjustments WHERE Flag IS NOT NULL "
                           "ORDER BY Symbol").fetchall()
    assert flagged == [("HALTCO", DAYS[12].date(), pa.FLAG_NO_TRADE, pytest.approx(0.5))]
    assert pd.read_csv(review)["Flag"].tolist() == [pa.FLAG_NO_TRADE] and summary["review_rows"] == 1
    conn.close()


# --- 8. the market's clock -------------------------------------------------------------------------

class UtcEvening(datetime):
    """20:00 UTC on 2026-10-15: already 2026-10-16 in India."""

    @classmethod
    def now(cls, tz=None):
        moment = datetime(2026, 10, 15, 20, 0, tzinfo=ZoneInfo("UTC"))
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


def test_dates_default_to_the_market_day_in_india_not_the_machines(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "datetime", UtcEvening)
    assert pa.market_today() == date(2026, 10, 16)
    conn = pocl_store(tmp_path)
    seen = {}
    real_review = pa.review_list
    monkeypatch.setattr(pa, "review_list", lambda events, today, sec=None: seen.setdefault("today", today) and
                        real_review(events, today, sec))
    pa.rebuild(conn)
    assert seen["today"] == date(2026, 10, 16)
    conn.close()

    import config
    import eod_maintenance
    monkeypatch.setattr(eod_maintenance, "datetime", UtcEvening)
    monkeypatch.setattr(config, "DUCKDB_FILE", tmp_path / "prices.duckdb")
    got = {}
    monkeypatch.setattr(eod_maintenance.price_adjustments, "refresh",
                        lambda conn, nse, logger, o, r, today, **paths: got.setdefault("today", today) and {})
    eod_maintenance.maintain(logging.getLogger("t"), nse_factory=lambda logger: None)
    assert got["today"] == date(2026, 10, 16)
