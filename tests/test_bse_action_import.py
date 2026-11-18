"""BSE's corporate-action list saved by hand (BSE refuses the scripted download) -- no network."""

from __future__ import annotations

import logging
from datetime import date

import duckdb
import pandas as pd
import pytest

import action_lists
import price_adjustments as pa
from eod_store import create_schema, upsert_daily_prices

HEADER = "Security Code,Security Name,Company Name,Ex Date,Purpose,Record Date\n"
DAYS = pd.bdate_range("2025-04-01", periods=30)
LOG = logging.getLogger("t")


def csv(*rows):
    return HEADER + "".join(f"{code},{name},{name} Ltd,{day:%d %b %Y},{purpose},{day:%d %b %Y}\n"
                            for code, name, day, purpose in rows)


@pytest.fixture
def conn(tmp_path):
    connection = duckdb.connect(str(tmp_path / "prices.duckdb"))
    create_schema(connection)
    yield connection
    connection.close()


def stored_bse(conn):
    return conn.execute("SELECT Code, Symbol, CAST(Ex_Date AS VARCHAR), Purpose FROM exchange_actions "
                        "WHERE Exchange = 'BSE' ORDER BY Code, Ex_Date").fetchall()


def test_a_saved_file_is_imported_and_moved_to_imported(conn, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "Corporate_Actions.csv").write_text("﻿" + csv((590001, "ONLYBSE", DAYS[20], "Bonus issue 2:1")),
                                                 encoding="utf-8")
    result = action_lists.import_bse_files(conn, inbox, date(2025, 5, 1), LOG)
    assert result == {"files": 1, "listed": 1, "new": 1, "replaced": 0, "rejected": 0}
    assert stored_bse(conn) == [("590001", "ONLYBSE", f"{DAYS[20]:%Y-%m-%d}", "Bonus issue 2:1")]
    assert not (inbox / "Corporate_Actions.csv").exists()
    assert [p.name for p in (inbox / "imported").iterdir()] == ["20250501_Corporate_Actions.csv"]
    # importing the same list again adds nothing
    (inbox / "again.csv").write_text(csv((590001, "ONLYBSE", DAYS[20], "Bonus issue 2:1")))
    assert action_lists.import_bse_files(conn, inbox, date(2025, 5, 1), LOG)["new"] == 0
    assert len(stored_bse(conn)) == 1


def test_a_newer_file_replaces_a_revised_row_inside_its_range_only_for_the_scrips_it_lists(conn, tmp_path):
    action_lists.store(conn, action_lists.bse_rows(csv((590001, "ONLYBSE", DAYS[10], "Bonus issue 1:1"),
                                                       (590002, "OTHER", DAYS[12], "Stock  Split From Rs.10/- to Rs.1/-"))))
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    # The file's range is what its rows span (BSE's CSV does not say what was asked): 590003 on days 5
    # and 15 makes it cover 590001's old and moved ex-dates; 590002, absent from the file, is kept.
    (inbox / "revised.csv").write_text(csv((590003, "THIRD", DAYS[5], "Dividend - Rs. - 1.0000"),
                                           (590001, "ONLYBSE", DAYS[11], "Bonus issue 1:1"),   # ex-date moved
                                           (590003, "THIRD", DAYS[15], "Dividend - Rs. - 1.0000")))
    result = action_lists.import_bse_files(conn, inbox, date(2025, 5, 1), LOG)
    assert (result["new"], result["replaced"]) == (3, 1)
    assert stored_bse(conn) == [("590001", "ONLYBSE", f"{DAYS[11]:%Y-%m-%d}", "Bonus issue 1:1"),
                                ("590002", "OTHER", f"{DAYS[12]:%Y-%m-%d}", "Stock  Split From Rs.10/- to Rs.1/-"),
                                ("590003", "THIRD", f"{DAYS[5]:%Y-%m-%d}", "Dividend - Rs. - 1.0000"),
                                ("590003", "THIRD", f"{DAYS[15]:%Y-%m-%d}", "Dividend - Rs. - 1.0000")]


def test_a_row_outside_the_files_range_is_never_deleted(conn, tmp_path):
    action_lists.store(conn, action_lists.bse_rows(csv((590001, "ONLYBSE", DAYS[10], "Bonus issue 1:1"))))
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "narrow.csv").write_text(csv((590001, "ONLYBSE", DAYS[20], "Stock  Split From Rs.10/- to Rs.5/-")))
    action_lists.import_bse_files(conn, inbox, date(2025, 5, 1), LOG)
    assert [row[2] for row in stored_bse(conn)] == [f"{DAYS[10]:%Y-%m-%d}", f"{DAYS[20]:%Y-%m-%d}"]


