"""The run fingerprint: stable hashes of what a run screened, and the comparison CLI."""

import json
import logging
import os
import sys
from datetime import date

import duckdb
import pytest

import config
import run_fingerprint
import run_screener
from eod_store import create_schema
from screening_readiness import EodReadiness

ROWS = [
    ("2026-10-13", "AAA", "NSE", 100.0, 105.0, 99.0, 104.5, 1000),
    ("2026-10-14", "AAA", "NSE", 104.5, 110.0, 104.0, 109.25, 1500),
    ("2026-10-13", "BBB", "BSE", 10.0, 10.5, 9.9, 10.1234, 300),
    ("2026-10-14", "BBB", "BSE", 10.1234, 11.0, 10.0, 10.9, 400),
]


def make_store(path, rows=ROWS, factors=True, factor=0.5, version=2):
    """A version 2 store (eod_prices a view over raw_prices) -- or version 3, its adjusted rows stored."""
    conn = duckdb.connect(str(path))
    try:
        create_schema(conn, version)
        conn.executemany(
            "INSERT INTO securities VALUES (?, ?, ?, 'EQ', NULL)",
            [("NSE", "AAA", "INE000A01011"), ("BSE", "BBB", "INE000B01011")],
        )
        conn.executemany(
            "INSERT INTO raw_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, 10, 5)", rows
        )
        if factors:
            conn.execute("INSERT INTO adjustment_periods VALUES ('NSE', 'AAA', DATE '2026-10-13', ?)", [factor])
            if version == 3:
                conn.execute("INSERT INTO adjustment_ledger VALUES ('NSE', 'AAA', DATE '2026-10-14', ?, 'list', "
                             "'list|NSE|AAA|split|0.5', 1.0, 'split', ?, TIMESTAMP '2026-10-14 18:00:00', 1)",
                             [factor, factor])
                conn.execute("INSERT INTO gap_history VALUES ('NSE', 'AAA', DATE '2026-10-14', 0.4)")
        else:
            # Factor and action tables renamed or gone (the view needs only adjustment_periods).
            conn.execute("DROP TABLE price_adjustments")
            conn.execute("DROP TABLE exchange_actions")
        if version == 3:
            from eod_store import rematerialize
            rematerialize(conn)
    finally:
        conn.close()
    return path


def make_external_store(path, rows=ROWS):
    """A store exported elsewhere: adjusted prices in an eod_prices table, no raw_prices."""
    conn = duckdb.connect(str(path))
    try:
        conn.execute("CREATE TABLE eod_prices (Date DATE, Symbol VARCHAR, Exchange VARCHAR, Open DOUBLE, "
                     "High DOUBLE, Low DOUBLE, Close DOUBLE, Volume BIGINT)")
        conn.executemany("INSERT INTO eod_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.execute("CREATE TABLE securities (Exchange VARCHAR, Symbol VARCHAR, ISIN VARCHAR, Series VARCHAR)")
        conn.execute("CREATE VIEW eod_data AS SELECT p.*, s.ISIN, s.Series FROM eod_prices p "
                     "JOIN securities s USING (Exchange, Symbol)")
    finally:
        conn.close()
    return path


def store_section(path, external=False):
    return run_fingerprint._store_section(path, external)


def test_store_hash_ignores_row_order_and_sees_one_price(tmp_path):
    first = store_section(make_store(tmp_path / "a.duckdb"))
    reordered = store_section(make_store(tmp_path / "b.duckdb", rows=list(reversed(ROWS))))
    changed_rows = list(ROWS)
    changed_rows[1] = changed_rows[1][:6] + (109.26,) + changed_rows[1][7:]
    changed = store_section(make_store(tmp_path / "c.duckdb", rows=changed_rows))

    assert (first["schema_version"], first["kind"], first["price_table"]) == (2, "v2", "raw_prices")
    assert first["exchanges"] == reordered["exchanges"]
    assert first["sessions"] == reordered["sessions"]
    nse = first["exchanges"]["NSE"]
    assert (nse["rows"], nse["sessions"], nse["symbols"]) == (2, 2, 1)
    assert (nse["first_date"], nse["last_date"]) == ("2026-10-13", "2026-10-14")
    assert changed["exchanges"]["NSE"]["content_hash"] != nse["content_hash"]
    assert changed["exchanges"]["BSE"] == first["exchanges"]["BSE"]
    # Only the changed session differs.
    assert changed["sessions"]["NSE"]["2026-10-13"] == first["sessions"]["NSE"]["2026-10-13"]
    assert changed["sessions"]["NSE"]["2026-10-14"] != first["sessions"]["NSE"]["2026-10-14"]
    assert first["tables"]["adjustment_periods"]["rows"] == 1
    assert first["tables"]["raw_prices"] == {"rows": 4}
    assert changed["engine_input_hash"] != first["engine_input_hash"]
    assert reordered["engine_input_hash"] == first["engine_input_hash"]


def test_a_factor_change_changes_the_engine_input_hash(tmp_path):
    halved = store_section(make_store(tmp_path / "a.duckdb", factor=0.5))
    thirds = store_section(make_store(tmp_path / "b.duckdb", factor=1 / 3))
    assert halved["exchanges"] == thirds["exchanges"]  # raw prices are equal
    assert halved["tables"]["adjustment_periods"]["hash"] != thirds["tables"]["adjustment_periods"]["hash"]
    assert halved["engine_input_hash"] != thirds["engine_input_hash"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 1e30])
