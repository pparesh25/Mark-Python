"""Tests for non-strategy screener services."""

from pathlib import Path

import pandas as pd

import config
import run_screener
from mark_screener import results
from mark_screener.scoring import add_priority_score


def test_priority_score_service_adds_shared_output_columns():
    frame = pd.DataFrame({
        "Current Price": [100, 200],
        "Debt to equity": [0.5, 1.5],
        "Volume 1month average": [1_000_000, 2_000_000],
        "Return over 1year": [20, 10],
        "Return over 3months": [8, 4],
        "Return on capital employed": [18, 10],
        "Return on equity": [16, 8],
        "Piotroski score": [8, 5],
        "Altman Z Score": [3, 1],
        "YOY Quarterly sales growth": [20, 5],
        "YOY Quarterly profit growth": [25, 3],
        "Change in FII holding": [2, -1],
        "Change in DII holding": [1, -2],
        "Pledged percentage": [5, 20],
    })

    result = add_priority_score(frame)

    assert {"Priority_Score", "Turnover_cr", "Tradeable"}.issubset(result.columns)
    assert result.loc[0, "Tradeable"] == "Y"
    assert result.loc[1, "Tradeable"] == "N"
    assert result["Priority_Score"].notna().all()


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
