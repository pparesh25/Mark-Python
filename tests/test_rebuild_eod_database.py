"""The rebuild from exchange files, end to end on files made by hand: no network, a temporary data root."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime, timedelta

import duckdb
import pandas as pd
import pytest

import config
import rebuild_eod_database as rebuild

ISIN = "INE123A01016"
HOLIDAY = datetime(2026, 7, 8)
SPLIT_DAY = datetime(2026, 7, 9)


def price(day: datetime) -> float:
    return 1000.0 if day < SPLIT_DAY else 201.0


class FakeNSE:
    def __init__(self):
        self.asked = []

    def equityBhavcopy(self, day, folder):
        self.asked.append(day.date())
        if day.date() == HOLIDAY.date():
            raise RuntimeError("NSE file is unavailable or not yet updated.")
        p = price(day)
        pd.DataFrame([{"ISIN": ISIN, "TckrSymb": "SPLITCO", "SctySrs": "EQ", "OpnPric": p, "HghPric": p,
                       "LwPric": p, "ClsPric": p, "TtlTradgVol": 1000}]).to_csv(
            folder / f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv", index=False)

    def deliveryBhavcopy(self, day, folder):
        if day.date() == HOLIDAY.date():
            raise RuntimeError("NSE file is unavailable or not yet updated.")
        pd.DataFrame([{"SYMBOL": "SPLITCO", " SERIES": " EQ", " DATE1": f" {day:%d-%b-%Y}", " NO_OF_TRADES": 50,
                       " DELIV_QTY": 600}]).to_csv(folder / f"sec_bhavdata_full_{day:%d%m%Y}.csv", index=False)

    def actions(self, segment, from_date, to_date):
        if segment != "equities" or not from_date <= SPLIT_DAY <= to_date:
            return []
        return [{"symbol": "SPLITCO", "series": "EQ", "isin": ISIN, "comp": "Split Co", "exDate": "09-Jul-2026",
                 "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"}]

    def exit(self):
        pass


class Response:
    def __init__(self, status, content=b"", text=""):
        self.status_code, self.content, self.text = status, content, text


def fake_get(url, headers=None, params=None, timeout=None):
    if "CorpactCSVDownload" in url:
        return Response(200, text="Security Code,Security Name,Company Name,Ex Date,Purpose,Record Date\n")
    day = datetime.strptime(url.split("_F_")[0][-8:], "%Y%m%d") if "BhavCopy" in url else None
    if "SCBSEALL" in url:
        ddmm, year = url.rsplit("SCBSEALL", 1)[1][:4], int(url.split("/gross/")[1][:4])
        day = datetime(year, int(ddmm[2:]), int(ddmm[:2]))
    if day.date() == HOLIDAY.date():
        return Response(404)
    if "SCBSEALL" in url:
        member = io.BytesIO()
        with zipfile.ZipFile(member, "w") as z:
            z.writestr(f"SCBSEALL{day:%d%m}.TXT", f"DATE|SCRIP CODE|DELIVERY QTY\n{day:%d%m%Y}|500123|300\n")
        return Response(200, member.getvalue())
    p = price(day) + 0.5
    body = pd.DataFrame([{"FinInstrmId": "500123", "TckrSymb": "SPLITCO", "ISIN": ISIN, "SctySrs": "A",
                          "OpnPric": p, "HghPric": p, "LwPric": p, "ClsPric": p, "TtlTradgVol": 500,
                          "TtlNbOfTxsExctd": 20}]).to_csv(index=False).encode()
    return Response(200, body)


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    database = tmp_path / "Database"
    metadata = database / "metadata"
    metadata.mkdir(parents=True)
    (metadata / "meta.json").write_text(json.dumps({"lastUpdate": "2026-06-01T00:00:00+05:30", "holidays": {},
                                                    "special_sessions": [], "equityActions": []}))
    for name, value in (("DATABASE_DIR", database), ("EOD_METADATA_DIR", metadata),
                        ("DUCKDB_FILE", database / "NSE_BSE_EOD.duckdb"), ("DATA_DIR", database / "NSE_EOD"),
                        ("BSE_DATA_DIR", database / "BSE_EOD")):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(rebuild, "RETRY_WAITS", ())
    monkeypatch.setattr(rebuild, "BSE_PAUSE", 0)
    old = duckdb.connect(str(database / "NSE_BSE_EOD.duckdb"))            # a version 1 store to replace
    old.execute("CREATE TABLE eod_prices (Date DATE, Symbol VARCHAR, Exchange VARCHAR, Close DOUBLE)")
    old.close()
    return database


def test_a_rebuild_stores_raw_days_decides_the_split_and_swaps_the_old_store_out(data_root):
    now = datetime(2026, 7, 10, 20, 0, tzinfo=config.MARKET_TIMEZONE)
    report = rebuild.rebuild(logging.getLogger("t"), years=0.02, now=now, nse_factory=FakeNSE, get=fake_get)

    assert report["swapped"] is True and report["holidays"] == ["2026-07-08"] and report["holes"] == []
    assert (data_root / "NSE_BSE_EOD.v1-backup.duckdb").exists()
    conn = duckdb.connect(str(data_root / "NSE_BSE_EOD.duckdb"), read_only=True)
    rows = conn.execute("SELECT Exchange, Date, Close, Volume, Dlv_Qty FROM eod_data ORDER BY Exchange, Date").df()
    events = conn.execute("SELECT Exchange, Event, Factor FROM price_adjustments WHERE Event IS NOT NULL "
                          "ORDER BY Exchange").fetchall()
    conn.close()
    nse = rows[rows["Exchange"] == "NSE"]
    assert nse["Close"].tolist() == pytest.approx([200.0, 200.0, 200.0, 201.0, 201.0])     # 1000 x 0.2 before the split
    assert nse["Volume"].iloc[0] == 5000 and nse["Dlv_Qty"].iloc[0] == 3000                # shares / 0.2
    assert events == [("BSE", "nse", pytest.approx(0.2)), ("NSE", "list", pytest.approx(0.2))]
    meta = json.loads((data_root / "metadata" / "meta.json").read_text())
    assert meta["lastUpdate"].startswith("2026-07-10") and "equityActions" not in meta
    assert "equityActions" in json.loads((data_root / "metadata" / "meta.json.before-rebuild").read_text())
    assert json.loads((data_root / "metadata" / "bse_meta.json").read_text())["lastUpdate"].startswith("2026-07-10")


def test_a_day_one_exchange_did_not_answer_keeps_the_old_store(data_root):
    def bse_refuses_one_day(url, **kwargs):
        if "20260709" in url:
            return Response(403, b"<HTML>Access Denied</HTML>")
        return fake_get(url, **kwargs)

    now = datetime(2026, 7, 10, 20, 0, tzinfo=config.MARKET_TIMEZONE)
    report = rebuild.rebuild(logging.getLogger("t"), years=0.02, now=now, nse_factory=FakeNSE, get=bse_refuses_one_day)
    assert report["swapped"] is False and report["holes"] == ["2026-07-09"]
    conn = duckdb.connect(str(data_root / "NSE_BSE_EOD.duckdb"), read_only=True)
    assert conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'raw_prices'").fetchone()[0] == 0
    conn.close()
