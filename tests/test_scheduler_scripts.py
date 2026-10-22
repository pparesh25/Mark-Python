"""The macOS scheduler scripts: catch-up decision, retries, generated plist, interpreter choice.

Nothing here touches the real launchd: decisions run in dry-run mode or with a
fake interpreter and a simulated clock, and the install/uninstall scripts run
with HOME in a temporary folder and a fake ``launchctl`` first on PATH.  Every
test needs bash; the scheduler ones need macOS ``date -j`` as well.
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
BASH = Path("/bin/bash")
needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or not BASH.exists(), reason="needs /bin/bash"
)
darwin_only = pytest.mark.skipif(
    sys.platform != "darwin" or not BASH.exists(), reason="uses macOS date -j and launchd"
)

# A Monday-to-Sunday week (2026-10-12 is a Monday).
MONDAY = datetime(2026, 10, 12, tzinfo=IST)
assert MONDAY.isoweekday() == 1


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    return (MONDAY + timedelta(days=day_offset)).replace(hour=hour, minute=minute)


def scheduler_env(tmp_path, now: datetime, machine_tz: str = "Asia/Kolkata", **extra) -> dict:
    return {
        **{key: value for key, value in os.environ.items() if key != "MARK_PYTHON"},
        "TZ": machine_tz,
        "MARK_SCHEDULER_NOW": str(int(now.timestamp())),
        "MARK_SCHEDULER_LAST_RUN_FILE": str(tmp_path / "last_run"),
        **extra,
    }


def write_last_run(tmp_path, when: datetime | None) -> None:
    if when is not None:
        (tmp_path / "last_run").write_text(f"{int(when.timestamp())} {when:%Y-%m-%d %H:%M:%S} IST\n")


def decide(tmp_path, now: datetime, last_run: datetime | None, machine_tz: str = "Asia/Kolkata") -> str:
    write_last_run(tmp_path, last_run)
    result = subprocess.run(
        [str(BASH), str(PROJECT / "run_scheduled.sh"), "--dry-run"],
        capture_output=True, text=True, env=scheduler_env(tmp_path, now, machine_tz), timeout=30, check=True,
    )
    assert "decision:" in result.stdout, result.stdout + result.stderr
    return result.stdout.split("decision:")[1].split("|")[0].strip()


CASES = [
    (at(7, 8), at(4, 19, 40), "skip"),     # Mon 08:00 after Fri 19:40 run
    (at(7, 8), at(3, 19, 40), "run"),      # Mon 08:00 after Thu run: Friday was missed
    (at(5, 10), at(4, 19, 40), "skip"),    # Sat after Fri run
    (at(5, 10), at(3, 19, 40), "run"),     # Sat catch-up of a missed Friday slot
    (at(5, 23, 59), at(4, 19, 40), "skip"),
    (at(6, 0, 5), at(4, 19, 40), "skip"),  # Sun after Fri run
    (at(6, 21), at(4, 19, 40), "skip"),
    (at(8, 20), at(7, 19, 45), "run"),     # Tue 20:00 after Mon run
    (at(8, 19), at(7, 19, 45), "skip"),    # Tue 19:00 after Mon 19:45 run
    (at(8, 19, 30), at(7, 19, 45), "run"), # Tue exactly at the slot
    (at(8, 20), None, "run"),              # no record yet
]


@darwin_only
@pytest.mark.parametrize("now, last_run, expected", CASES)
def test_catch_up_decision(tmp_path, now, last_run, expected):
    assert decide(tmp_path, now, last_run) == expected


@darwin_only
@pytest.mark.parametrize("machine_tz", ["America/New_York", "UTC", "Pacific/Kiritimati"])
def test_decision_is_made_in_india_time_whatever_the_machine_zone(tmp_path, machine_tz):
    for now, last_run, expected in CASES:
        (tmp_path / "last_run").unlink(missing_ok=True)
        assert decide(tmp_path, now, last_run, machine_tz) == expected, (now, last_run, machine_tz)


@darwin_only
def test_a_record_from_the_older_version_is_read_in_local_time(tmp_path):
    (tmp_path / "last_run").write_text("2026-10-16 19:40:00\n")  # Friday, as the old script wrote it
    result = subprocess.run(
        [str(BASH), str(PROJECT / "run_scheduled.sh"), "--dry-run"],
        capture_output=True, text=True, env=scheduler_env(tmp_path, at(7, 8)), timeout=30, check=True,
    )
    assert "decision: skip" in result.stdout


@darwin_only
def test_dry_run_never_writes_the_last_run_record(tmp_path):
    decide(tmp_path, at(8, 20), None)
    assert not (tmp_path / "last_run").exists()


def fake_interpreter(tmp_path, exit_codes: list[int]) -> dict:
    """MARK_PYTHON stand-in that exits with the given codes in turn and logs each call."""
    codes = tmp_path / "codes"
    codes.write_text("\n".join(str(code) for code in exit_codes) + "\n")
    calls = tmp_path / "calls"
    fake = tmp_path / "fake-python"
    fake.write_text(
        "#!/bin/bash\n"
        f'echo "MARK_SCHEDULED=$MARK_SCHEDULED $*" >> "{calls}"\n'
        f'code=$(head -n 1 "{codes}")\n'
        f'tail -n +2 "{codes}" > "{codes}.next"; mv "{codes}.next" "{codes}"\n'
        'exit "${code:-0}"\n'
    )
    fake.chmod(0o755)
    return {"MARK_PYTHON": str(fake), "calls": calls}


def scheduled_run(tmp_path, now: datetime, exit_codes: list[int], *args, machine_tz="Asia/Kolkata"):
    fake = fake_interpreter(tmp_path, exit_codes)
    env = scheduler_env(tmp_path, now, machine_tz, MARK_PYTHON=fake["MARK_PYTHON"])
    result = subprocess.run(
        [str(BASH), str(PROJECT / "run_scheduled.sh"), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )
    calls = fake["calls"].read_text().splitlines() if fake["calls"].exists() else []
    return result, calls


def recorded_start(tmp_path) -> int:
    return int((tmp_path / "last_run").read_text().split()[0])


@darwin_only
def test_success_records_the_start_time_and_runs_scheduled(tmp_path):
    result, calls = scheduled_run(tmp_path, at(8, 19, 30), [0], "--include-sme", machine_tz="UTC")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == ["MARK_SCHEDULED=1 run_screener.py --include-sme"]
    assert recorded_start(tmp_path) == int(at(8, 19, 30).timestamp())
    assert "IST" in (tmp_path / "last_run").read_text()


@darwin_only
def test_stale_data_and_busy_are_retried_every_30_minutes(tmp_path):
    result, calls = scheduled_run(tmp_path, at(8, 19, 30), [3, 4, 0])
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 3
    assert "Retrying at 2026-10-20 20:00:00 IST" in result.stdout
    # The record holds the start of the run that succeeded (20:30).
    assert recorded_start(tmp_path) == int(at(8, 20, 30).timestamp())


@darwin_only
def test_retries_stop_at_23_30_and_the_slot_is_given_up(tmp_path):
    result, calls = scheduled_run(tmp_path, at(8, 19, 30), [3] * 20)
    assert result.returncode == 3
    assert len(calls) == 9  # 19:30, 20:00, ..., 23:30
    assert not (tmp_path / "last_run").exists()
    assert "giving up until the next slot" in result.stdout
    # A later start the same night does not try again; the next slot does.
    assert decide(tmp_path, at(8, 23, 50), None) == "skip"
    assert decide(tmp_path, at(9, 7), None) == "skip"
    assert decide(tmp_path, at(9, 19, 30), None) == "run"


@darwin_only
def test_a_failed_run_is_retried_like_stale_data_and_not_recorded_until_it_succeeds(tmp_path):
    # 2026-10-22: the Mac woke without internet and the run died with exit 1.
    result, calls = scheduled_run(tmp_path, at(8, 19, 30), [1, 1, 0])
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 3
    assert "failed with exit code 1" in result.stdout
    assert recorded_start(tmp_path) == int(at(8, 20, 30).timestamp())


@darwin_only
def test_a_run_that_keeps_failing_gives_up_at_23_30_without_a_record(tmp_path):
    result, calls = scheduled_run(tmp_path, at(8, 19, 30), [1] * 20)
    assert result.returncode == 1
    assert len(calls) == 9
    assert not (tmp_path / "last_run").exists()
    assert decide(tmp_path, at(9, 7), None) == "skip"  # given up for that slot


@darwin_only
def test_a_late_catch_up_gets_a_single_attempt(tmp_path):
    result, calls = scheduled_run(tmp_path, at(5, 10), [3, 0])  # Saturday, for Friday's slot
    assert result.returncode == 3
    assert len(calls) == 1


@needs_bash
def test_schedule_is_defined_once_in_run_scheduled():
    result = subprocess.run(
        [str(BASH), str(PROJECT / "run_scheduled.sh"), "--print-schedule"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert result.stdout.split() == ["19", "30", "Asia/Kolkata", "1", "2", "3", "4", "5"]


def _fake_launchctl(tmp_path, running: bool = False, bootstrap_fails: bool = False) -> dict:
    """A launchctl stand-in that records its arguments and keeps a loaded/not-loaded state."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "launchctl_calls"
    loaded = tmp_path / "loaded"
    fake = bin_dir / "launchctl"
    fake.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{record}"\n'
        'case "$1" in\n'
        f'  print) [ -f "{loaded}" ] || {{ echo "Could not find service" >&2; exit 113; }}\n'
        f'         if [ "{int(running)}" = 1 ]; then echo "	state = running"; echo "	pid = 4242";'
        ' else echo "	state = not running"; fi ;;\n'
        f'  bootstrap|load) [ "{int(bootstrap_fails)}" = 1 ] && {{ echo "Bootstrap failed: 5: Input/output error" >&2;'
        f' exit 5; }}; touch "{loaded}" ;;\n'
        f'  bootout|unload) rm -f "{loaded}" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return {"env": env, "record": record, "home": home, "loaded": loaded}


