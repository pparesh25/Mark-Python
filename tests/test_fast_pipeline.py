import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pandas as pd

from mark_screener import fast_pipeline


def test_screen_exchange_requests_both_strategies_in_one_rust_call(
    tmp_path, monkeypatch
):
    captured = {}
    monkeypatch.setattr(fast_pipeline.config, "OUT_DIR", tmp_path)
    monkeypatch.setattr(fast_pipeline.config, "RUST_BINARY", Path("/fake/screener"))
    monkeypatch.setattr(fast_pipeline.config, "DUCKDB_FILE", Path("/fake/eod.duckdb"))
    monkeypatch.setattr(
        fast_pipeline.config, "FUNDAMENTAL_FILE", Path("/fake/fundamentals.csv")
    )

    def fake_run(cmd, capture_output, text):
        captured["cmd"] = cmd
        output = tmp_path / "NSE" / "NSE_rust_screened.json"
        output.write_text(
            json.dumps(
                {
                    "mark_tight_qualified": [],
                    "tight_flag_qualified": [],
                }
            )
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(fast_pipeline.subprocess, "run", fake_run)

    exchange, dataset, _ = fast_pipeline._screen_exchange("NSE")

    assert exchange == "NSE"
    assert dataset["mark_tight_qualified"] == []
    assert dataset["tight_flag_qualified"] == []
    assert "--mark-tight-only" not in captured["cmd"]
    assert "--tight-flag-only" not in captured["cmd"]
    assert captured["cmd"].count("screen") == 1


def test_result_jobs_prepare_both_output_categories():
    dataset = {
        "mark_tight_qualified": [
            {
                "symbol": "ABC",
                "ema_50": 10,
                "ema_150": 9,
                "ema_200": 8,
                "mark_tight_date": "2026-07-24",
            }
        ],
        "tight_flag_qualified": [
            {
                "symbol": "XYZ",
                "tight_flag_date": "2026-07-24",
            }
        ],
    }

    jobs = fast_pipeline._result_jobs("NSE", dataset, pd.DataFrame())

    assert [job[2] for job in jobs] == ["nse_mark_tight", "nse_tight_flag"]
    assert jobs[0][5] == "Mark_Tight_Date"
    assert {"EMA_50", "EMA_150", "EMA_200"}.issubset(jobs[0][0].columns)
    assert jobs[1][5] == "tight_flag_date"


def test_filter_stats_apply_fundamental_and_bse_unique_isin_rules(
    tmp_path, monkeypatch
):
    fundamental_file = tmp_path / "fundamentals.csv"
    pd.DataFrame(
        {"ISIN Code": ["IN_SHARED", "IN_NSE", "IN_BSE", "IN_BSE_SME"]}
    ).to_csv(fundamental_file, index=False)

    database = tmp_path / "eod.duckdb"
    conn = duckdb.connect(str(database))
    conn.execute(
        """
        CREATE TABLE eod_data (
            Exchange VARCHAR, Symbol VARCHAR, ISIN VARCHAR
        )
        """
    )
    conn.executemany(
        "INSERT INTO eod_data VALUES (?, ?, ?)",
        [
            ("NSE", "SHARED", "IN_SHARED"),
            ("NSE", "NSEONLY", "IN_NSE"),
            ("NSE", "ETF", "NOT_IN_FUNDAMENTALS"),
            ("BSE", "SHARED_BSE", "IN_SHARED"),
            ("BSE", "BSEONLY", "IN_BSE"),
            ("BSE", "BSEONLY", "IN_BSE"),
            ("BSE", "BSEONLY_SME", "IN_BSE_SME"),
        ],
    )
    conn.close()

    monkeypatch.setattr(fast_pipeline.config, "FUNDAMENTAL_FILE", fundamental_file)
    monkeypatch.setattr(fast_pipeline.config, "DUCKDB_FILE", database)
    monkeypatch.setattr(fast_pipeline.config, "MIN_HISTORY_DAYS", 2)

    stats = fast_pipeline._load_filter_stats()

    assert stats["fundamental_isins"] == 4
    assert stats["nse_matching"] == 2
    assert stats["bse_matching"] == 3
    assert stats["bse_overlap"] == 1
    assert stats["bse_unique"] == 2
    assert stats["NSE"]["selected"] == 2
    assert stats["BSE"]["selected"] == 1
    assert stats["BSE"]["history_ready"] == 1
