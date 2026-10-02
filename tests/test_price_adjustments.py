"""Raw prices, the exchanges' lists and the factors decided from them -- on data made by hand, no network."""

from __future__ import annotations

import logging
from datetime import date, timedelta

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
