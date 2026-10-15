"""The macOS scheduler scripts: catch-up decision, generated plist, interpreter choice.

Nothing here touches the real launchd: the decision runs in dry-run mode, and
the install/uninstall scripts run with HOME in a temporary folder and a fake
``launchctl`` first on PATH.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PROJECT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="uses macOS date -j and launchd")

# A Monday-to-Sunday week (2026-10-12 is a Monday).
MONDAY = datetime(2026, 10, 12, tzinfo=IST)
assert MONDAY.isoweekday() == 1


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    return (MONDAY + timedelta(days=day_offset)).replace(hour=hour, minute=minute)


def decide(tmp_path, now: datetime, last_run: datetime | None) -> str:
    last_file = tmp_path / "last_run"
    if last_run is not None:
        last_file.write_text(last_run.strftime("%Y-%m-%d %H:%M:%S") + "\n")
    env = {
        **os.environ,
        "TZ": "Asia/Kolkata",
        "MARK_SCHEDULER_NOW": str(int(now.timestamp())),
        "MARK_SCHEDULER_LAST_RUN_FILE": str(last_file),
    }
    result = subprocess.run(
        ["/bin/bash", str(PROJECT / "run_scheduled.sh"), "--dry-run"],
        capture_output=True, text=True, env=env, timeout=30, check=True,
    )
    assert "decision:" in result.stdout, result.stdout + result.stderr
    return result.stdout.split("decision:")[1].split("|")[0].strip()


@darwin_only
@pytest.mark.parametrize(
    "now, last_run, expected",
    [
        (at(7, 8), at(4, 19, 40), "skip"),     # Mon 08:00 after Fri 19:40 run
        (at(7, 8), at(3, 19, 40), "run"),      # Mon 08:00 after Thu run: Friday was missed
        (at(5, 10), at(4, 19, 40), "skip"),    # Sat after Fri run
        (at(5, 23, 59), at(4, 19, 40), "skip"),
        (at(6, 0, 5), at(4, 19, 40), "skip"),  # Sun after Fri run
        (at(6, 21), at(4, 19, 40), "skip"),
        (at(8, 20), at(7, 19, 45), "run"),     # Tue 20:00 after Mon run
        (at(8, 19), at(7, 19, 45), "skip"),    # Tue 19:00 after Mon 19:45 run
        (at(8, 19, 30), at(7, 19, 45), "run"), # Tue exactly at the slot
        (at(8, 20), None, "run"),              # no record yet
    ],
)
def test_catch_up_decision(tmp_path, now, last_run, expected):
    assert decide(tmp_path, now, last_run) == expected


@darwin_only
def test_dry_run_never_writes_the_last_run_record(tmp_path):
    decide(tmp_path, at(8, 20), None)
    assert not (tmp_path / "last_run").exists()


def test_schedule_is_defined_once_in_run_scheduled():
    result = subprocess.run(
        ["/bin/bash", str(PROJECT / "run_scheduled.sh"), "--print-schedule"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert result.stdout.split() == ["19", "30", "1", "2", "3", "4", "5"]


def _fake_launchctl(tmp_path) -> dict:
    """A launchctl stand-in that records its arguments, first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "launchctl_calls"
    fake = bin_dir / "launchctl"
    fake.write_text(f'#!/bin/bash\necho "$*" >> "{record}"\nexit 0\n')
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return {"env": env, "record": record, "home": home}


def _copy_project_scripts(tmp_path) -> Path:
    project = tmp_path / "Some Project & Co"
    project.mkdir()
    for name in ("run_scheduled.sh", "schedule_setup.sh", "schedule_uninstall.sh", "Mark_Python.sh"):
        shutil.copy2(PROJECT / name, project / name)
    return project