def test_non_finite_values_are_hashed_not_fatal(tmp_path, bad):
    store = store_section(make_store(tmp_path / "a.duckdb", factor=bad))
    again = store_section(make_store(tmp_path / "b.duckdb", factor=bad))
    digest = store["tables"]["adjustment_periods"]
    assert "error" not in digest and digest["rows"] == 1
    assert digest["hash"] == again["tables"]["adjustment_periods"]["hash"]
    assert digest["hash"] != store_section(make_store(tmp_path / "c.duckdb"))["tables"]["adjustment_periods"]["hash"]

    rows = list(ROWS)
    rows[0] = rows[0][:6] + (bad,) + rows[0][7:]
    prices = store_section(make_store(tmp_path / "d.duckdb", rows=rows))
    assert "error" not in prices and prices["exchanges"]["NSE"]["rows"] == 2


def test_an_exported_store_is_external_not_v1(tmp_path):
    path = make_external_store(tmp_path / "lake.duckdb")
    external = store_section(path, external=True)
    assert (external["kind"], external["schema_version"], external["price_table"]) == ("external", "external",
                                                                                      "eod_prices")
    assert external["exchanges"]["NSE"]["rows"] == 2
    assert store_section(path)["kind"] == "v1"  # the same layout synced here is an old store


def test_a_version_3_store_hashes_its_stored_adjusted_prices_the_ledger_and_the_gap_history(tmp_path):
    v3 = store_section(make_store(tmp_path / "a.duckdb", version=3))
    assert (v3["schema_version"], v3["kind"], v3["price_table"]) == (3, "v3", "eod_prices")
    assert v3["tables"]["eod_prices"] == {"rows": 4}
    ledger = v3["tables"]["adjustment_ledger"]
    assert ledger["rows"] == 1 and ledger["hash"] and "Decided_At" not in ledger["columns"]
    assert v3["tables"]["gap_history"]["rows"] == 1 and v3["tables"]["gap_history"]["hash"]
    assert "hash" not in v3["tables"]["maintenance_state"]            # when, not what
    # The engine input is the stored adjusted prices: another factor over the same raw rows changes it.
    other = store_section(make_store(tmp_path / "b.duckdb", version=3, factor=1 / 3))
    assert other["engine_input_hash"] != v3["engine_input_hash"]
    assert other["tables"]["adjustment_ledger"]["hash"] != ledger["hash"]
    assert other["exchanges"]["NSE"]["content_hash"] != v3["exchanges"]["NSE"]["content_hash"]
    # Version 2 hashes its raw rows: equal where no factor applies (BSE), different where one does (NSE).
    v2 = store_section(make_store(tmp_path / "c.duckdb", version=2))
    assert v2["kind"] == "v2" and v2["exchanges"]["BSE"] == v3["exchanges"]["BSE"]
    assert v2["exchanges"]["NSE"]["content_hash"] != v3["exchanges"]["NSE"]["content_hash"]


