import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from mark_screener import fast_pipeline


def test_screen_all_requests_both_exchanges_and_strategies_in_one_rust_call(
    tmp_path, monkeypatch
):
    captured = {}
    monkeypatch.setattr(fast_pipeline.config, "OUT_DIR", tmp_path)
    monkeypatch.setattr(fast_pipeline.config, "RUST_BINARY", Path("/fake/screener"))
    monkeypatch.setattr(fast_pipeline.config, "DUCKDB_FILE", Path("/fake/eod.duckdb"))
    monkeypatch.setattr(
        fast_pipeline.config, "FUNDAMENTAL_FILE", Path("/fake/fundamentals.csv")
    )

    def fake_run(cmd, capture_output, text, encoding, errors):
        captured["cmd"] = cmd
        for exchange in ("NSE", "BSE"):
            output_dir = tmp_path / exchange
            output_dir.mkdir(exist_ok=True)
            output = output_dir / f"{exchange}_rust_screened.json"
            output.write_text(
                json.dumps(
                    {
                        "selected_symbols": 0,
                        "history_ready": 0,
                        "max_history": 0,
                        "mark_tight_qualified": [],
                        "tight_flag_qualified": [],
                    }
                )
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(fast_pipeline.subprocess, "run", fake_run)

    datasets, _ = fast_pipeline._screen_all_exchanges()

    assert datasets["NSE"]["mark_tight_qualified"] == []
    assert datasets["BSE"]["tight_flag_qualified"] == []
    assert "ALL" in captured["cmd"]
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
    assert jobs[1][5] == "breakout_date"


def test_chart_reset_removes_legacy_mark_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(fast_pipeline.config, "CHART_DIR", tmp_path)
    legacy = tmp_path / "NSE" / "mark"
    legacy.mkdir(parents=True)
    (legacy / "old.png").write_bytes(b"old")

    fast_pipeline._reset_chart_dirs()

    assert not legacy.exists()
    for exchange in ("NSE", "BSE"):
        assert (tmp_path / exchange / "mark_tight").is_dir()
        assert (tmp_path / exchange / "Tight Flag").is_dir()