@darwin_only
def test_setup_generates_the_plist_from_its_own_directory(tmp_path):
    fake = _fake_launchctl(tmp_path)
    project = _copy_project_scripts(tmp_path)

    printed = subprocess.run(
        ["/bin/bash", str(project / "schedule_setup.sh"), "--print-plist"],
        capture_output=True, text=True, env=fake["env"], timeout=30, check=True,
    )
    assert not fake["record"].exists()  # printing never touches launchd
    plist = plistlib.loads(printed.stdout.encode())
    assert plist["Label"] == "com.paresh.markscreener"
    assert plist["WorkingDirectory"] == str(project)
    assert plist["ProgramArguments"] == ["/bin/bash", str(project / "run_scheduled.sh")]
    assert plist["RunAtLoad"] is True
    assert plist["StartCalendarInterval"] == [
        {"Weekday": day, "Hour": 19, "Minute": 30} for day in range(1, 6)
    ]
    assert plist["StandardOutPath"] == str(project / "logs" / "scheduler_stdout.log")

    subprocess.run(
        ["/bin/bash", str(project / "schedule_setup.sh")],
        capture_output=True, text=True, env=fake["env"], timeout=60, check=True,
    )
    installed = fake["home"] / "Library" / "LaunchAgents" / "com.paresh.markscreener.plist"
    assert plistlib.loads(installed.read_bytes()) == plist
    assert not (project / "com.paresh.markscreener.plist").exists()
    calls = fake["record"].read_text().splitlines()
    domain = f"gui/{os.getuid()}"
    assert calls[0] == f"bootout {domain}/com.paresh.markscreener"
    assert calls[-1] == f"bootstrap {domain} {installed}"

    subprocess.run(
        ["/bin/bash", str(project / "schedule_uninstall.sh")],
        capture_output=True, text=True, env=fake["env"], timeout=30, check=True,
    )
    assert not installed.exists()
    assert fake["record"].read_text().splitlines()[-1] == f"bootout {domain}/com.paresh.markscreener"


def _fake_python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/bash\necho "FAKE-PYTHON $0 $*"\n')
    path.chmod(0o755)
    return path


def _mark_python(project: Path, home: Path, **extra) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "MARK_PYTHON"}
    env.update({"HOME": str(home), "PATH": "/usr/bin:/bin", **extra})
    return subprocess.run(
        ["/bin/bash", str(project / "Mark_Python.sh"), "--no-watchlists"],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_mark_python_prefers_mark_python_then_venv_then_conda(tmp_path):
    project = _copy_project_scripts(tmp_path)
    home = tmp_path / "home"
    conda = _fake_python(home / "miniconda3" / "envs" / "mark_screener" / "bin" / "python")

    result = _mark_python(project, home)
    assert result.returncode == 0, result.stderr
    assert f"FAKE-PYTHON {conda} run_screener.py --no-watchlists" in result.stdout
    assert f"using {conda} (conda env mark_screener under {home / 'miniconda3'})" in result.stdout

    forge = _fake_python(home / "miniforge3" / "envs" / "mark_screener" / "bin" / "python")
    assert f"FAKE-PYTHON {forge} " in _mark_python(project, home).stdout

    venv = _fake_python(project / ".venv" / "bin" / "python")
    assert f"FAKE-PYTHON {venv} " in _mark_python(project, home).stdout

    explicit = _fake_python(tmp_path / "custom" / "python")
    result = _mark_python(project, home, MARK_PYTHON=str(explicit))
    assert f"FAKE-PYTHON {explicit} " in result.stdout
    assert "(MARK_PYTHON)" in result.stdout


def test_mark_python_rejects_a_bad_override(tmp_path):
    project = _copy_project_scripts(tmp_path)
    result = _mark_python(project, tmp_path / "home", MARK_PYTHON=str(tmp_path / "missing"))
    assert result.returncode != 0
    assert "MARK_PYTHON is set but is not an executable file" in result.stderr


@pytest.mark.skipif(
    Path("/opt/homebrew/Caskroom/miniforge/base/envs/mark_screener/bin/python").exists(),
    reason="a system-wide mark_screener env exists on this machine",
)
def test_mark_python_explains_a_missing_environment(tmp_path):
    project = _copy_project_scripts(tmp_path)
    result = _mark_python(project, tmp_path / "home")
    assert result.returncode == 1
    assert "no Python environment found" in result.stderr
    assert "MARK_PYTHON" in result.stderr