def test_store_hash_is_a_fixed_value(tmp_path):
    """The text hashed is platform independent, so the value itself is pinned."""
    import hashlib

    store = store_section(make_store(tmp_path / "a.duckdb"))

    def row_hash(*fields):
        text = "|".join(str(field) for field in fields)
        return int(hashlib.md5(text.encode()).hexdigest()[:16], 16)

    # Raw prices to 1/10^6, one row a session for AAA.
    sessions = {
        "2026-10-13": f"1:{row_hash('AAA', 100000000, 105000000, 99000000, 104500000, 1000):016x}",
        "2026-10-14": f"1:{row_hash('AAA', 104500000, 110000000, 104000000, 109250000, 1500):016x}",
    }
    assert store["sessions"]["NSE"] == sessions
    lines = sorted(f"{day} {entry}" for day, entry in sessions.items())
    expected = hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]
    assert store["exchanges"]["NSE"]["content_hash"] == expected


def test_factor_tables_may_be_absent(tmp_path):
    store = store_section(make_store(tmp_path / "a.duckdb", factors=False))
    assert "price_adjustments" not in store["tables"]
    assert store["exchange_actions"] is None
    assert store["exchanges"]["NSE"]["rows"] == 2


def test_fingerprint_failures_only_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        fingerprint = run_fingerprint.collect(
            timestamp="20261014_234004",
            database=tmp_path / "missing.duckdb",
            fundamentals=tmp_path / "missing.csv",
            engine={"version": "x", "sha256": "0" * 64, "path": "x"},
            readiness=None,
        )
    assert fingerprint["store"]["error"] == "price store missing"
    assert fingerprint["fundamentals"]["present"] is False
    run_fingerprint.summary_lines(fingerprint)  # never raises on partial data

    def broken():
        raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        assert run_fingerprint._section("demo", broken) == {"error": "RuntimeError: boom"}
    assert "demo unavailable" in caplog.text


