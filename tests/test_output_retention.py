"""Old run outputs are removed; anything else in the folders is kept."""

from __future__ import annotations

import logging
from datetime import date

import config
import output_retention


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    return path


def test_only_dated_run_outputs_older_than_the_limit_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "qualified_stocks")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    out, logs = config.OUT_DIR / "NSE" / "mark_tight", config.LOG_DIR
    old = [touch(out / "20260901_193000_nse_mark_tight_qualified.csv"),
           touch(logs / "screening_log_20260901_193000.txt"),
           touch(logs / "run_fingerprint_20260901_193000.json")]
    kept = [touch(out / "20261001_193000_nse_mark_tight_qualified.csv"),       # 48 days: inside 60
            touch(out / "notes_20200101.csv"),                                  # not a run output
            touch(logs / "scheduler_stdout.log"),
            touch(logs / "screening_log_20260931_193000.txt")]                  # not a real date
    removed = output_retention.prune(logging.getLogger("t"), date(2026, 11, 18), keep_days=60)
    assert removed == 3
    assert not any(p.exists() for p in old)
    assert all(p.exists() for p in kept)
    assert output_retention.prune(logging.getLogger("t"), date(2026, 11, 18), keep_days=0) == 0