def _copy_project_scripts(tmp_path) -> Path:
    project = tmp_path / "Some Project & Co"
    project.mkdir()
    for name in ("run_scheduled.sh", "schedule_setup.sh", "schedule_uninstall.sh", "Mark_Python.sh"):
        shutil.copy2(PROJECT / name, project / name)
    return project


def _print_plist(project: Path, env: dict, machine_tz: str) -> dict:
    printed = subprocess.run(
        [str(BASH), str(project / "schedule_setup.sh"), "--print-plist"],
        capture_output=True, text=True, env={**env, "TZ": machine_tz}, timeout=30, check=True,
    )
    return plistlib.loads(printed.stdout.encode())


@darwin_only
def test_setup_generates_the_plist_from_its_own_directory(tmp_path):
    fake = _fake_launchctl(tmp_path)
    project = _copy_project_scripts(tmp_path)

    plist = _print_plist(project, fake["env"], "Asia/Kolkata")
    assert not fake["record"].exists()  # printing never touches launchd
    assert plist["Label"] == "com.paresh.markscreener"
    assert plist["WorkingDirectory"] == str(project)
    assert plist["ProgramArguments"] == ["/bin/bash", str(project / "run_scheduled.sh")]
    assert plist["RunAtLoad"] is True
    assert plist["StartCalendarInterval"] == [
        {"Weekday": day, "Hour": 19, "Minute": 30} for day in range(1, 6)
    ]
    assert plist["StandardOutPath"] == str(project / "logs" / "scheduler_stdout.log")

    result = subprocess.run(
        [str(BASH), str(project / "schedule_setup.sh")],
        capture_output=True, text=True, env={**fake["env"], "TZ": "Asia/Kolkata"}, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    installed = fake["home"] / "Library" / "LaunchAgents" / "com.paresh.markscreener.plist"
    assert plistlib.loads(installed.read_bytes()) == plist
    assert not (project / "com.paresh.markscreener.plist").exists()
    service = f"gui/{os.getuid()}/com.paresh.markscreener"
    calls = fake["record"].read_text().splitlines()
    assert calls == [f"print {service}", f"bootstrap gui/{os.getuid()} {installed}", f"print {service}"]
    assert "Mon 19:30" in result.stdout

    # Reinstalling a loaded, idle job boots it out first.
    subprocess.run([str(BASH), str(project / "schedule_setup.sh")], env={**fake["env"], "TZ": "Asia/Kolkata"},
                   capture_output=True, text=True, timeout=60, check=True)
    assert f"bootout {service}" in fake["record"].read_text().splitlines()

    result = subprocess.run(
        [str(BASH), str(project / "schedule_uninstall.sh")],
        capture_output=True, text=True, env=fake["env"], timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not installed.exists()
    assert not fake["loaded"].exists()


@darwin_only
@pytest.mark.parametrize(
    "machine_tz, expected",
    [
        ("UTC", [(day, 14, 0) for day in range(1, 6)]),
        ("Asia/Tokyo", [(day, 23, 0) for day in range(1, 6)]),
        ("Pacific/Kiritimati", [(day, 4, 0) for day in range(2, 7)]),  # UTC+14: the next morning
    ],
)
def test_plist_slots_are_india_time_converted_to_the_machine_zone(tmp_path, machine_tz, expected):
    fake = _fake_launchctl(tmp_path)
    plist = _print_plist(_copy_project_scripts(tmp_path), fake["env"], machine_tz)
    assert [(slot["Weekday"], slot["Hour"], slot["Minute"]) for slot in plist["StartCalendarInterval"]] == expected


@darwin_only
def test_setup_refuses_to_stop_a_running_job_without_force(tmp_path):
    fake = _fake_launchctl(tmp_path, running=True)
    fake["loaded"].touch()
    project = _copy_project_scripts(tmp_path)
    result = subprocess.run([str(BASH), str(project / "schedule_setup.sh")], env=fake["env"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "running right now (pid 4242)" in result.stderr
    assert not any(line.startswith("bootout") for line in fake["record"].read_text().splitlines())

    result = subprocess.run([str(BASH), str(project / "schedule_setup.sh"), "--force"], env=fake["env"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("bootout") for line in fake["record"].read_text().splitlines())


@darwin_only
def test_setup_reports_a_job_that_did_not_load(tmp_path):
    fake = _fake_launchctl(tmp_path, bootstrap_fails=True)
    project = _copy_project_scripts(tmp_path)
    result = subprocess.run([str(BASH), str(project / "schedule_setup.sh")], env=fake["env"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "Bootstrap failed: 5: Input/output error" in result.stderr
    assert "is not loaded" in result.stderr


def _fake_python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/bash\necho "FAKE-PYTHON $0 $*"\n')
    path.chmod(0o755)
    return path


def _mark_python(project: Path, home: Path, **extra) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "MARK_PYTHON"}
    env.update({"HOME": str(home), "PATH": "/usr/bin:/bin", **extra})
    return subprocess.run(
        [str(BASH), str(project / "Mark_Python.sh"), "--no-watchlists"],
        capture_output=True, text=True, env=env, timeout=30,
    )


@needs_bash
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


@needs_bash
def test_mark_python_rejects_a_bad_override(tmp_path):
    project = _copy_project_scripts(tmp_path)
    result = _mark_python(project, tmp_path / "home", MARK_PYTHON=str(tmp_path / "missing"))
    assert result.returncode != 0
    assert "MARK_PYTHON is set but is not an executable file" in result.stderr


@needs_bash
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