def test_full_fingerprint_and_outputs(tmp_path):
    store = make_store(tmp_path / "a.duckdb")
    fundamentals = tmp_path / "all-company_20261014.csv"
    fundamentals.write_text("Name,ISIN Code\nA,INE000A01011\nB,INE000B01011\nC,INE000B01011\n")
    out = tmp_path / "qualified"
    (out / "NSE" / "mark_tight").mkdir(parents=True)
    (out / "NSE" / "Tight Flag").mkdir(parents=True)
    (out / "NSE" / "mark_tight" / "20261014_234004_nse_mark_tight_qualified.csv").write_text(
        "Index,symbol,Mark_Tight_Date\n0,AAA,2026-10-14\n1,CCC,2026-10-01\n"
    )
    (out / "NSE" / "Tight Flag" / "20261014_234004_nse_tight_flag_qualified.csv").write_text(
        "Index,symbol,breakout_date\n0,AAA,2026-09-30\n"
    )
    readiness = EodReadiness(target_date=date(2026, 10, 15),
                             as_of_dates={"NSE": date(2026, 10, 14), "BSE": date(2026, 10, 13)})

    fingerprint = run_fingerprint.collect(
        timestamp="20261014_234004", database=store, fundamentals=fundamentals,
        engine={"version": "stock_analysis 0.3.0", "sha256": "a" * 64, "path": "/x"},
        readiness=readiness, local_fallback=True, warnings=["w"],
    )
    run_fingerprint.add_outputs(fingerprint, out_dir=out, timestamp="20261014_234004")
    path = run_fingerprint.write(fingerprint, tmp_path / "fp.json")
    loaded = json.loads(path.read_text())

    assert loaded["data_as_of"] == {
        "BSE": "2026-10-13", "NSE": "2026-10-14", "target": "2026-10-15",
        "screening_as_of": "2026-10-13", "as_of_mismatch": True, "local_fallback": True,
        "sync_stopped": False, "warnings": ["w"],
    }
    assert loaded["fundamentals"]["rows"] == 3
    assert loaded["fundamentals"]["distinct_isins"] == 2
    assert loaded["run"]["timestamp_ist"] == "2026-10-14T23:40:04+05:30"
    for package in ("python", "pandas", "numpy", "duckdb", "nse", "httpx"):
        assert package in loaded["packages"]
    assert loaded["outputs"]["NSE/mark_tight"]["entries"] == ["AAA|2026-10-14", "CCC|2026-10-01"]
    assert loaded["outputs"]["NSE/Tight Flag"]["breakout_column"] == "breakout_date"
    assert loaded["overrides"]["present"] in (True, False)
    assert any("as-of mismatch: YES" in line for line in run_fingerprint.summary_lines(loaded))


def test_engine_info_has_the_full_checksum(tmp_path):
    binary = tmp_path / "stock_analysis"
    binary.write_bytes(b"binary")
    info = run_fingerprint.engine_info(binary)
    assert info["sha256"].startswith("9a3a45d01531a20e") and len(info["sha256"]) == 64
    assert info["path"] == str(binary)


def _fingerprint(**changes):
    base = {
        "run": {"timestamp": "20261014_234004", "platform": "macOS"},
        "packages": {"python": "3.10.20", "pandas": "2.3.3", "nse": "1.2.6", "httpx": "0.28.1"},
        "store": {
            "path": "/a", "schema_version": 2,
            "exchanges": {"NSE": {"rows": 2, "last_date": "2026-10-14"}},
            "sessions": {"NSE": {"2026-10-13": "1:aa", "2026-10-14": "1:bb"}},
        },
        "outputs": {"NSE/mark_tight": {"count": 2, "hash": "h1", "entries": ["AAA|2026-10-14", "BBB|2026-10-01"]}},
    }
    for key, value in changes.items():
        section, field = key.split("__")
        base[section][field] = value
    return base


def test_compare_names_the_fields_sessions_and_symbols(tmp_path, capsys):
    a = _fingerprint()
    b = _fingerprint(
        run__platform="Windows-11",
        packages__pandas="2.2.3",
        store__sessions={"NSE": {"2026-10-13": "1:zz"}},
    )
    b["outputs"] = {"NSE/mark_tight": {"count": 1, "hash": "h2", "entries": ["AAA|2026-10-14"]}}
    relevant, informational = run_fingerprint.compare(a, b)
    text = "\n".join(relevant)
    assert "packages.pandas: A='2.3.3'  B='2.2.3'" in text
    assert "1 session(s) only in A: 2026-10-14" in text
    assert "1 shared session(s) differ: 2026-10-13" in text
    assert "only in A: BBB|2026-10-01" in text
    assert any(line.startswith("run.platform") for line in informational)

    (tmp_path / "a.json").write_text(json.dumps(a))
    (tmp_path / "b.json").write_text(json.dumps(b))
    # Python, nse and httpx versions do not compute prices: informational only.
    other_runtime = _fingerprint(run__platform="Linux", packages__python="3.11.9", packages__nse="3.2.1",
                                 packages__httpx="0.27.0")
    (tmp_path / "c.json").write_text(json.dumps(other_runtime))
    assert run_fingerprint.main([str(tmp_path / "a.json"), str(tmp_path / "b.json")]) == 1
    assert "difference(s) that can change results" in capsys.readouterr().out
    assert run_fingerprint.main([str(tmp_path / "a.json"), str(tmp_path / "c.json")]) == 0
    assert "No differences" in capsys.readouterr().out


