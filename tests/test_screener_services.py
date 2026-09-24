"""Tests for non-strategy screener services."""

from pathlib import Path

import config
import run_screener


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


def test_local_processing_invokes_one_rust_pipeline(tmp_path, monkeypatch):
    rust_binary = tmp_path / "stock_analysis"
    rust_binary.write_bytes(b"binary")
    monkeypatch.setattr(config, "RUST_BINARY", rust_binary)
    monkeypatch.setattr(config, "DUCKDB_FILE", tmp_path / "eod.duckdb")
    monkeypatch.setattr(config, "FUNDAMENTAL_FILE", tmp_path / "fund.csv")
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "qualified")
    monkeypatch.setattr(config, "CHART_DIR", tmp_path / "charts")
    monkeypatch.setattr(config, "OPENTRADER_ROOT", tmp_path / "opentrader")
    monkeypatch.setattr(config, "CURRENT_TIMESTAMP", "20260730_120000")
    monkeypatch.setattr(config, "FLAG_LOOKBACK_DAYS", 90)
    monkeypatch.setattr(config, "WATCHLISTS_ENABLED", False)
    monkeypatch.setattr(config, "STRIP_SME_WATCHLIST_SUFFIX", True)
    captured = {}

    def fake_run_step(name, cmd, logger, cwd=None):
        captured["name"] = name
        captured["cmd"] = cmd
        return 0.1

    monkeypatch.setattr(run_screener, "run_step", fake_run_step)
    run_screener.run_all_screening(
        logger=None,
        include_sme=True,
    )

    command = captured["cmd"]
    assert command[:2] == [str(rust_binary), "pipeline"]
    assert command.count("pipeline") == 1
    assert str(config.DUCKDB_FILE) in command
    assert str(config.FUNDAMENTAL_FILE) in command
    assert "--include-sme" in command
    assert "--no-watchlists" in command
    assert "--strip-sme-suffix" in command
    stale_flag = command.index("--max-stale-sessions")
    assert command[stale_flag + 1] == str(config.MAX_STALE_SESSIONS)


def test_rust_binary_description_includes_checksum(tmp_path):
    binary = tmp_path / "stock_analysis"
    binary.write_bytes(b"binary")

    description = run_screener.describe_rust_binary(binary)

    # sha256(b"binary") starts with 9a3a45d0; a non-executable file still
    # gets a checksum instead of aborting the run.
    assert "sha256 9a3a45d01531a20e" in description
    assert str(binary) in description


def test_run_timestamp_uses_the_exchange_time_zone():
    assert str(config.MARKET_TIMEZONE) == "Asia/Kolkata"


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
