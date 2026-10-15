"""setup_macos.sh accepts only Python 3.10-3.13 (the pinned numpy 2.2.6 has no other wheels)."""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or platform.machine() != "arm64" or not Path("/bin/bash").exists(),
    reason="setup_macos.sh runs only on Apple Silicon macOS",
)


def _fake_python(path: Path, version: str) -> Path:
    major, minor = (int(part) for part in version.split(".")[:2])
    ok = (3, 10) <= (major, minor) <= (3, 13)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = "--version" ]; then echo "Python {version}"; exit 0; fi\n'
        f'if [ "$1" = "-m" ]; then echo "FAKE venv $*"; exit 7; fi\n'
        f"exit {0 if ok else 1}\n"
    )
    path.chmod(0o755)
    return path


def _setup(tmp_path, env_extra) -> subprocess.CompletedProcess:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    shutil.copy2(PROJECT / "setup_macos.sh", project / "setup_macos.sh")
    env = {key: value for key, value in os.environ.items() if key != "PYTHON_BIN"}
    env.update(env_extra)
    return subprocess.run(["/bin/bash", str(project / "setup_macos.sh"), "--skip-rust"],
                          capture_output=True, text=True, env=env, timeout=60)


def test_an_unsupported_python_bin_is_refused(tmp_path):
    fake = _fake_python(tmp_path / "bin" / "python3.14", "3.14.0")
    result = _setup(tmp_path, {"PYTHON_BIN": str(fake)})
    assert result.returncode == 1
    assert "is not Python 3.10-3.13" in result.stderr
    assert "FAKE venv" not in result.stdout


def test_the_search_skips_unsupported_versions(tmp_path):
    bin_dir = tmp_path / "bin"
    _fake_python(bin_dir / "python3", "3.14.0")
    result = _setup(tmp_path, {"PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert result.returncode == 1
    assert "No supported Python found" in result.stderr

    _fake_python(bin_dir / "python3.12", "3.12.7")
    result = _setup(tmp_path, {"PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert "Using python3.12 (Python 3.12.7)" in result.stdout
    assert "FAKE venv -m venv .venv" in result.stdout  # it went on to create the venv with 3.12