def test_consistency_warning_is_logged_once_and_kept(caplog):
    logger = logging.getLogger("test-consistency")
    warnings = []
    with caplog.at_level(logging.WARNING):
        run_screener.consistency_warning(logger, warnings, "NSE and BSE differ")
        run_screener.consistency_warning(logger, warnings, "NSE and BSE differ")
    assert warnings == ["NSE and BSE differ"]
    assert caplog.text.count("RESULTS MAY DIFFER: NSE and BSE differ") == 1


@pytest.mark.parametrize("timestamp", ["20261014_234004"])
def test_fingerprint_path_is_in_the_log_dir(timestamp):
    assert run_fingerprint.fingerprint_path(timestamp) == config.LOG_DIR / f"run_fingerprint_{timestamp}.json"


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """main() against a temporary data root; returns a function that runs it and gives (exit code, log)."""
    for name in ("DATA_ROOT", "DATABASE_DIR", "DATA_DIR", "BSE_DATA_DIR", "DUCKDB_FILE", "FUNDAMENTAL_DIR",
                 "FUNDAMENTAL_FILE", "OUT_DIR", "CHART_DIR", "LOG_DIR", "DAILY_BHAVCOPY_DIR",
                 "BSE_DAILY_BHAVCOPY_DIR", "EOD_METADATA_DIR", "LOG_FILE", "SCREENER_CREDS", "RUN_LOCK_FILE",
                 "WATCHLISTS_ENABLED", "OPENTRADER_ROOT", "STRIP_SME_WATCHLIST_SUFFIX", "RUST_BINARY"):
        monkeypatch.setattr(config, name, getattr(config, name))
    for name in ("MARK_DATA_ROOT", "MARK_WRITE_WATCHLISTS", "MARK_OPENTRADER_ROOT",
                 "MARK_STRIP_SME_WATCHLIST_SUFFIX", "MARK_SCREENER_TIMESTAMP", "MARK_SCHEDULED"):
        # setenv records the original value (or its absence) so main()'s writes are undone.
        original = os.environ.get(name)
        monkeypatch.setenv(name, original or "")
        if original is None:
            monkeypatch.delenv(name)
    root_logger = logging.getLogger()
    before = list(root_logger.handlers)
    monkeypatch.setattr(root_logger, "handlers", list(before))
    monkeypatch.setattr(root_logger, "level", root_logger.level)

    root = tmp_path / "root"
    config.configure_data_root(root)
    (root / "Database").mkdir(parents=True)
    fundamentals = config.FUNDAMENTAL_DIR / f"all-company_{config.CURRENT_DATE}.csv"
    fundamentals.parent.mkdir(parents=True)
    header = ("ISIN Code,Name,Industry,Current Price,Market Capitalization,"
              "Return on capital employed,Return on equity")
    fundamentals.write_text(header + "\n" + "INE000A01011,A,X,1,2,3,4\n" * 60)
    monkeypatch.setattr(config, "RUST_BINARY", tmp_path / "no-engine")
    state = {"readiness": EodReadiness(target_date=date(2026, 10, 14),
                                       as_of_dates={"NSE": date(2026, 10, 14), "BSE": date(2026, 10, 13)}),
             "screened": 0}
    monkeypatch.setattr(run_screener, "inspect_eod_readiness", lambda *args: state["readiness"])

    def fake_screening(logger, include_sme=False, only_sme=False):
        state["screened"] += 1
        folder = config.OUT_DIR / "NSE" / "mark_tight"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{config.CURRENT_TIMESTAMP}_nse_mark_tight_qualified.csv").write_text(
            "Index,symbol,Mark_Tight_Date\n0,AAA,2026-10-14\n")

    monkeypatch.setattr(run_screener, "run_all_screening", fake_screening)

    def run(*extra):
        monkeypatch.setattr(sys, "argv", ["run_screener.py", "--data-root", str(root), "--skip-sync",
                                          "--no-watchlists", *extra])
        try:
            run_screener.main()
            code = 0
        except SystemExit as stop:
            code = stop.code
        finally:
            for handler in logging.getLogger().handlers:
                handler.flush()
        return code, config.LOG_FILE.read_text(encoding="utf-8")

    run.state = state
    yield run
    for handler in logging.getLogger().handlers:
        if handler not in before:
            handler.close()  # Windows cannot remove an open log file


