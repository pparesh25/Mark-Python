"""Tests for non-strategy screener services."""

from pathlib import Path
from datetime import date, timedelta

import pandas as pd

import config
import run_screener
from mark_screener import results


def test_results_are_indexed_by_latest_breakout_first(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WATCHLISTS_ENABLED", False)
    monkeypatch.setattr(results, "CURRENT_TIMESTAMP", "20260730_120000")
    out_dir = tmp_path / "csv"
    out_dir.mkdir()
    today = date.today()
    technical = pd.DataFrame({
        "symbol": ["OLDER", "LATEST"],
        "isin": ["IN1", "IN2"],
        "date": [today.isoformat(), today.isoformat()],
        "breakout_date": [
            (today - timedelta(days=5)).isoformat(),
            (today - timedelta(days=1)).isoformat(),
        ],
    })
    fundamentals = pd.DataFrame({
        "ISIN Code": ["IN1", "IN2"],
        "Name": ["Older Ltd", "Latest Ltd"],
    })

    ordered = results.process_results(
        technical,
        fundamentals,
        "nse_tight_flag",
        out_dir,
        tmp_path / "charts",
        "breakout_date",
        tmp_path,
        render_charts=False,
    )

    assert ordered["symbol"].tolist() == ["LATEST", "OLDER"]
    assert ordered["Days_Since_Breakout"].tolist() == [1, 5]
    exported = pd.read_csv(
        out_dir / "20260730_120000_nse_tight_flag_qualified.csv"
    )
    assert exported[["Index", "symbol", "Days_Since_Breakout"]].to_dict(
        "records"
    ) == [
        {"Index": 0, "symbol": "LATEST", "Days_Since_Breakout": 1},
        {"Index": 1, "symbol": "OLDER", "Days_Since_Breakout": 5},
    ]


def test_opentrader_watchlist_paths_are_exchange_specific(tmp_path):
    assert config.get_watchlist_dir("NSE", root=tmp_path) == (
        tmp_path / "databases" / "NSE_EOD" / "watchlists"
    )
    assert config.get_watchlist_dir("BSE", root=tmp_path) == (
        tmp_path / "databases" / "BSE_EOD" / "watchlists"
    )


def test_screener_credentials_live_below_portable_data_root(tmp_path):
    assert config.get_screener_credentials_path(tmp_path) == (
        tmp_path / "Screener login" / "screener.txt"
    )


def test_shared_result_writer_preserves_database_symbol_suffix(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "WATCHLISTS_ENABLED", True)
    monkeypatch.setattr(config, "STRIP_SME_WATCHLIST_SUFFIX", False)
    monkeypatch.setattr(config, "OPENTRADER_ROOT", tmp_path)
    monkeypatch.setattr(results, "CURRENT_TIMESTAMP", "20260727_120000")
    frame = pd.DataFrame({
        "Exchange": ["BSE", "BSE", "BSE"],
        "symbol": ["SECOND_SME", "FIRST", "FIRST"],
    })

    output = results.write_watchlist(frame, "bse_tight_flag")

    assert output == (
        tmp_path / "databases" / "BSE_EOD" / "watchlists"
        / "20260727_120000_bse_tight_flag.txt"
    )
    assert output.read_text(encoding="utf-8") == "SECOND_SME\nFIRST\n"


def test_watchlist_writer_can_strip_sme_suffix_explicitly(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "WATCHLISTS_ENABLED", True)
    monkeypatch.setattr(config, "STRIP_SME_WATCHLIST_SUFFIX", True)
    monkeypatch.setattr(config, "OPENTRADER_ROOT", tmp_path)
    monkeypatch.setattr(results, "CURRENT_TIMESTAMP", "20260727_120001")

    output = results.write_watchlist(
        pd.DataFrame({"Exchange": ["NSE"], "symbol": ["ABC_SME"]}),
        "nse_mark_tight",
    )

    assert output.read_text(encoding="utf-8") == "ABC\n"


def test_watchlist_writer_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WATCHLISTS_ENABLED", False)
    monkeypatch.setattr(config, "OPENTRADER_ROOT", tmp_path)

    output = results.write_watchlist(
        pd.DataFrame({"Exchange": ["NSE"], "symbol": ["ABC"]}),
        "nse_mark_tight",
    )

    assert output is None
    assert not any(tmp_path.rglob("*"))


def test_fundamental_selection_requires_explicit_stale_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "FUNDAMENTAL_DIR", tmp_path)
    monkeypatch.setattr(config, "CURRENT_DATE", "20260727")
    older = tmp_path / "all-company_20260726.csv"
    older.write_text("old", encoding="utf-8")

    assert config.select_fundamental_file() == (
        tmp_path / "all-company_20260727.csv"
    )
    assert config.select_fundamental_file(allow_stale=True) == older


def test_portable_cli_accepts_path_and_watchlist_flags():
    args = run_screener.build_argument_parser().parse_args([
        "--data-root",
        "/data/mark",
        "--watchlist-root",
        "/data/opentrader",
        "--no-watchlists",
        "--strip-sme-watchlist-suffix",
        "--allow-stale-fundamentals",
    ])

    assert args.data_root == Path("/data/mark")
    assert args.watchlist_root == Path("/data/opentrader")
    assert args.watchlists is False
    assert args.strip_sme_watchlist_suffix is True
    assert args.allow_stale_fundamentals is True
