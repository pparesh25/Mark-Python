"""A Screener.in file taken during the day carries intraday "Current Price"; only an after-close file is reused."""

from __future__ import annotations

import os
from datetime import date, datetime

import config
import run_screener


def _file_at(tmp_path, when: datetime):
    path = tmp_path / "all-company_20261105.csv"
    path.write_text("x")
    stamp = when.replace(tzinfo=config.MARKET_TIMEZONE).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_a_file_taken_at_midday_is_before_the_close(tmp_path):
    # Windows, 2026-11-05 12:50 IST
    assert run_screener.fundamentals_taken_before_close(_file_at(tmp_path, datetime(2026, 11, 5, 12, 50)), date(2026, 11, 5))


def test_a_file_taken_after_18_00_is_kept(tmp_path):
    # Mac, 2026-11-05 19:30 IST
    assert not run_screener.fundamentals_taken_before_close(_file_at(tmp_path, datetime(2026, 11, 5, 19, 30)), date(2026, 11, 5))


def test_a_weekend_file_covers_fridays_session(tmp_path):
    assert not run_screener.fundamentals_taken_before_close(_file_at(tmp_path, datetime(2026, 11, 7, 10, 0)), date(2026, 11, 6))


def test_the_machine_time_zone_does_not_matter(tmp_path, monkeypatch):
    path = _file_at(tmp_path, datetime(2026, 11, 5, 19, 30))
    monkeypatch.setenv("TZ", "America/New_York")
    if hasattr(__import__("time"), "tzset"):
        __import__("time").tzset()
    assert not run_screener.fundamentals_taken_before_close(path, date(2026, 11, 5))