MISMATCH = "NSE and BSE local data end on different sessions (BSE=2026-10-13, NSE=2026-10-14)"


def test_pipeline_warns_on_as_of_mismatch_and_writes_the_fingerprint(pipeline):
    """main(): the mismatch is logged where found, repeated in the summary, and kept in the JSON."""
    make_store(config.DUCKDB_FILE)
    code, log = pipeline()

    assert code == 0  # a manual run still succeeds
    assert log.count(f"RESULTS MAY DIFFER: {MISMATCH}") == 1
    summary = log.split("Pipeline complete")[1]
    assert f"! {MISMATCH}" in summary
    assert "screening local fallback data" in summary
    assert "Run fingerprint:" in log
    assert not config.RUN_LOCK_FILE.exists()

    fingerprint = json.loads(run_fingerprint.fingerprint_path().read_text(encoding="utf-8"))
    assert fingerprint["data_as_of"]["as_of_mismatch"] is True
    assert fingerprint["data_as_of"]["local_fallback"] is True
    assert any(MISMATCH in message for message in fingerprint["data_as_of"]["warnings"])
    assert fingerprint["outputs"]["NSE/mark_tight"]["entries"] == ["AAA|2026-10-14"]
    assert fingerprint["store"]["exchanges"]["NSE"]["rows"] == 2


def test_a_scheduled_run_on_data_not_current_exits_3(pipeline, monkeypatch):
    make_store(config.DUCKDB_FILE)
    monkeypatch.setenv("MARK_SCHEDULED", "1")
    code, log = pipeline()
    assert code == run_screener.EXIT_DATA_NOT_CURRENT == 3
    assert pipeline.state["screened"] == 1  # the lists were still written
    assert not config.RUN_LOCK_FILE.exists()

    pipeline.state["readiness"] = EodReadiness(target_date=date(2026, 10, 14),
                                               as_of_dates={"NSE": date(2026, 10, 14), "BSE": date(2026, 10, 14)})
    code, log = pipeline()
    assert code == 0


def test_an_exported_store_screened_with_skip_sync_is_not_called_version_1(pipeline):
    make_external_store(config.DUCKDB_FILE)
    code, log = pipeline()
    assert code == 0
    assert "version 1" not in log
    fingerprint = json.loads(run_fingerprint.fingerprint_path().read_text(encoding="utf-8"))
    assert fingerprint["store"]["kind"] == "external"


def make_maintained_store(path):
    """A version 3 store decided by the full rebuild: AAA halves on 2026-10-14."""
    import pandas as pd

    import price_adjustments
    from eod_store import upsert_daily_prices

    conn = duckdb.connect(str(path))
    try:
        create_schema(conn)
        days = pd.bdate_range("2026-09-01", "2026-10-14")
        closes = [100.0] * (len(days) - 1) + [50.0]
        upsert_daily_prices(conn, pd.DataFrame({
            "Date": days, "Symbol": "AAA", "Exchange": "NSE", "ISIN": "INE000A01011", "Series": "EQ",
            "Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": 1000}))
        price_adjustments.rebuild(conn, None, None, date(2026, 10, 14))
    finally:
        conn.close()