@pytest.mark.parametrize("text", ["<HTML>Access Denied</HTML>", HEADER, "a,b\n1,2\n"])
def test_a_file_that_is_not_bses_list_is_rejected_and_changes_nothing(conn, tmp_path, text):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "wrong.csv").write_text(text)
    (inbox / "notes.txt").write_text("not a csv")
    result = action_lists.import_bse_files(conn, inbox, date(2025, 5, 1), LOG)
    assert (result["files"], result["rejected"]) == (0, 1)
    assert stored_bse(conn) == []
    assert [p.name for p in (inbox / "rejected").iterdir()] == ["20250501_wrong.csv"]
    assert (inbox / "notes.txt").exists()                      # only .csv files are touched


def test_a_missing_inbox_imports_nothing(conn, tmp_path):
    assert action_lists.import_bse_files(conn, tmp_path / "absent", date(2025, 5, 1), LOG)["files"] == 0
    assert action_lists.import_bse_files(conn, None, date(2025, 5, 1), LOG)["files"] == 0


def test_the_stale_warning_names_the_page_the_range_and_the_folder(tmp_path, caplog):
    status, inbox, today = tmp_path / "status.json", tmp_path / "inbox", date(2026, 11, 18)
    with caplog.at_level(logging.WARNING):
        assert action_lists.warn_if_stale(status, inbox, today, 7, LOG)            # never recorded
    message = caplog.text
    assert "bseindia.com/corporates/corporate_act" in message and str(inbox) in message
    assert "19/09/2026" in message and "02/01/2027" in message                     # window(today)
    action_lists.record_ok(status, "BSE", date(2026, 11, 12), "imported file")
    assert not action_lists.warn_if_stale(status, inbox, today, 7, LOG)            # 6 days old
    assert action_lists.warn_if_stale(status, inbox, date(2026, 11, 19), 7, LOG)   # 7 days old
    status.write_text("not json")
    assert action_lists.warn_if_stale(status, inbox, today, 7, LOG)


def test_a_fetch_bse_answers_is_recorded_a_refused_one_is_not(conn, tmp_path):
    status = tmp_path / "status.json"

    class Refused:
        status_code, text = 403, "<HTML>Access Denied</HTML>"

    class Empty:
        status_code, text = 200, HEADER

    window = (DAYS[0].date(), DAYS[29].date())
    action_lists.refresh(conn, None, *window, LOG, get=lambda *a, **k: Refused(), exchanges=("BSE",),
                         status_path=status, today=date(2025, 5, 1))
    assert action_lists.read_status(status) == {}
    action_lists.refresh(conn, None, *window, LOG, get=lambda *a, **k: Empty(), exchanges=("BSE",),
                         status_path=status, today=date(2025, 5, 2))
    assert action_lists.read_status(status) == {"BSE": {"last_ok": "2025-05-02", "source": "fetched"}}


def test_an_imported_bonus_is_applied_to_bse_only_prices_by_the_next_refresh(conn, tmp_path, monkeypatch):
    days = list(DAYS[:30])
    closes = [300.0] * 20 + [100.0] * 10                     # a 2:1 bonus on day 20
    upsert_daily_prices(conn, pd.DataFrame({
        "Date": days, "Symbol": "ONLYBSE", "Exchange": "BSE", "ISIN": "INE590001011", "Series": "A",
        "Code": "590001", "Open": closes, "High": closes, "Low": closes, "Close": closes,
        "Volume": 1000, "Total_Trades": 10, "Dlv_Qty": 600}))
    inbox, status = tmp_path / "inbox", tmp_path / "status.json"
    inbox.mkdir()
    (inbox / "bse.csv").write_text(csv((590001, "ONLYBSE", DAYS[20], "Bonus issue 2:1")))

    def refused(*args, **kwargs):
        raise RuntimeError("BSE action list answered HTTP 403")

    monkeypatch.setattr(action_lists, "fetch_bse_parts", refused)
    summary = pa.refresh(conn, None, LOG, today=DAYS[29].date(), bse_inbox=inbox, status_path=status)
    assert summary["bse_import"]["new"] == 1
    assert action_lists.read_status(status)["BSE"]["source"] == "imported file"
    first = conn.execute("SELECT Close FROM eod_prices WHERE Symbol = 'ONLYBSE' AND Exchange = 'BSE' "
                         "ORDER BY Date LIMIT 1").fetchone()[0]
    assert first == pytest.approx(100.0)
