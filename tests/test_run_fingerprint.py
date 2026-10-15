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


def make_store(path, rows=ROWS, factors=True):
    conn = duckdb.connect(str(path))
    try:
        create_schema(conn)
        conn.executemany(
            "INSERT INTO securities VALUES (?, ?, ?, 'EQ', NULL)",
            [("NSE", "AAA", "INE000A01011"), ("BSE", "BBB", "INE000B01011")],
        )
        conn.executemany(
            "INSERT INTO raw_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, 10, 5)", rows
        )
        if factors:
            conn.execute("INSERT INTO adjustment_periods VALUES ('NSE', 'AAA', DATE '2026-10-13', 0.5)")
        else:
            # Factor and action tables renamed or gone (the view needs only adjustment_periods).
            conn.execute("DROP TABLE price_adjustments")
            conn.execute("DROP TABLE exchange_actions")
    finally:
        conn.close()
    return path


def store_section(path):
    return run_fingerprint._store_section(path)


def test_store_hash_ignores_row_order_and_sees_one_price(tmp_path):
    first = store_section(make_store(tmp_path / "a.duckdb"))
    reordered = store_section(make_store(tmp_path / "b.duckdb", rows=list(reversed(ROWS))))
    changed_rows = list(ROWS)
    changed_rows[1] = changed_rows[1][:6] + (109.26,) + changed_rows[1][7:]
    changed = store_section(make_store(tmp_path / "c.duckdb", rows=changed_rows))

    assert first["schema_version"] == 2
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
    assert "hash" not in first["tables"]["raw_prices"]


def test_store_hash_is_a_fixed_value(tmp_path):
    """The text hashed is platform independent, so the value itself is pinned."""
    store = store_section(make_store(tmp_path / "a.duckdb"))
    # AAA is halved by its factor: Close 52.25 and 54.625; volume doubled.
    import hashlib

    def row_hash(*fields):
        text = "|".join(str(field) for field in fields)
        return int(hashlib.md5(text.encode()).hexdigest()[:16], 16)

    expected = (
        row_hash("AAA", "2026-10-13", 500000, 525000, 495000, 522500, 2000)
        + row_hash("AAA", "2026-10-14", 522500, 550000, 520000, 546250, 3000)
    ) % (1 << 64)
    assert store["exchanges"]["NSE"]["content_hash"] == f"{expected:016x}"


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
    for package in ("python", "pandas", "numpy", "duckdb"):
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
        "packages": {"pandas": "2.3.3"},
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
    (tmp_path / "c.json").write_text(json.dumps(_fingerprint(run__platform="Linux")))
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


def test_pipeline_warns_on_as_of_mismatch_and_writes_the_fingerprint(tmp_path, monkeypatch):
    """main(): the mismatch is logged where found, repeated in the summary, and kept in the JSON."""
    for name in ("DATA_ROOT", "DATABASE_DIR", "DATA_DIR", "BSE_DATA_DIR", "DUCKDB_FILE", "FUNDAMENTAL_DIR",
                 "FUNDAMENTAL_FILE", "OUT_DIR", "CHART_DIR", "LOG_DIR", "DAILY_BHAVCOPY_DIR",
                 "BSE_DAILY_BHAVCOPY_DIR", "EOD_METADATA_DIR", "LOG_FILE", "SCREENER_CREDS",
                 "WATCHLISTS_ENABLED", "OPENTRADER_ROOT", "STRIP_SME_WATCHLIST_SUFFIX", "RUST_BINARY"):
        monkeypatch.setattr(config, name, getattr(config, name))
    for name in ("MARK_DATA_ROOT", "MARK_WRITE_WATCHLISTS", "MARK_OPENTRADER_ROOT",
                 "MARK_STRIP_SME_WATCHLIST_SUFFIX", "MARK_SCREENER_TIMESTAMP"):
        # setenv records the original value (or its absence) so main()'s writes are undone.
        original = os.environ.get(name)
        monkeypatch.setenv(name, original or "")
        if original is None:
            monkeypatch.delenv(name)
    root_logger = logging.getLogger()
    monkeypatch.setattr(root_logger, "handlers", list(root_logger.handlers))
    monkeypatch.setattr(root_logger, "level", root_logger.level)

    root = tmp_path / "root"
    config.configure_data_root(root)
    (root / "Database").mkdir(parents=True)
    make_store(config.DUCKDB_FILE)
    fundamentals = config.FUNDAMENTAL_DIR / f"all-company_{config.CURRENT_DATE}.csv"
    fundamentals.parent.mkdir(parents=True)
    header = ("ISIN Code,Name,Industry,Current Price,Market Capitalization,"
              "Return on capital employed,Return on equity")
    fundamentals.write_text(header + "\n" + "INE000A01011,A,X,1,2,3,4\n" * 60)
    monkeypatch.setattr(config, "RUST_BINARY", tmp_path / "no-engine")
    readiness = EodReadiness(target_date=date(2026, 10, 14),
                             as_of_dates={"NSE": date(2026, 10, 14), "BSE": date(2026, 10, 13)})
    monkeypatch.setattr(run_screener, "inspect_eod_readiness", lambda *args: readiness)

    def fake_screening(logger, include_sme=False, only_sme=False):
        folder = config.OUT_DIR / "NSE" / "mark_tight"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{config.CURRENT_TIMESTAMP}_nse_mark_tight_qualified.csv").write_text(
            "Index,symbol,Mark_Tight_Date\n0,AAA,2026-10-14\n")

    monkeypatch.setattr(run_screener, "run_all_screening", fake_screening)
    monkeypatch.setattr(sys, "argv", ["run_screener.py", "--data-root", str(root), "--skip-sync", "--no-watchlists"])

    run_screener.main()
    for handler in logging.getLogger().handlers:
        handler.flush()

    log = config.LOG_FILE.read_text(encoding="utf-8")
    mismatch = "NSE and BSE local data end on different sessions (BSE=2026-10-13, NSE=2026-10-14)"
    assert log.count(f"RESULTS MAY DIFFER: {mismatch}") == 1
    summary = log.split("Pipeline complete")[1]
    assert f"! {mismatch}" in summary
    assert "screening local fallback data" in summary
    assert "Run fingerprint:" in log

    fingerprint = json.loads(run_fingerprint.fingerprint_path().read_text(encoding="utf-8"))
    assert fingerprint["data_as_of"]["as_of_mismatch"] is True
    assert fingerprint["data_as_of"]["local_fallback"] is True
    assert any(mismatch in message for message in fingerprint["data_as_of"]["warnings"])
    assert fingerprint["outputs"]["NSE/mark_tight"]["entries"] == ["AAA|2026-10-14"]
    assert fingerprint["store"]["exchanges"]["NSE"]["rows"] == 2