def test_verify_exits_0_on_an_equal_store_5_after_a_repair_6_when_it_cannot(pipeline):
    make_maintained_store(config.DUCKDB_FILE)
    code, log = pipeline("--verify")
    assert code == 0 and "equals a full rebuild" in log
    assert pipeline.state["screened"] == 0 and not config.RUN_LOCK_FILE.exists()
    conn = duckdb.connect(str(config.DUCKDB_FILE))
    conn.execute("UPDATE eod_prices SET Close = Close * 2 WHERE Date = '2026-09-01'")
    conn.close()
    code, log = pipeline("--verify")
    assert code == run_screener.EXIT_VERIFY_REPAIRED == 5 and "repaired" in log
    assert pipeline("--verify")[0] == 0
    config.DUCKDB_FILE.unlink()
    code, log = pipeline("--verify")
    assert code == run_screener.EXIT_VERIFY_FAILED == 6 and "Verify failed" in log


def test_verify_respects_the_run_lock(pipeline):
    make_maintained_store(config.DUCKDB_FILE)
    config.RUN_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    holder = {"pid": os.getpid(), "host": run_screener.platform.node(), "started": run_screener.time.time(),
              "token": "other"}
    config.RUN_LOCK_FILE.write_text(json.dumps(holder))
    code, log = pipeline("--verify")
    assert code == run_screener.EXIT_BUSY and "equals a full rebuild" not in log


def test_a_second_run_while_one_holds_the_lock_exits_busy(pipeline):
    make_store(config.DUCKDB_FILE)
    config.RUN_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    holder = {"pid": os.getpid(), "host": run_screener.platform.node(), "started": run_screener.time.time(),
              "started_ist": "2026-10-14T19:30:00+05:30", "run": "20261014_193000", "token": "other"}
    config.RUN_LOCK_FILE.write_text(json.dumps(holder))
    code, log = pipeline()
    assert code == run_screener.EXIT_BUSY == 4
    assert "Another Mark Screener run is in progress" in log
    assert pipeline.state["screened"] == 0
    assert json.loads(config.RUN_LOCK_FILE.read_text())["token"] == "other"  # not removed


def _dead_pid() -> int:
    import subprocess

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@pytest.mark.parametrize("holder", ["dead", "old", "garbage-old"])
def test_a_stale_lock_is_taken_over(tmp_path, holder, caplog):
    path = tmp_path / "mark_screener.lock"
    now = run_screener.time.time()
    if holder == "dead":
        path.write_text(json.dumps({"pid": _dead_pid(), "host": run_screener.platform.node(), "started": now}))
    elif holder == "old":
        path.write_text(json.dumps({"pid": os.getpid(), "host": "elsewhere", "started": now - 7 * 3600}))
    else:
        path.write_text("{not json")
        os.utime(path, (now - 7 * 3600, now - 7 * 3600))
    logger = logging.getLogger("test-lock")
    with caplog.at_level(logging.WARNING):
        lock = run_screener.acquire_run_lock(logger, path)
    assert lock is not None
    assert "Removing a stale run lock" in caplog.text
    assert json.loads(path.read_text())["pid"] == os.getpid()
    run_screener.release_run_lock(lock)
    assert not path.exists()


def test_a_live_lock_on_another_host_is_respected(tmp_path):
    path = tmp_path / "mark_screener.lock"
    path.write_text(json.dumps({"pid": 1, "host": "other-machine", "started": run_screener.time.time()}))
    assert run_screener.acquire_run_lock(logging.getLogger("test-lock"), path) is None
    assert path.exists()


def test_release_leaves_a_lock_that_is_not_ours(tmp_path):
    path = tmp_path / "mark_screener.lock"
    lock = run_screener.acquire_run_lock(logging.getLogger("test-lock"), path)
    path.write_text(json.dumps({"pid": 1, "token": "someone-else"}))
    run_screener.release_run_lock(lock)
    assert path.exists()
